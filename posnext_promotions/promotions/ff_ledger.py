# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family usage ledger.

One **Friends and Family Usage** row per invoice line that received F&F,
written on submit and flagged ``is_cancelled`` on cancel: the audit trail.
One **Friends and Family Usage Total** row per (customer, company, period): the
running sum of those rows' ``base_amount`` (that company's currency) that are
not cancelled, and the row submits lock. Usage is shared across programs.
Caps compare per company: companies may keep different currencies, so their
amounts are never added together.

Concurrency
-----------
Validate prices the invoice without a lock: fast, and possibly stale. Submit
is the authority. ``on_submit`` upserts the counter row and reads it ``FOR
UPDATE`` by its primary key, which takes a record lock only, never a gap lock,
so submits for different customers never wait on or deadlock with each other.
It then checks the caps, inserts its ledger rows and adds them to the counter,
all in the invoice's transaction. Two submits for one customer and period run
one after the other. A locking read sees the latest committed value; a plain
read would return the snapshot this transaction took during validate (MariaDB
REPEATABLE READ).

Amounts come from the persisted row after ERPNext recalculated it:
``(price_list_rate - rate) x qty`` on lines tagged F&F. Nothing is taken from
a client-sent discount field.

Returns credit the period of their original invoice, and only from the
original row (:func:`return_credits`): its recorded F&F amount, pro rata to
the quantity returned. Nothing on the return row itself is trusted. Consolidated Sales
Invoices are skipped (their POS Invoices were recorded), and so are Sales
Invoices that are not POS (``is_pos = 0``).
"""

from __future__ import annotations

import calendar
import datetime
import hashlib

import frappe
from frappe import _
from frappe.query_builder.functions import Coalesce, IfNull, Sum
from frappe.utils import cint, cstr, flt, getdate, now_datetime
from pypika.terms import ExistsCriterion, ValueWrapper, Values

from posnext_promotions.promotions import ff_config
from posnext_promotions.promotions import ff_constants as const
from posnext_promotions.promotions import ff_money, ff_paid
from posnext_promotions.promotions.friends_and_family_caps import (
	CAP_MONTHLY,
	CAP_ORDER,
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	FIELD_FNF_PERCENTAGE,
	FIELD_FNF_PROGRAM,
	INVOICE_DOCTYPES,
	get_monthly_usage,
)
from posnext_promotions.promotions.friends_and_family import forget_request_cache

LEDGER_DOCTYPE = "Friends and Family Usage"
TOTAL_DOCTYPE = "Friends and Family Usage Total"
UNIQUE_KEY = "ff_usage_line"
UNIQUE_KEY_FIELDS = ("invoice_doctype", "invoice", "invoice_item")
PERIOD_INDEX = "customer_period_key_index"
PERIOD_ONLY_INDEX = "period_key_index"
TOTAL_UNIQUE_KEY = "ff_usage_total_key"
TOTAL_UNIQUE_KEY_FIELDS = ("customer", "company", "period_key")
FNF_SOURCE = "friends_and_family"
REASON_BREACH = "cap_breach_at_submit"
# Return row -> original row link, per invoice doctype.
RETURN_LINK_FIELDS = {"Sales Invoice": "sales_invoice_item", "POS Invoice": "pos_invoice_item"}
# Why a return row got no credit although it may have had F&F.
RETURN_NO_LINK = "return_without_original_row"
RETURN_ORIGINAL_MISSING = "return_original_row_missing"
RETURN_ORIGINAL_QTY_ZERO = "return_original_qty_zero"
RETURN_ORIGINAL_IS_RETURN = "return_original_is_return"
RETURN_ORIGINAL_CONSOLIDATED = "return_original_consolidated"
ORIGINAL_ROW_INDEX = "original_row_index"

def _ledger():
	return frappe.qb.DocType(LEDGER_DOCTYPE)


def _totals():
	return frappe.qb.DocType(TOTAL_DOCTYPE)


# ---- schema -----------------------------------------------------------------


def ensure_ledger_unique_key() -> None:
	"""Add the ledger's ``ff_usage_line`` unique key, (customer, period_key), period_key and
	``original_row_index`` indexes, and the counter's unique key, when missing. Idempotent.

	Checks this site's schema with ``SHOW INDEX``. ``frappe.db.add_unique`` checks
	``information_schema`` by table name only, so another site's database on the
	same server could make it skip. DDL commits the open transaction.
	"""
	# Raw SQL here only: frappe.qb cannot express DDL or SHOW INDEX.
	if frappe.db.table_exists(LEDGER_DOCTYPE, cached=False):
		table = f"tab{LEDGER_DOCTYPE}"
		if not frappe.db.has_index(table, UNIQUE_KEY):
			cols = ", ".join(f"`{f}`" for f in UNIQUE_KEY_FIELDS)
			frappe.db.sql_ddl(f"alter table `{table}` add unique index `{UNIQUE_KEY}` ({cols})")
		if not frappe.db.has_index(table, PERIOD_INDEX):
			frappe.db.sql_ddl(f"alter table `{table}` add index `{PERIOD_INDEX}` (`customer`, `period_key`)")
		# period_key: the rebuild's locking SUM per period locks only that period's rows.
		# original_row: the return path forces this index by name.
		for column in ("period_key", "original_row"):
			_ensure_search_index(table, column)
	if frappe.db.table_exists(TOTAL_DOCTYPE, cached=False):
		table = f"tab{TOTAL_DOCTYPE}"
		# SHOW INDEX lists a key's columns in order; Column_name is the 5th field.
		cols = [r[4] for r in frappe.db.sql(f"show index from `{table}` where Key_name = %s", TOTAL_UNIQUE_KEY)]
		if cols and list(cols) != list(TOTAL_UNIQUE_KEY_FIELDS):
			# Pre-company key from an earlier dev build: replace it.
			frappe.db.sql_ddl(f"alter table `{table}` drop index `{TOTAL_UNIQUE_KEY}`")
			cols = []
		if not cols:
			fields = ", ".join(f"`{f}`" for f in TOTAL_UNIQUE_KEY_FIELDS)
			frappe.db.sql_ddl(f"alter table `{table}` add unique index `{TOTAL_UNIQUE_KEY}` ({fields})")


def _ensure_search_index(table: str, column: str) -> None:
	"""A ``search_index`` column's index under one name on every site: ``<column>_index``.

	Frappe names a ``search_index`` index ``<column>`` when CREATE TABLE makes it
	(fresh install) and ``<column>_index`` when ALTER adds it later. The CREATE name
	is renamed, so one index exists under one name, and Frappe's own sync (which
	checks indexes by column) neither adds nor drops one. The DocField must keep
	``search_index``, or each schema sync drops the index. RENAME INDEX needs
	MariaDB 10.5.2+.
	"""
	name = f"{column}_index"
	if not frappe.db.has_column(table.removeprefix("tab"), column) or frappe.db.has_index(table, name):
		return
	if frappe.db.has_index(table, column):
		frappe.db.sql_ddl(f"alter table `{table}` rename index `{column}` to `{name}`")
	else:
		frappe.db.sql_ddl(f"alter table `{table}` add index `{name}` (`{column}`)")


# ---- periods ----------------------------------------------------------------


def period_key(on_date) -> str:
	"""Cap period of a date. Month only in Phase 1: ``YYYY-MM``."""
	return getdate(on_date).strftime("%Y-%m")


def period_bounds(key: str) -> tuple[datetime.date, datetime.date]:
	year, month = (int(p) for p in cstr(key).split("-"))
	return datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1])


# ---- usage ------------------------------------------------------------------


def total_name(customer: str, company: str, period_key: str) -> str:
	"""Deterministic primary key of the counter row, so locking it is an exact record lock."""
	digest = hashlib.sha1(f"{customer}\n{company}\n{period_key}".encode()).hexdigest()
	return f"{period_key}-{digest}"


def _upsert_total(name: str, customer: str, company: str, period_key: str, base_amount: float, overwrite: bool):
	"""Insert the counter row, or on its primary key either leave it (lock only) or set its value.

	``ON DUPLICATE KEY UPDATE`` on the primary key takes an exclusive record lock on
	an existing row, so two submits never share an S lock and then race to upgrade it.
	"""
	T = _totals()
	now = now_datetime()
	user = frappe.session.user
	query = (
		frappe.qb.into(T)
		.columns(
			T.name, T.creation, T.modified, T.owner, T.modified_by, T.docstatus,
			T.customer, T.company, T.period_key, T.base_amount,
		)
		.insert(name, now, now, user, user, 0, customer, company, period_key, flt(base_amount))
	)
	if overwrite:
		query = query.on_duplicate_key_update(T.base_amount, Values(T.base_amount)).on_duplicate_key_update(
			T.modified, Values(T.modified)
		)
	else:
		query = query.on_duplicate_key_update(T.name, T.name)
	query.run()
	_forget_usage()


def _forget_usage() -> None:
	"""A counter changed: this request's cached monthly usage is stale."""
	forget_request_cache(get_monthly_usage)


def _busy(customer):
	frappe.throw(
		_("Friends & Family usage is busy for customer {0}, please retry.").format(customer),
		title=_("Please retry"),
	)


def lock_usage(customer: str, company: str, period_key: str) -> float:
	"""Lock the customer's counter for ``company`` and ``period_key``; return its value (company currency).

	Upserts the row and reads it ``FOR UPDATE`` by primary key: record locks only,
	no gap locks. Held until the transaction ends.

	No retry: a lock-wait timeout may roll back more than the statement
	(``innodb_rollback_on_timeout``) and the invoice already holds its GL / stock
	locks, so the submit fails fast and the cashier retries. A deadlock
	propagates as is.
	"""
	if not company:
		frappe.throw(_("Friends & Family usage needs the invoice's company."))
	name = total_name(customer, company, period_key)
	T = _totals()
	try:
		_upsert_total(name, customer, company, period_key, 0.0, overwrite=False)
		value = frappe.qb.from_(T).select(T.base_amount).where(T.name == name).for_update().run()[0][0]
	except frappe.QueryTimeoutError:
		_busy(customer)
	return flt(value)


def _add_to_total(customer: str, company: str, period_key: str, delta: float) -> None:
	"""Add ``delta`` to a counter this transaction already holds (see :func:`lock_usage`).

	Stores the raw sum, which can be negative (a return of an invoice the ledger
	never recorded), so the counter always equals the ledger sum whatever the
	order of events. Reads clamp at 0 (:func:`get_usage`, :func:`on_submit`).
	"""
	if not delta:
		return
	T = _totals()
	(
		frappe.qb.update(T)
		.set(T.base_amount, T.base_amount + flt(delta))
		.set(T.modified, now_datetime())
		.where(T.name == total_name(customer, company, period_key))
	).run()
	_forget_usage()


def _recorded_for_invoice(doctype: str, invoice: str) -> float:
	"""This invoice's own counted rows (plain read: only this invoice's submit writes them)."""
	if not invoice:
		return 0.0
	L = _ledger()
	total = (
		frappe.qb.from_(L)
		.select(Sum(L.base_amount))
		.where((L.invoice_doctype == doctype) & (L.invoice == invoice) & (L.is_cancelled == 0))
	).run()[0][0]
	return flt(total)


def get_usage(customer, company, period_key, exclude_invoice: str | None = None) -> float:
	"""F&F already granted to ``customer`` by ``company`` in ``period_key`` (its currency), net of returns.

	A plain read of the counter (validate: fast, may be stale; submit re-checks
	under :func:`lock_usage`). ``exclude_invoice`` drops that invoice's own rows.
	"""
	customer = cstr(customer).strip()
	if not customer or not period_key:
		return 0.0
	if not company:
		frappe.throw(_("Friends & Family usage needs the invoice's company."))
	used = flt(frappe.db.get_value(TOTAL_DOCTYPE, total_name(customer, company, period_key), "base_amount"))
	if exclude_invoice:
		L = _ledger()
		used -= flt(
			(
				frappe.qb.from_(L)
				.select(Sum(L.base_amount))
				.where(
					(L.invoice == exclude_invoice)
					& (L.customer == customer)
					& (L.company == company)
					& (L.period_key == period_key)
					& (L.is_cancelled == 0)
				)
			).run()[0][0]
		)
	return max(0.0, used)  # the counter may be negative (see _add_to_total); usage never is


# ---- rows -------------------------------------------------------------------


def is_tracked(doc) -> bool:
	"""POS Invoices, and Sales Invoices with ``is_pos``. Consolidated invoices never."""
	if cint(doc.get("is_consolidated")):
		return False
	doctype = cstr(doc.get("doctype"))
	if doctype == "POS Invoice":
		return True
	return doctype == "Sales Invoice" and bool(cint(doc.get("is_pos")))


def line_fnf_amount(row) -> float:
	"""Saving the row gives, from its persisted prices (negative on a return row)."""
	if cstr(row.get(FIELD_DISCOUNT_SOURCE)) != FNF_SOURCE:
		return 0.0
	price_list_rate = flt(row.get("price_list_rate"))
	rate = flt(row.get("rate"))
	if price_list_rate <= rate:
		return 0.0
	return (price_list_rate - rate) * flt(row.get("qty"))


# ---- returns ----------------------------------------------------------------


def _original_rows(doc, links: set) -> dict:
	"""Original rows by name: on ``return_against``, submitted, same customer.

	``is_return`` / ``is_consolidated`` are returned for the caller to reject: a
	credit note's rows, and a consolidated Sales Invoice's rows (they copy the F&F
	fields of POS Invoices that were recorded already), are never an original.
	"""
	doctype = cstr(doc.get("doctype"))
	if not links or not doc.get("return_against"):
		return {}
	Item = frappe.qb.DocType(f"{doctype} Item")
	Inv = frappe.qb.DocType(doctype)
	# POS Invoice has no is_consolidated column.
	consolidated = Inv.is_consolidated if doctype == "Sales Invoice" else ValueWrapper(0)
	rows = (
		frappe.qb.from_(Item)
		.join(Inv)
		.on(Inv.name == Item.parent)
		.select(
			IfNull(Inv.is_return, 0).as_("is_return"),
			IfNull(consolidated, 0).as_("is_consolidated"),
			Item.name,
			Item.item_code,
			Item.qty,
			Item[FIELD_DISCOUNT_SOURCE].as_("source"),
			Item[FIELD_FNF_AMOUNT].as_("fnf_amount"),
			Item[FIELD_FNF_PERCENTAGE].as_("fnf_percentage"),
			Item[FIELD_FNF_PROGRAM].as_("fnf_program"),
		)
		.where(
			Item.name.isin(sorted(links))
			& (Item.parent == doc.get("return_against"))
			& (Item.parenttype == doctype)
			& (Inv.docstatus == 1)
			& (Inv.customer == doc.get("customer"))
		)
	).run(as_dict=True)
	return {row.name: row for row in rows}


def _prior_credits(doc, originals: set, for_update: bool = False) -> dict:
	"""``{original row: [qty returned, credit given]}`` by other returns, from the ledger (Ruling 38).

	Return ledger rows carry ``original_row`` and ``qty`` and are written only under
	the original period's counter lock (:func:`_on_submit_return`), so a locking
	read taken after that lock sees every committed return of the row.
	``for_update`` makes it that locking read; it is bound to ``original_row_index``
	(forced: on a small table the optimiser may otherwise scan, and a locking scan
	would lock every ledger row).
	"""
	if not originals:
		return {}
	L = _ledger()
	query = (
		frappe.qb.from_(L)
		.select(L.original_row, L.qty, L.amount)
		.where(
			L.original_row.isin(sorted(originals))
			& (L.invoice_doctype == doc.get("doctype"))
			& (L.is_cancelled == 0)
		)
	)
	if doc.get("name"):
		query = query.where(L.invoice != doc.get("name"))
	if for_update:
		query = query.force_index(ORIGINAL_ROW_INDEX).for_update()
	prior: dict = {}
	for row in query.run(as_dict=True):
		done = prior.setdefault(row.original_row, [0.0, 0.0])
		done[0] += abs(flt(row.qty))
		done[1] -= flt(row.amount)
	return {link: [qty, max(0.0, credit)] for link, (qty, credit) in prior.items()}


def return_credits(doc, for_update: bool = False) -> list[frappe._dict]:
	"""F&F usage each return row gives back, derived from its ORIGINAL row only.

	Per row: ``credit`` (<= 0), ``original`` (the original row when it carried
	F&F, else ``None``) and ``warning`` (why no original row could be used).
	Credit = the original row's recorded F&F amount x cumulative returned qty /
	original qty, minus what earlier returns of that row already credited, so
	partial returns add up to the original amount exactly and never beyond it.
	A missing link, an original row that is not on ``return_against`` (or not
	submitted, other customer or item), an original that is itself a return or a
	consolidated invoice, or an original qty of 0 credits nothing.

	Earlier returns are read from the ledger (:func:`_prior_credits`).
	``for_update``: with a locking read (submit only, after :func:`lock_usage` on
	the original period's counter and this return's own rows are inserted).
	"""
	doctype = cstr(doc.get("doctype"))
	link_field = RETURN_LINK_FIELDS.get(doctype)
	rows = doc.get("items") or []
	links = {cstr(r.get(link_field)) for r in rows if link_field and r.get(link_field)}
	originals = _original_rows(doc, links)
	prior = _prior_credits(doc, set(originals), for_update=for_update)
	precision = ff_money.precision_for(ff_money.document_currency(doc))
	results = []
	for row in rows:
		result = frappe._dict(row=row, credit=0.0, original=None, warning=None)
		results.append(result)
		link = cstr(row.get(link_field)) if link_field else ""
		original = originals.get(link)
		if not link:
			result.warning = RETURN_NO_LINK
			continue
		if original is None or cstr(original.item_code) != cstr(row.get("item_code")):
			result.warning = RETURN_ORIGINAL_MISSING
			continue
		# Before the qty check: a return's rows have negative qty.
		if cint(original.is_return):
			result.warning = RETURN_ORIGINAL_IS_RETURN
			continue
		if cint(original.is_consolidated):
			result.warning = RETURN_ORIGINAL_CONSOLIDATED
			continue
		original_qty = flt(original.qty)
		if original_qty <= 0:
			result.warning = RETURN_ORIGINAL_QTY_ZERO
			continue
		original_amount = flt(original.fnf_amount, precision)
		if cstr(original.source) != FNF_SOURCE or original_amount <= 0:
			continue
		done = prior.setdefault(link, [0.0, 0.0])
		returned = min(original_qty, done[0] + abs(flt(row.get("qty"))))
		target = min(original_amount, flt(original_amount * returned / original_qty, precision))
		credit = max(0.0, flt(target - done[1], precision))
		done[0] += abs(flt(row.get("qty")))
		done[1] += credit
		result.credit = -credit
		result.original = original
	return results


def _ledger_posting_date(doc):
	"""Posting date that picks the period: a return uses its original invoice's."""
	if cint(doc.get("is_return")) and doc.get("return_against"):
		original = frappe.db.get_value(doc.get("doctype"), doc.get("return_against"), "posting_date")
		if original:
			return original
	return doc.get("posting_date")


def ledger_rows(doc, credits: list | None = None) -> list[dict]:
	"""Ledger values for each F&F line of ``doc`` (empty when the invoice is not tracked).

	``credits``: :func:`return_credits` already computed for a return (under its lock).
	"""
	if not is_tracked(doc) or not doc.get("customer"):
		return []
	company = doc.get("company")
	company_currency = ff_money.company_currency(company)
	currency = ff_money.document_currency(doc)
	conversion_rate = flt(doc.get("conversion_rate")) or 1.0
	amount_precision = ff_money.precision_for(currency)
	base_precision = ff_money.precision_for(company_currency)
	period = period_key(_ledger_posting_date(doc))
	if cint(doc.get("is_return")):
		# Never from the return row's prices: a crafted price_list_rate would credit anything.
		# Every row with an F&F original is recorded, even at 0: its qty counts
		# towards the cumulative returned qty of later returns of that row.
		amounts = [
			(r.row, r.credit, r.original.name if r.original else None)
			for r in (credits if credits is not None else return_credits(doc))
		]
	else:
		amounts = [(row, line_fnf_amount(row), None) for row in doc.get("items") or []]
	rows = []
	for row, line_amount, original_row in amounts:
		amount = flt(line_amount, amount_precision)
		if not (amount or original_row) or not row.get("name"):
			continue
		rows.append(
			{
				"customer": doc.get("customer"),
				"company": company,
				# "program" stays blank in Phase 1 (one program); Phase 3 fills it.
				"invoice_doctype": doc.get("doctype"),
				"invoice": doc.get("name"),
				"invoice_item": row.get("name"),
				"original_row": original_row,
				"qty": flt(row.get("qty")),
				"posting_date": getdate(doc.get("posting_date")),
				"period_key": period,
				"currency": currency,
				"amount": amount,
				"base_amount": flt(amount * conversion_rate, base_precision),
				"is_return": cint(doc.get("is_return")),
				"is_cancelled": 0,
			}
		)
	return rows


def insert_row(values: dict) -> bool:
	"""Insert one ledger row. ``False`` when the line is already recorded (unique key)."""
	now = now_datetime()
	user = frappe.session.user
	record = {
		"name": frappe.generate_hash(length=12),
		"creation": now,
		"modified": now,
		"owner": user,
		"modified_by": user,
		"docstatus": 0,
		**values,
	}
	L = _ledger()
	try:
		frappe.qb.into(L).columns(*(L[c] for c in record)).insert(*record.values()).run()
	except Exception as e:
		if frappe.db.is_duplicate_entry(e) and not frappe.db.is_primary_key_violation(e):
			return False
		raise
	return True


def record_rows(rows: list[dict]) -> list[dict]:
	"""Insert ledger rows and add the counted ones to their counters. Returns the rows inserted."""
	inserted = [values for values in rows if insert_row(values)]
	deltas: dict[tuple, float] = {}
	for values in inserted:
		if not cint(values.get("is_cancelled")):
			key = (values["customer"], values["company"], values["period_key"])
			deltas[key] = deltas.get(key, 0.0) + flt(values["base_amount"])
	for (customer, company, period), delta in sorted(deltas.items()):
		lock_usage(customer, company, period)
		_add_to_total(customer, company, period, delta)
	return inserted


# ---- caps at submit ---------------------------------------------------------


def _over(value: float, cap: float, unit: float) -> bool:
	"""``value`` exceeds ``cap`` by more than one precision unit (per-line rounding)."""
	return flt(value) > flt(cap) + unit


def _check_caps(doc, rows, used: float) -> dict | None:
	"""The cap this invoice breaks at submit, or ``None``. ``used`` = locked counter, this invoice excluded."""
	from posnext_promotions.promotions.friends_and_family import get_active_program

	program = get_active_program(doc.get("posting_date"))
	if program is None or not program.has_caps:
		return None
	currency = ff_money.company_currency(doc.get("company"))
	total = ff_money.money(sum(flt(r["base_amount"]) for r in rows), currency)
	if total <= 0:
		return None
	unit = 10 ** -ff_money.precision_for(currency)
	per_order = flt(program.max_discount_per_order)
	if per_order > 0 and _over(total, per_order, unit):
		return {
			"cap_hit": CAP_ORDER,
			"cap": per_order,
			"used": 0.0,
			"invoice_total": total,
			"currency": currency,
			"message": _("Friends & Family discount {0} on this invoice exceeds the per-order cap of {1}.").format(
				ff_money.fmt(total, currency), ff_money.fmt(per_order, currency)
			),
		}
	monthly = flt(program.max_discount_per_customer_month)
	if monthly <= 0 or not _over(used + total, monthly, unit):
		return None
	period = rows[0]["period_key"]
	return {
		"cap_hit": CAP_MONTHLY,
		"cap": monthly,
		"used": ff_money.money(used, currency),
		"invoice_total": total,
		"period_key": period,
		"currency": currency,
		"message": _(
			"Friends & Family discount {0} on this invoice exceeds the customer's remaining monthly allowance: "
			"{1} of the {2} cap already used in {3}."
		).format(ff_money.fmt(total, currency), ff_money.fmt(used, currency), ff_money.fmt(monthly, currency), period),
	}


def _log_breach(doc, breach: dict) -> None:
	from posnext_promotions.promotions.friends_and_family_log import LOG_CAP_HIT, write_log

	write_log(
		LOG_CAP_HIT,
		customer=doc.get("customer"),
		reference_doctype=doc.get("doctype"),
		reference_name=doc.get("name"),
		posting_date=doc.get("posting_date"),
		pos_profile=doc.get("pos_profile"),
		company=doc.get("company"),
		discount_source=FNF_SOURCE,
		discount_amount=breach["invoice_total"],
		capped=1,
		cap_hit=breach["cap_hit"],
		reason=REASON_BREACH,
		message=breach["message"],
		details=breach,
	)


# ---- hooks ------------------------------------------------------------------


def _persist_return_stamps(doc, credits) -> None:
	"""The credit decided under the lock is the row's stamp (later returns sum these stamps)."""
	item_doctype = f"{doc.get('doctype')} Item"
	for result in credits:
		row = result.row
		if flt(row.get(FIELD_FNF_AMOUNT)) == flt(result.credit):
			continue
		if isinstance(row, dict):
			row[FIELD_FNF_AMOUNT] = flt(result.credit)
		else:
			row.set(FIELD_FNF_AMOUNT, flt(result.credit))
		if row.get("name"):
			frappe.db.set_value(item_doctype, row.get("name"), FIELD_FNF_AMOUNT, flt(result.credit), update_modified=False)


def _on_submit_return(doc) -> None:
	"""Record a return's credit; returns of one original row run one after the other (Rulings 36, 38).

	Validate stamped each row from a snapshot read, which may miss a return of the
	same original row committed since. Here, in this order:

	1. lock the original period's counter (every return of the row needs it:
	   same customer, company, period), so those returns serialise;
	2. insert this return's ledger rows with amount 0 (they carry
	   ``original_row`` and ``qty``);
	3. re-read earlier returns of those rows from the ledger with a locking read
	   (index-bound) and recompute the credit;
	4. update this return's own rows by their unique key and add the total to
	   the counter.

	Inserting before the locking read matters: the read takes gap locks on
	``original_row_index``; were the insert after it, two returns of different
	originals in one gap would each wait on the other's gap lock (deadlock).
	Return headers are never locked (Ruling 38).
	"""
	if not is_tracked(doc) or not doc.get("customer"):
		return
	plain = return_credits(doc)
	# A snapshot read can only over-state the credit; no F&F original means nothing to record.
	if not any(r.original is not None for r in plain):
		return
	customer, company = doc.get("customer"), doc.get("company")
	period = period_key(_ledger_posting_date(doc))
	lock_usage(customer, company, period)
	try:
		inserted = [values for values in ledger_rows(doc, credits=plain) if insert_row({**values, "amount": 0, "base_amount": 0})]
		if not inserted:
			return  # already recorded (the hook ran before for this invoice)
		credits = return_credits(doc, for_update=True)
	except frappe.QueryTimeoutError:
		_busy(customer)
	_persist_return_stamps(doc, credits)
	final = {values["invoice_item"]: values for values in ledger_rows(doc, credits=credits)}
	L = _ledger()
	delta = 0.0
	for values in inserted:
		row = final.get(values["invoice_item"])
		if not row or not row["amount"]:
			continue
		(
			frappe.qb.update(L)
			.set(L.amount, row["amount"])
			.set(L.base_amount, row["base_amount"])
			.where(
				(L.invoice_doctype == values["invoice_doctype"])
				& (L.invoice == values["invoice"])
				& (L.invoice_item == values["invoice_item"])
			)
		).run()
		delta += flt(row["base_amount"])
	if delta:
		_add_to_total(customer, company, period, delta)


@ff_money.in_document_currency
def on_submit(doc, method=None):
	"""Sales Invoice / POS Invoice ``on_submit``: lock the counter, check the caps, record usage."""
	if cint(doc.get("is_return")):
		_on_submit_return(doc)
		return
	rows = ledger_rows(doc)
	if not rows:
		return
	customer, company, period = rows[0]["customer"], rows[0]["company"], rows[0]["period_key"]
	used = lock_usage(customer, company, period) - _recorded_for_invoice(doc.get("doctype"), doc.get("name"))
	breach = _check_caps(doc, rows, max(0.0, used))  # clamp on read only
	block = const.CAP_BREACH_POLICIES[1]
	# An offline sale already happened (the customer has left): log, never block.
	if breach and ff_config.resolve().on_cap_breach_at_submit.value == block and not ff_paid.is_offline(doc):
		frappe.throw(breach["message"], title=_("Friends & Family cap exceeded"))
	record_rows(rows)
	if breach:
		_log_breach(doc, breach)


def on_cancel(doc, method=None):
	"""Sales Invoice / POS Invoice ``on_cancel``: the invoice's rows stop counting.

	Each row is flipped by primary key with ``is_cancelled = 0`` in the WHERE, and
	only rows whose flip happened (rowcount) are subtracted, so a second call
	subtracts nothing.
	"""
	if not doc.get("name"):
		return
	L = _ledger()
	rows = (
		frappe.qb.from_(L)
		.select(L.name, L.customer, L.company, L.period_key, L.base_amount)
		.where((L.invoice_doctype == doc.get("doctype")) & (L.invoice == doc.get("name")) & (L.is_cancelled == 0))
	).run(as_dict=True)
	if not rows:
		return
	# Same lock order as submit: counters first, then ledger rows.
	for key in sorted({(row.customer, row.company, row.period_key) for row in rows}):
		lock_usage(*key)
	deltas: dict[tuple, float] = {}
	now = now_datetime()
	for row in rows:
		(
			frappe.qb.update(L)
			.set(L.is_cancelled, 1)
			.set(L.modified, now)
			.where((L.name == row.name) & (L.is_cancelled == 0))
		).run()
		if frappe.db._cursor.rowcount:
			key = (row.customer, row.company, row.period_key)
			deltas[key] = deltas.get(key, 0.0) - flt(row.base_amount)
	for (customer, company, period), delta in sorted(deltas.items()):
		_add_to_total(customer, company, period, delta)


# ---- backfill ---------------------------------------------------------------


def _backfill_candidates(doctype: str, start, end, after: str, limit: int) -> list[str]:
	Inv = frappe.qb.DocType(doctype)
	Orig = frappe.qb.DocType(doctype).as_("o")
	Line = frappe.qb.DocType(f"{doctype} Item")
	has_fnf_line = ExistsCriterion(
		frappe.qb.from_(Line)
		.select(Line.name)
		.where((Line.parent == Inv.name) & (Line.parenttype == doctype) & (Line[FIELD_DISCOUNT_SOURCE] == FNF_SOURCE))
	)
	in_period = ((IfNull(Inv.is_return, 0) == 0) & Inv.posting_date[start:end]) | (
		(Inv.is_return == 1) & Coalesce(Orig.posting_date, Inv.posting_date)[start:end]
	)
	query = (
		frappe.qb.from_(Inv)
		.left_join(Orig)
		.on(Orig.name == Inv.return_against)
		.select(Inv.name)
		.where((Inv.docstatus == 1) & (Inv.name > after) & in_period & has_fnf_line)
		.orderby(Inv.name)
		.limit(limit)
	)
	if doctype != "POS Invoice":
		query = query.where(IfNull(Inv.is_pos, 0) == 1)
	if frappe.db.has_column(doctype, "is_consolidated"):
		query = query.where(IfNull(Inv.is_consolidated, 0) == 0)
	return query.run(pluck=True)


def _row_exists(values: dict) -> bool:
	return bool(frappe.db.exists(LEDGER_DOCTYPE, {f: values[f] for f in UNIQUE_KEY_FIELDS}))


def rebuild_totals(period_key: str) -> None:
	"""Recompute every counter of ``period_key`` from the ledger (idempotent: sets, never adds).

	Safe while tills submit: the period's counters are locked first (``FOR UPDATE``
	on the primary-key prefix ``"<period>-"`` of :func:`total_name`, the same lock
	order as submit), then the ledger is summed with a locking read, which sees
	committed rows rather than this transaction's snapshot. A submit either
	finishes before the rebuild takes its counter, or waits until it commits.
	"""
	T = _totals()
	L = _ledger()
	existing = (
		frappe.qb.from_(T).select(T.name).where(T.name.like(f"{period_key}-%")).for_update()
	).run(pluck=True)
	sums = (
		frappe.qb.from_(L)
		.select(L.customer, L.company, Sum(L.base_amount))
		.where((L.period_key == period_key) & (L.is_cancelled == 0))
		.groupby(L.customer, L.company)
		.for_update()
	).run()
	written = set()
	for customer, company, total in sums:
		name = total_name(customer, company, period_key)
		_upsert_total(name, customer, company, period_key, flt(total), overwrite=True)
		written.add(name)
	stale = [name for name in existing if name not in written]
	if stale:
		(
			frappe.qb.update(T).set(T.base_amount, 0).set(T.modified, now_datetime()).where(T.name.isin(stale))
		).run()
		_forget_usage()


def backfill_return_links() -> int:
	"""Fill ``original_row`` / ``qty`` on return ledger rows recorded before those fields (Ruling 38).

	Later returns of the same original row read their predecessors from these
	fields. Idempotent: only rows with no ``original_row`` yet. Returns the count.
	"""
	L = _ledger()
	rows = (
		frappe.qb.from_(L)
		.select(L.name, L.invoice_doctype, L.invoice_item)
		.where((L.is_return == 1) & (IfNull(L.original_row, "") == ""))
	).run(as_dict=True)
	count = 0
	for row in rows:
		link_field = RETURN_LINK_FIELDS.get(row.invoice_doctype)
		if not link_field:
			continue
		Item = frappe.qb.DocType(f"{row.invoice_doctype} Item")
		item = (
			frappe.qb.from_(Item).select(Item[link_field].as_("link"), Item.qty).where(Item.name == row.invoice_item)
		).run(as_dict=True)
		if not item:
			continue
		(
			frappe.qb.update(L)
			.set(L.original_row, item[0].link or None)
			.set(L.qty, flt(item[0].qty))
			.where(L.name == row.name)
		).run()
		count += 1
	return count


def backfill(period_key: str, batch_size: int = 500, dry_run: bool = False) -> dict:
	"""Record submitted F&F invoices of ``period_key`` that have no ledger rows yet, then
	rebuild that period's counters from the ledger.

	Pages by invoice name and commits after each batch. Idempotent: the unique
	key (ensured first) skips lines already recorded, and counters are
	recomputed, not added to. ``dry_run`` writes nothing and returns what it
	would write. Returns ``{"invoices": n, "rows": m}``: counts only. They are
	exact for returns too (a return row with an F&F original is written even at
	0); the credit amounts are not previewed, since a dry run cannot see earlier
	returns' credits it has not written.
	"""
	if not dry_run:
		ensure_ledger_unique_key()
	start, end = period_bounds(period_key)
	batch_size = max(1, cint(batch_size))
	counts = {"invoices": 0, "rows": 0}
	for doctype in INVOICE_DOCTYPES:
		if not frappe.db.table_exists(doctype) or not frappe.db.has_column(f"{doctype} Item", FIELD_DISCOUNT_SOURCE):
			continue
		after = ""
		while True:
			names = _backfill_candidates(doctype, start, end, after, batch_size)
			if not names:
				break
			for name in names:
				rows = ledger_rows(frappe.get_doc(doctype, name))
				if dry_run:
					written = sum(1 for values in rows if not _row_exists(values))
				else:
					written = sum(1 for values in rows if insert_row(values))
				if written:
					counts["invoices"] += 1
					counts["rows"] += written
			after = names[-1]
			if not dry_run:
				frappe.db.commit()
	if not dry_run:
		# Batch job: commit so the rebuild starts a fresh transaction (its reads lock anyway).
		frappe.db.commit()
		rebuild_totals(period_key)
		frappe.db.commit()
	return counts
