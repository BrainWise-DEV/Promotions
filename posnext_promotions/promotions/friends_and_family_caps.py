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
POS Invoice Item. On submit, ``ff_ledger`` writes one **Friends and Family
Usage** row per such line (company currency); usage for a month is the sum of
those rows that are not cancelled. Drafts never count, cancelled invoices stop
counting, consolidated Sales Invoices are skipped.

Caps are company-currency amounts, counted per company (companies may keep
different currencies). An invoice in another currency gets an allowance of
``remaining / conversion_rate`` in its own currency.

Returns / credit notes: a return line copies the F&F source of the original
line; on validate we store the **negative** F&F saving for the returned qty.
The return is credited to the month of the **original** invoice
(``return_against``): returning a September purchase in October does not
create extra October allowance; returning an October purchase in October
gives the allowance back.

Allocation
----------
When the allowance is smaller than the uncapped F&F total, the allowance is
spread **proportionally** over the covered lines where F&F wins. Because a
smaller F&F can lose to a line's own promotion, the engine re-checks: lines
where the reduced F&F now loses (tie still → F&F) drop out and the allowance is
re-spread over the rest (a dropped line is not re-admitted, which keeps the
loop finite and deterministic). Line amounts are rounded **down** to the
currency's smallest unit (:mod:`ff_money`), so the applied total never exceeds
the cap.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_money

FIELD_FNF_AMOUNT = "posnext_fnf_discount_amount"
FIELD_DISCOUNT_SOURCE = "posnext_discount_source"
# PN-133: the discount F&F replaced on the line (JSON), so a later save can
# re-run the comparison — and give the promotion back if F&F no longer wins.
FIELD_FNF_REPLACED = "posnext_fnf_replaced_discount"
# The F&F line snapshot: the program's percentage and the program name (blank in Phase 1).
# Together with the source tag these mark a line as F&F; no percentage heuristic.
FIELD_FNF_PERCENTAGE = "posnext_fnf_percentage"
FIELD_FNF_PROGRAM = "posnext_fnf_program"
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
	{
		"fieldname": FIELD_FNF_PERCENTAGE,
		"module": "POSNext Promotions",
		"label": "Friends & Family %",
		"fieldtype": "Percent",
		"insert_after": FIELD_FNF_REPLACED,
		"read_only": 1,
		"no_copy": 0,
		"print_hide": 1,
		"description": "Friends & Family program percentage when this line was stamped "
		"(copied from the original line on returns).",
	},
	{
		"fieldname": FIELD_FNF_PROGRAM,
		"module": "POSNext Promotions",
		"label": "Friends & Family Program",
		"fieldtype": "Data",
		"insert_after": FIELD_FNF_PERCENTAGE,
		"read_only": 1,
		"no_copy": 0,
		"print_hide": 1,
		"description": "Friends & Family program that discounted this line.",
	},
]


def ensure_invoice_item_fields() -> None:
	"""Create the F&F audit fields on Sales / POS Invoice Item (idempotent)."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	specs = {dt: ITEM_FIELD_SPECS for dt in INVOICE_ITEM_DOCTYPES if frappe.db.exists("DocType", dt)}
	if specs:
		create_custom_fields(specs, update=True)


def month_bounds(on_date=None) -> tuple:
	d = getdate(on_date or nowdate())
	last = calendar.monthrange(d.year, d.month)[1]
	return d.replace(day=1), d.replace(day=last)


def get_monthly_usage(
	customer: str | None, on_date=None, exclude: str | None = None, company: str | None = None
) -> float:
	"""F&F ``company`` already granted ``customer`` in the month of ``on_date`` (net of returns).

	Reads the usage counter (that company's currency). Caps are per company:
	``company`` is required whenever there is a customer. No lock: validate may
	see a stale value; submit re-checks under the counter lock.
	"""
	from posnext_promotions.promotions import ff_ledger

	return ff_ledger.get_usage(
		customer, company, ff_ledger.period_key(on_date or nowdate()), exclude_invoice=exclude
	)


def cart_cap_basis(pos_profile: str | None, posting_date=None) -> tuple[str, float]:
	"""``(company, conversion_rate)`` caps use for a cart sold through ``pos_profile``."""
	rate = cap_conversion_rate(pos_profile, posting_date)  # raises without a valid profile
	return frappe.get_cached_value("POS Profile", pos_profile, "company"), rate


def cap_conversion_rate(pos_profile: str | None, posting_date=None) -> float:
	"""Company-currency units per cart-currency unit for a POS Profile's cart (like ``doc.conversion_rate``).

	1 when the profile currency equals the company's. Otherwise ERPNext's
	exchange rate on ``posting_date``. A missing profile or rate is an error,
	never 1 (ERPNext could not build that invoice either). Called only when caps
	are set.
	"""
	if not pos_profile:
		frappe.throw(_("A POS Profile is needed to check Friends & Family caps in company currency."))
	values = frappe.get_cached_value("POS Profile", pos_profile, ["company", "currency"], as_dict=True)
	if not values:
		frappe.throw(_("POS Profile {0} not found.").format(pos_profile))
	company_currency = (
		frappe.get_cached_value("Company", values.company, "default_currency") if values.company else None
	)
	# POS Profile.company / currency and Company.default_currency are mandatory in
	# ERPNext; a blank one cannot hold a foreign-currency cart.
	if not values.currency or not company_currency or values.currency == company_currency:
		return 1.0
	from erpnext.setup.utils import get_exchange_rate

	rate = flt(get_exchange_rate(values.currency, company_currency, posting_date or nowdate()))
	if rate <= 0:
		frappe.throw(
			_("No exchange rate from {0} to {1} on {2}: Friends & Family caps cannot be checked.").format(
				values.currency, company_currency, posting_date or nowdate()
			)
		)
	return rate


@dataclass
class CapContext:
	# Caps and usage are company currency; ``allowance`` is invoice currency.
	per_order_cap: float = 0.0
	monthly_cap: float = 0.0
	monthly_used: float = 0.0
	conversion_rate: float = 1.0
	# ``None``: the engine's current currency / the invoice currency.
	currency: str | None = None
	company_currency: str | None = None

	def __post_init__(self):
		self.currency = self.currency or ff_money.current_currency()
		self.company_currency = self.company_currency or self.currency

	@property
	def active(self) -> bool:
		return self.per_order_cap > 0 or self.monthly_cap > 0

	@property
	def monthly_remaining(self) -> float | None:
		if self.monthly_cap <= 0:
			return None
		return max(0.0, ff_money.money(self.monthly_cap - self.monthly_used, self.company_currency))

	@property
	def allowance(self) -> float | None:
		"""Max F&F for this order in invoice currency (``None`` = unlimited)."""
		limits = [v for v in (self.per_order_cap or None, self.monthly_remaining) if v is not None]
		if not limits:
			return None
		rate = flt(self.conversion_rate) or 1.0
		return min(limits) if rate == 1.0 else ff_money.floor_money(min(limits) / rate, self.currency)

	@property
	def binding_cap(self) -> str | None:
		if not self.active:
			return None
		remaining = self.monthly_remaining
		if self.per_order_cap > 0 and (remaining is None or self.per_order_cap <= remaining):
			return CAP_ORDER
		return CAP_MONTHLY if remaining is not None else None


def get_cap_context(
	program,
	customer,
	posting_date=None,
	exclude: str | None = None,
	conversion_rate: float = 1.0,
	company: str | None = None,
) -> CapContext:
	"""Caps for one order sold by ``company``. ``conversion_rate`` converts invoice currency
	to company currency; the monthly cap counts only that company's usage."""
	if program is None:
		return CapContext()
	monthly_cap = flt(getattr(program, "max_discount_per_customer_month", 0))
	return CapContext(
		per_order_cap=flt(getattr(program, "max_discount_per_order", 0)),
		monthly_cap=monthly_cap,
		monthly_used=(
			get_monthly_usage(customer, posting_date, exclude=exclude, company=company) if monthly_cap > 0 else 0.0
		),
		conversion_rate=flt(conversion_rate) or 1.0,
		company_currency=ff_money.company_currency(company),
	)


def allocate(lines, allowance: float) -> dict:
	"""Spread ``allowance`` over F&F lines. ``lines``: ``[(idx, fnf_full, competitor_amount)]``.

	Returns ``{idx: capped_amount}`` for lines where (capped) F&F still wins.
	"""
	allowance = max(0.0, flt(allowance))
	floor = ff_money.floor_money
	winning = [ln for ln in lines if flt(ln[1]) > 0 and bd.money(ln[1]) >= bd.money(ln[2])]
	factor = 1.0
	while winning:
		total = sum(flt(ln[1]) for ln in winning)
		factor = 1.0 if total <= allowance else (allowance / total if total else 0.0)
		still = [ln for ln in winning if floor(ln[1] * factor) > 0 and bd.money(floor(ln[1] * factor)) >= bd.money(ln[2])]
		if len(still) == len(winning):
			break
		winning = still
	if factor >= 1.0:
		return {ln[0]: flt(ln[1]) for ln in winning}
	return {ln[0]: floor(ln[1] * factor) for ln in winning}


def build_cap_payload(ctx: CapContext, uncapped_total: float, applied_total: float) -> dict:
	"""``caps`` block for API payloads: limits, usage, what was cut and why (EN + AR).

	Caps and usage are in the company currency, F&F totals in the invoice currency;
	each amount is rounded and shown in its own.
	"""
	remaining = ctx.monthly_remaining

	def company(value) -> float:
		return ff_money.money(value, ctx.company_currency)

	def invoice(value) -> float:
		return ff_money.money(value, ctx.currency)

	capped = ctx.active and invoice(applied_total) < invoice(uncapped_total)
	payload = {
		"active": ctx.active,
		"max_discount_per_order": company(ctx.per_order_cap),
		"max_discount_per_customer_month": company(ctx.monthly_cap),
		"monthly_used": company(ctx.monthly_used),
		"monthly_remaining": remaining,
		"allowance": ctx.allowance,
		"fnf_uncapped_total": invoice(uncapped_total),
		"fnf_applied_total": invoice(applied_total),
		"capped": bool(capped),
		"cap_hit": None,
		"monthly_remaining_after": (
			max(0.0, company(remaining - flt(applied_total) * (flt(ctx.conversion_rate) or 1.0)))
			if remaining is not None
			else None
		),
		"message": "",
		"message_en": "",
		"message_ar": "",
		"customer_message": None,
	}
	if not capped:
		return payload

	def fmt_company(value) -> str:
		return ff_money.fmt(company(value), ctx.company_currency)

	def fmt_invoice(value) -> str:
		return ff_money.fmt(invoice(value), ctx.currency)

	cap_hit = ctx.binding_cap
	payload["cap_hit"] = cap_hit
	if cap_hit == CAP_MONTHLY and company(remaining or 0) <= 0:
		en = "Monthly Friends & Family limit of {0} reached — no Friends & Family discount on this order."
		ar = "تم الوصول إلى الحد الشهري لخصم الأصدقاء والعائلة ({0}) — لن يتم تطبيق الخصم على هذه الفاتورة."
		args = (fmt_company(ctx.monthly_cap),)
	elif cap_hit == CAP_MONTHLY:
		en = "Friends & Family discount reduced to {0} — monthly limit {1}, {2} left this month before this order."
		ar = "تم تخفيض خصم الأصدقاء والعائلة إلى {0} — الحد الشهري {1}، والمتبقي هذا الشهر قبل هذه الفاتورة {2}."
		args = (fmt_invoice(applied_total), fmt_company(ctx.monthly_cap), fmt_company(remaining))
	else:
		en = "Friends & Family discount capped at the per-order maximum of {0} (full discount would be {1})."
		ar = "تم تحديد خصم الأصدقاء والعائلة بالحد الأقصى للفاتورة ({0}) بدلاً من {1}."
		args = (fmt_company(ctx.per_order_cap), fmt_invoice(uncapped_total))
	payload["message_en"] = en.format(*args)
	payload["message_ar"] = ar.format(*args)
	payload["message"] = _(en).format(*args)
	if cap_hit == CAP_MONTHLY and company(remaining or 0) <= 0:
		from posnext_promotions.promotions.friends_and_family_messages import (
			EVENT_MONTHLY_CAP_REACHED,
			get_customer_message,
		)

		payload["customer_message"] = get_customer_message(EVENT_MONTHLY_CAP_REACHED)
	return payload
