# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family audit log (PN-131).

One **Friends and Family Log** row per traceable F&F decision, so support and
admins can answer "why did / didn't this customer get F&F?" from the desk
(filters: date, customer, log type, invoice).

What is logged, and when
------------------------
* **Invoice submit** (Sales Invoice / POS Invoice ``on_submit``) — from the
  decision the validate hook just took (``doc.flags.friends_and_family``), one
  row per relevant line:

  - ``Discount Applied``: customer, invoice, item, F&F amount (line total, from
    the row's ``posnext_fnf_discount_amount``), %, source, comparison outcome
    vs the line's promotion / coupon / manual discount, capped flag.
  - ``Comparison Lost``: F&F covered the line but another discount was bigger
    (line promotion / coupon / manual, or an order-level discount).
  - ``Cap Hit``: F&F dropped from a line by a PN-129 cap, plus one invoice-level
    row with the caps summary when a cap reduced F&F.
  - ``Excluded``: line out of the program scope or explicitly excluded.
  - ``Eligibility Removed``: an F&F stamp was stripped because the customer is
    no longer eligible.
  - ``Order Comparison``: F&F vs an order-level coupon / promotion / manual
    header discount (winner, amounts, outcome).

  Drafts are never logged (they churn on every save); only what was really
  sold. Ineligible customers with nothing stripped produce no rows.
* **Returns** (``is_return`` submit) — ``Discount Returned`` per F&F row with
  the negative amount, ``return_against`` and the original invoice's POS
  Profile; a zero-amount ``Discount Returned`` warning per row that may have
  had F&F but has no usable original row (so nothing was credited).
* **Cancel** — the invoice's earlier rows are flagged ``is_cancelled`` and a
  ``Discount Reversed`` row (negated amount) is added per F&F row, so the log
  stays append-only and totals per customer still reconcile.
* **Customer** ``on_update`` — ``Eligibility Applied`` / ``Eligibility Removed``
  when a change to the F&F flag, customer group or disabled state changes
  membership under the current Settings.
* **Friends and Family Settings** ``on_update`` — ``Settings Changed`` with a
  field-by-field diff (tables summarised as added / removed rows). The full
  document history is also kept by Frappe's Version (``track_changes``).

Consolidated Sales Invoices (merged POS Invoices) are skipped so nothing is
logged twice. Logging never blocks a sale: errors go to the Error Log.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, now_datetime, nowdate

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_ledger
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	FIELD_FNF_PERCENTAGE,
)

LOG_DOCTYPE = "Friends and Family Log"

LOG_APPLIED = "Discount Applied"
LOG_COMPARISON_LOST = "Comparison Lost"
LOG_EXCLUDED = "Excluded"
LOG_CAP_HIT = "Cap Hit"
LOG_ORDER_COMPARISON = "Order Comparison"
LOG_ELIGIBILITY_APPLIED = "Eligibility Applied"
LOG_ELIGIBILITY_REMOVED = "Eligibility Removed"
LOG_REVERSED = "Discount Reversed"
LOG_RETURNED = "Discount Returned"
LOG_SETTINGS_CHANGED = "Settings Changed"

FNF = "friends_and_family"

# Engine line reasons (friends_and_family_apply.REASON_*) → log type.
_REASON_LOG_TYPES = {
	"other_discount_better": LOG_COMPARISON_LOST,
	"order_discount_better": LOG_COMPARISON_LOST,
	"cap_reached": LOG_CAP_HIT,
	"excluded": LOG_EXCLUDED,
	"out_of_scope": LOG_EXCLUDED,
	"stripped": LOG_ELIGIBILITY_REMOVED,
}


def _json(value) -> str | None:
	if value in (None, {}, []):
		return None
	return json.dumps(value, ensure_ascii=False, default=str, indent=1)


def write_log(log_type: str, **values):
	"""Insert one log row (system write — cashiers have no permission on the DocType)."""
	details = values.pop("details", None)
	doc = frappe.get_doc(
		{
			"doctype": LOG_DOCTYPE,
			"log_type": log_type,
			"posting_date": values.pop("posting_date", None) or nowdate(),
			"timestamp": now_datetime(),
			"user": frappe.session.user,
			"details": _json(details) if not isinstance(details, str) else details,
			**values,
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_links = True
	doc.insert()
	return doc


def _safe(fn):
	"""Audit logging must never block a sale or an admin save."""

	def wrapper(doc, method=None):
		if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
			return None
		try:
			return fn(doc, method)
		except Exception:
			frappe.log_error(title=f"Friends and Family Log: {fn.__name__}")
			if frappe.flags.in_test:
				raise
			return None

	wrapper.__name__ = fn.__name__
	wrapper.__doc__ = fn.__doc__
	return wrapper


# ---- invoices -------------------------------------------------------------


def _invoice_context(doc) -> dict:
	return {
		"customer": doc.get("customer"),
		"reference_doctype": doc.get("doctype"),
		"reference_name": doc.get("name"),
		"posting_date": doc.get("posting_date"),
		"pos_profile": doc.get("pos_profile"),
		"company": doc.get("company"),
	}


def _row_context(row) -> dict:
	return {
		"item_code": row.get("item_code"),
		"item_name": row.get("item_name"),
		"invoice_row": row.get("name"),
		"qty": flt(row.get("qty")),
		"price_list_rate": flt(row.get("price_list_rate")),
	}


def _is_fnf_row(row) -> bool:
	return cstr(row.get(FIELD_DISCOUNT_SOURCE)) == FNF or bool(flt(row.get(FIELD_FNF_AMOUNT)))


def _row_fnf_amount(row, line: dict | None = None) -> float:
	stored = flt(row.get(FIELD_FNF_AMOUNT))
	if stored:
		return bd.money(stored)
	return bd.money(flt((line or {}).get("discount_amount")))


def _line_entry(doc, row, line: dict, payload: dict) -> tuple[str, dict] | None:
	"""(log type, values) for one engine line decision, or ``None`` when not worth logging."""
	decision = line.get("best_discount") or {}
	if line.get("applies"):
		log_type = LOG_APPLIED
	else:
		log_type = _REASON_LOG_TYPES.get(cstr(line.get("reason")))
		if not log_type:
			return None

	order = (payload.get("best_discount") or {}).get("order") or {}
	if line.get("applies"):
		source = FNF
	elif line.get("reason") == "order_discount_better" and order:
		source = order.get("winner")
	elif log_type == LOG_EXCLUDED:
		source = bd.classify_line_source(row)
	else:
		source = decision.get("winner") or line.get("competing_source")

	values = {
		**_invoice_context(doc),
		**_row_context(row),
		"discount_source": source,
		"discount_amount": _row_fnf_amount(row, line) if line.get("applies") else 0.0,
		"discount_percentage": flt(row.get("discount_percentage")) if line.get("applies") else 0.0,
		"fnf_uncapped_amount": flt(line.get("fnf_uncapped_amount")),
		"capped": cint(bool(line.get("capped"))),
		"cap_hit": (payload.get("caps") or {}).get("cap_hit") if line.get("capped") or log_type == LOG_CAP_HIT else None,
		"comparison_outcome": (
			order.get("outcome") if line.get("reason") == "order_discount_better" and order else decision.get("outcome")
		),
		"competing_source": line.get("competing_source"),
		"competing_amount": flt(line.get("competing_amount")),
		"reason": line.get("reason"),
		"message": decision.get("message_en") or payload.get("message_en") or payload.get("message"),
		"details": {
			"eligibility": payload.get("reason"),
			"program_percentage": payload.get("discount_percentage"),
			"decision": decision or None,
		},
	}
	return log_type, values


def _return_warning_message(reason: str, against: str | None) -> str:
	if reason == ff_ledger.RETURN_ORIGINAL_MISSING:
		return _("Return row's original row was not found on {0}: no Friends & Family usage credited.").format(against)
	if reason == ff_ledger.RETURN_ORIGINAL_QTY_ZERO:
		return _("Return row's original row on {0} has no quantity: no Friends & Family usage credited.").format(against)
	if reason == ff_ledger.RETURN_ORIGINAL_IS_RETURN:
		return _("Return row links a row of {0}, which is itself a return: no Friends & Family usage credited.").format(
			against
		)
	if reason == ff_ledger.RETURN_ORIGINAL_CONSOLIDATED:
		return _(
			"Return row links a row of {0}, a consolidated invoice whose POS Invoices were recorded already: "
			"no Friends & Family usage credited."
		).format(against)
	return _("Return row has no link to its original invoice row: no Friends & Family usage credited.")


def _log_return(doc, items) -> None:
	"""``Discount Returned`` per F&F return row, plus a zero-amount warning per row that got no credit.

	Rows carry the ORIGINAL invoice's POS Profile: the credit belongs to that sale.
	"""
	against = doc.get("return_against")
	context = _invoice_context(doc)
	if against and doc.get("doctype"):
		context["pos_profile"] = frappe.db.get_value(doc.get("doctype"), against, "pos_profile") or context["pos_profile"]
	for row in items:
		if not _is_fnf_row(row):
			continue
		write_log(
			LOG_RETURNED,
			**context,
			**_row_context(row),
			discount_source=FNF,
			discount_amount=_row_fnf_amount(row),
			discount_percentage=flt(row.get(FIELD_FNF_PERCENTAGE)),
			return_against=against,
			reason="return",
			message=_("Friends & Family discount returned against {0}").format(against),
		)
	flags = getattr(doc, "flags", None)
	by_name = {row.get("name"): row for row in items}
	for warning in (flags.get("ff_return_warnings") if flags is not None else None) or []:
		row = by_name.get(warning.get("invoice_row")) or frappe._dict(warning)
		write_log(
			LOG_RETURNED,
			**context,
			**_row_context(row),
			discount_source=FNF,
			discount_amount=0.0,
			return_against=against,
			reason=warning.get("reason"),
			message=_return_warning_message(warning.get("reason"), against),
			details=warning,
		)


@_safe
def log_invoice_submit(doc, method=None):
	"""Sales Invoice / POS Invoice ``on_submit``."""
	if cint(doc.get("is_consolidated")):
		return
	items = doc.get("items") or []
	if cint(doc.get("is_return")):
		_log_return(doc, items)
		return

	flags = getattr(doc, "flags", None)
	payload = flags.get("friends_and_family") if flags is not None else None
	if not payload:
		# The validate hook did not run (e.g. non-POS invoice); log what the rows carry.
		for row in items:
			if _is_fnf_row(row):
				write_log(
					LOG_APPLIED,
					**_invoice_context(doc),
					**_row_context(row),
					discount_source=FNF,
					discount_amount=_row_fnf_amount(row),
					discount_percentage=flt(row.get("discount_percentage")),
					reason="applied",
				)
		return

	if payload.get("reason") == "profile_not_allowed":
		# POS Profile has "Allow F&F" off: F&F is disabled there — nothing to log.
		return

	lines = payload.get("items") or []
	if not payload.get("eligible") and not any(ln.get("reason") == "stripped" for ln in lines):
		return

	for row, line in zip(items, lines, strict=False):
		entry = _line_entry(doc, row, line, payload)
		if entry:
			write_log(entry[0], **entry[1])

	caps = payload.get("caps") or {}
	if caps.get("capped"):
		write_log(
			LOG_CAP_HIT,
			**_invoice_context(doc),
			discount_source=FNF,
			discount_amount=flt(caps.get("fnf_applied_total")),
			fnf_uncapped_amount=flt(caps.get("fnf_uncapped_total")),
			capped=1,
			cap_hit=caps.get("cap_hit"),
			reason="cap_reached",
			message=caps.get("message_en") or caps.get("message"),
			details=caps,
		)

	order = (payload.get("best_discount") or {}).get("order")
	if order:
		candidates = {c.get("source"): c for c in order.get("candidates") or []}
		fnf_c = candidates.pop(FNF, None) or {}
		other = next(iter(candidates.values()), {})
		write_log(
			LOG_ORDER_COMPARISON,
			**_invoice_context(doc),
			discount_source=order.get("winner"),
			discount_amount=flt(fnf_c.get("amount")) if order.get("winner") == FNF else 0.0,
			comparison_outcome=order.get("outcome"),
			competing_source=other.get("source"),
			competing_amount=flt(other.get("amount")),
			reason="order_comparison",
			message=order.get("message_en"),
			details=order,
		)


@_safe
def log_invoice_cancel(doc, method=None):
	"""Sales Invoice / POS Invoice ``on_cancel`` — flag earlier rows, append reversals."""
	if cint(doc.get("is_consolidated")):
		return
	frappe.db.sql(
		f"""update `tab{LOG_DOCTYPE}` set is_cancelled = 1
		where reference_doctype = %s and reference_name = %s and is_cancelled = 0""",
		(doc.get("doctype"), doc.get("name")),
	)
	for row in doc.get("items") or []:
		if not _is_fnf_row(row):
			continue
		write_log(
			LOG_REVERSED,
			**_invoice_context(doc),
			**_row_context(row),
			discount_source=FNF,
			discount_amount=-_row_fnf_amount(row),
			return_against=doc.get("return_against"),
			reason="cancelled",
			is_cancelled=1,
			message=_("Invoice {0} cancelled — Friends & Family discount reversed").format(doc.get("name")),
		)


# ---- customer eligibility -------------------------------------------------


def _email_eligible_snapshot(doc) -> tuple[bool, str | None]:
	"""Eligible under current Settings from email domain + verification (ignores program dates)."""
	from posnext_promotions.promotions import friends_and_family_eligibility as elig
	from posnext_promotions.promotions.friends_and_family import get_settings

	if cint(doc.get("disabled")):
		return False, elig.REASON_CUSTOMER_DISABLED
	settings = get_settings()
	failure, _domain, _domain_ok = elig.check_customer_email(doc, settings)
	if failure:
		return False, failure
	return True, elig.REASON_EMAIL_VERIFIED


@_safe
def log_customer_eligibility_change(doc, method=None):
	"""Customer ``on_update`` — log when email-based F&F eligibility flips."""
	from posnext_promotions.promotions.friends_and_family_eligibility import (
		VERIFIED_EMAIL_FIELD,
		VERIFIED_FLAG_FIELD,
		has_verification_fields,
	)

	before = doc.get_doc_before_save()
	watched = ["email_id", "disabled"]
	if has_verification_fields():
		watched += [VERIFIED_FLAG_FIELD, VERIFIED_EMAIL_FIELD]
	old = {f: (before.get(f) if before else None) for f in watched}
	new = {f: doc.get(f) for f in watched}
	if before and all(cstr(old[f]) == cstr(new[f]) for f in watched):
		return

	was, was_reason = _email_eligible_snapshot(before) if before else (False, None)
	now, now_reason = _email_eligible_snapshot(doc)
	if was == now:
		return

	changes = {f: [old[f], new[f]] for f in watched if cstr(old[f]) != cstr(new[f])}
	write_log(
		LOG_ELIGIBILITY_APPLIED if now else LOG_ELIGIBILITY_REMOVED,
		customer=doc.name,
		reference_doctype="Customer",
		reference_name=doc.name,
		reason=now_reason if now else was_reason,
		message=(
			_("Customer {0} is now eligible for Friends & Family ({1})").format(doc.name, now_reason)
			if now
			else _("Customer {0} is no longer eligible for Friends & Family ({1})").format(doc.name, now_reason)
		),
		details={"changes": changes, "before": was_reason, "after": now_reason},
	)


# ---- settings -------------------------------------------------------------

_LAYOUT_TYPES = ("Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading")


def _table_rows(doc, field) -> list[str]:
	meta = frappe.get_meta(field.options)
	keys = [f.fieldname for f in meta.fields if f.fieldtype not in _LAYOUT_TYPES]
	rows = []
	for row in doc.get(field.fieldname) or []:
		values = [cstr(row.get(k)) for k in keys if row.get(k) not in (None, "")]
		rows.append(" / ".join(values))
	return rows


def settings_diff(before, after) -> dict:
	"""``{fieldname: {"label", "old", "new"} | {"label", "added", "removed"}}``."""
	diff = {}
	for field in after.meta.fields:
		if field.fieldtype in _LAYOUT_TYPES:
			continue
		if field.fieldtype in ("Table", "Table MultiSelect"):
			old = _table_rows(before, field) if before else []
			new = _table_rows(after, field)
			added = [r for r in new if r not in old]
			removed = [r for r in old if r not in new]
			if added or removed:
				diff[field.fieldname] = {"label": field.label, "added": added, "removed": removed}
			continue
		old = before.get(field.fieldname) if before else None
		new = after.get(field.fieldname)
		if field.fieldtype in ("Check", "Int"):
			old, new = cint(old), cint(new)
		elif field.fieldtype in ("Float", "Currency", "Percent"):
			old, new = flt(old), flt(new)
		else:
			old, new = cstr(old) or None, cstr(new) or None
		if old != new:
			diff[field.fieldname] = {"label": field.label, "old": old, "new": new}
	return diff


def _describe(diff: dict) -> str:
	parts = []
	for change in diff.values():
		if "added" in change:
			bits = []
			if change["added"]:
				bits.append("+" + ", +".join(change["added"]))
			if change["removed"]:
				bits.append("-" + ", -".join(change["removed"]))
			parts.append(f"{change['label']}: {'; '.join(bits)}")
		else:
			parts.append(f"{change['label']}: {change['old']} → {change['new']}")
	return "\n".join(parts)


@_safe
def log_settings_change(doc, method=None):
	"""Friends and Family Settings ``on_update`` — one ``Settings Changed`` row with the diff."""
	diff = settings_diff(doc.get_doc_before_save(), doc)
	if not diff:
		return
	write_log(
		LOG_SETTINGS_CHANGED,
		reference_doctype=doc.doctype,
		reference_name=doc.name,
		reason="settings_changed",
		message=_describe(diff),
		details=diff,
	)
