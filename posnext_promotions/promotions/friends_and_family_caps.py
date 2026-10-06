# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family discount caps (PN-129).

Two admin caps on **Friends and Family Settings** (money; 0 = no cap):

* ``max_discount_per_order`` — total F&F discount on one invoice.
* ``max_discount_per_customer_month`` — cumulative F&F discount one customer can
  receive in a calendar month.

The allowance for an order is ``min(per-order cap, monthly cap - used this
month)``.

Monthly usage
-------------
Every invoice line that receives F&F stores the money it saved in the custom
field ``posnext_fnf_discount_amount`` (line total) and
``posnext_discount_source = "friends_and_family"`` on Sales Invoice Item and
POS Invoice Item. Usage for a month is the sum of that amount over the
customer's **submitted** (``docstatus = 1``) Sales and POS Invoices posted in
that month. Cancelled invoices (``docstatus = 2``) and drafts never count.
Consolidated Sales Invoices (``is_consolidated = 1``) are skipped so a POS
Invoice merged into one is not counted twice.

Returns / credit notes: a return line copies the F&F source of the original
line; on validate we store the **negative** F&F saving for the returned qty.
The return is credited to the month of the **original** invoice
(``return_against``) — returning a September purchase in October does not
create extra October allowance; returning an October purchase in October
gives the allowance back.

Invoices saved before PN-129 carry no stored amount and do not count.

Allocation
----------
When the allowance is smaller than the uncapped F&F total, the allowance is
spread **proportionally** over the covered lines where F&F wins. Because a
smaller F&F can lose to a line's own promotion, the engine re-checks: lines
where the reduced F&F now loses (tie still → F&F) drop out and the allowance is
re-spread over the rest (a dropped line is not re-admitted, which keeps the
loop finite and deterministic). Line amounts are rounded **down** to the cent,
so the applied total never exceeds the cap.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate, nowdate

from posnext_promotions.promotions import best_discount as bd

FIELD_FNF_AMOUNT = "posnext_fnf_discount_amount"
FIELD_DISCOUNT_SOURCE = "posnext_discount_source"
# PN-133: the discount F&F replaced on the line (JSON), so a later save can
# re-run the comparison — and give the promotion back if F&F no longer wins.
FIELD_FNF_REPLACED = "posnext_fnf_replaced_discount"
INVOICE_ITEM_DOCTYPES = ("Sales Invoice Item", "POS Invoice Item")
INVOICE_DOCTYPES = ("Sales Invoice", "POS Invoice")

CAP_ORDER = "order"
CAP_MONTHLY = "monthly"

ITEM_FIELD_SPECS = [
	{
		"fieldname": FIELD_DISCOUNT_SOURCE,
		"module": "POSNext Promotions",
		"label": "Discount Source",
		"fieldtype": "Data",
		"insert_after": "discount_amount",
		"read_only": 1,
		"print_hide": 1,
		"report_hide": 0,
		"description": "Which promotion engine discounted this line (e.g. friends_and_family).",
	},
	{
		"fieldname": FIELD_FNF_AMOUNT,
		"module": "POSNext Promotions",
		"label": "Friends & Family Discount",
		"fieldtype": "Currency",
		"options": "currency",
		"insert_after": FIELD_DISCOUNT_SOURCE,
		"read_only": 1,
		"print_hide": 1,
		"description": "Friends & Family saving on this line (line total). Negative on returns. "
		"Drives the monthly per-customer cap.",
	},
	{
		"fieldname": FIELD_FNF_REPLACED,
		"module": "POSNext Promotions",
		"label": "Discount Replaced by Friends & Family",
		"fieldtype": "Small Text",
		"insert_after": FIELD_FNF_AMOUNT,
		"read_only": 1,
		"hidden": 1,
		"print_hide": 1,
		"report_hide": 1,
		"description": "Promotion / coupon / manual discount that Friends & Family replaced (JSON).",
	},
]


def ensure_invoice_item_fields() -> None:
	"""Create the F&F audit fields on Sales / POS Invoice Item (idempotent)."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	specs = {dt: ITEM_FIELD_SPECS for dt in INVOICE_ITEM_DOCTYPES if frappe.db.exists("DocType", dt)}
	if specs:
		create_custom_fields(specs, update=True)


def floor_cents(value) -> float:
	return math.floor(flt(value) * 100 + 1e-6) / 100.0


def ceil_cents(value) -> float:
	return math.ceil(flt(value) * 100 - 1e-6) / 100.0


def month_bounds(on_date=None) -> tuple:
	d = getdate(on_date or nowdate())
	last = calendar.monthrange(d.year, d.month)[1]
	return d.replace(day=1), d.replace(day=last)


def _has_usage_columns(doctype: str) -> bool:
	return frappe.db.table_exists(doctype) and frappe.db.has_column(f"{doctype} Item", FIELD_FNF_AMOUNT)


def get_monthly_usage(customer: str | None, on_date=None, exclude: str | None = None) -> float:
	"""F&F already granted to ``customer`` in the calendar month of ``on_date`` (net of returns)."""
	customer = cstr(customer).strip()
	if not customer:
		return 0.0
	start, end = month_bounds(on_date)
	total = 0.0
	for doctype in INVOICE_DOCTYPES:
		if not _has_usage_columns(doctype):
			continue
		consolidated = (
			"and ifnull(p.is_consolidated, 0) = 0" if frappe.db.has_column(doctype, "is_consolidated") else ""
		)
		exclude_sql = "and p.name != %(exclude)s" if exclude else ""
		params = {"customer": customer, "start": start, "end": end, "exclude": exclude}
		sales = frappe.db.sql(
			f"""
			select coalesce(sum(i.`{FIELD_FNF_AMOUNT}`), 0)
			from `tab{doctype} Item` i
			join `tab{doctype}` p on p.name = i.parent
			where p.docstatus = 1 and p.customer = %(customer)s and ifnull(p.is_return, 0) = 0
				and p.posting_date between %(start)s and %(end)s {consolidated} {exclude_sql}
			""",
			params,
		)[0][0]
		returns = frappe.db.sql(
			f"""
			select coalesce(sum(i.`{FIELD_FNF_AMOUNT}`), 0)
			from `tab{doctype} Item` i
			join `tab{doctype}` p on p.name = i.parent
			join `tab{doctype}` o on o.name = p.return_against
			where p.docstatus = 1 and p.customer = %(customer)s and ifnull(p.is_return, 0) = 1
				and o.posting_date between %(start)s and %(end)s {consolidated} {exclude_sql}
			""",
			params,
		)[0][0]
		total += flt(sales) + flt(returns)
	return max(0.0, total)


@dataclass
class CapContext:
	per_order_cap: float = 0.0
	monthly_cap: float = 0.0
	monthly_used: float = 0.0

	@property
	def active(self) -> bool:
		return self.per_order_cap > 0 or self.monthly_cap > 0

	@property
	def monthly_remaining(self) -> float | None:
		if self.monthly_cap <= 0:
			return None
		return max(0.0, bd.money(self.monthly_cap - self.monthly_used))

	@property
	def allowance(self) -> float | None:
		"""Max F&F for this order (``None`` = unlimited)."""
		limits = [v for v in (self.per_order_cap or None, self.monthly_remaining) if v is not None]
		return min(limits) if limits else None

	@property
	def binding_cap(self) -> str | None:
		if not self.active:
			return None
		remaining = self.monthly_remaining
		if self.per_order_cap > 0 and (remaining is None or self.per_order_cap <= remaining):
			return CAP_ORDER
		return CAP_MONTHLY if remaining is not None else None


def get_cap_context(program, customer, posting_date=None, exclude: str | None = None) -> CapContext:
	if program is None:
		return CapContext()
	monthly_cap = flt(getattr(program, "max_discount_per_customer_month", 0))
	return CapContext(
		per_order_cap=flt(getattr(program, "max_discount_per_order", 0)),
		monthly_cap=monthly_cap,
		monthly_used=get_monthly_usage(customer, posting_date, exclude=exclude) if monthly_cap > 0 else 0.0,
	)


def allocate(lines, allowance: float) -> dict:
	"""Spread ``allowance`` over F&F lines. ``lines``: ``[(idx, fnf_full, competitor_amount)]``.

	Returns ``{idx: capped_amount}`` for lines where (capped) F&F still wins.
	"""
	allowance = max(0.0, flt(allowance))
	winning = [ln for ln in lines if flt(ln[1]) > 0 and bd.money(ln[1]) >= bd.money(ln[2])]
	factor = 1.0
	while winning:
		total = sum(flt(ln[1]) for ln in winning)
		factor = 1.0 if total <= allowance else (allowance / total if total else 0.0)
		still = [
			ln for ln in winning if floor_cents(ln[1] * factor) > 0 and bd.money(floor_cents(ln[1] * factor)) >= bd.money(ln[2])
		]
		if len(still) == len(winning):
			break
		winning = still
	if factor >= 1.0:
		return {ln[0]: flt(ln[1]) for ln in winning}
	return {ln[0]: floor_cents(ln[1] * factor) for ln in winning}


def _fmt(value) -> str:
	return f"{bd.money(value):,.2f}"


def build_cap_payload(ctx: CapContext, uncapped_total: float, applied_total: float) -> dict:
	"""``caps`` block for API payloads: limits, usage, what was cut and why (EN + AR)."""
	remaining = ctx.monthly_remaining
	capped = ctx.active and bd.money(applied_total) < bd.money(uncapped_total)
	payload = {
		"active": ctx.active,
		"max_discount_per_order": bd.money(ctx.per_order_cap),
		"max_discount_per_customer_month": bd.money(ctx.monthly_cap),
		"monthly_used": bd.money(ctx.monthly_used),
		"monthly_remaining": remaining,
		"allowance": ctx.allowance,
		"fnf_uncapped_total": bd.money(uncapped_total),
		"fnf_applied_total": bd.money(applied_total),
		"capped": bool(capped),
		"cap_hit": None,
		"monthly_remaining_after": (
			max(0.0, bd.money(remaining - applied_total)) if remaining is not None else None
		),
		"message": "",
		"message_en": "",
		"message_ar": "",
	}
	if not capped:
		return payload

	cap_hit = ctx.binding_cap
	payload["cap_hit"] = cap_hit
	if cap_hit == CAP_MONTHLY and bd.money(remaining or 0) <= 0:
		en = "Monthly Friends & Family limit of {0} reached — no Friends & Family discount on this order."
		ar = "تم الوصول إلى الحد الشهري لخصم الأصدقاء والعائلة ({0}) — لن يتم تطبيق الخصم على هذه الفاتورة."
		args = (_fmt(ctx.monthly_cap),)
	elif cap_hit == CAP_MONTHLY:
		en = "Friends & Family discount reduced to {0} — monthly limit {1}, {2} left this month before this order."
		ar = "تم تخفيض خصم الأصدقاء والعائلة إلى {0} — الحد الشهري {1}، والمتبقي هذا الشهر قبل هذه الفاتورة {2}."
		args = (_fmt(applied_total), _fmt(ctx.monthly_cap), _fmt(remaining))
	else:
		en = "Friends & Family discount capped at the per-order maximum of {0} (full discount would be {1})."
		ar = "تم تحديد خصم الأصدقاء والعائلة بالحد الأقصى للفاتورة ({0}) بدلاً من {1}."
		args = (_fmt(ctx.per_order_cap), _fmt(uncapped_total))
	payload["message_en"] = en.format(*args)
	payload["message_ar"] = ar.format(*args)
	payload["message"] = _(en).format(*args)
	return payload
