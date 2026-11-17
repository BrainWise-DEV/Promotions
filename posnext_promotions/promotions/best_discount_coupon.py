# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Promo code (POS Coupon) vs Friends & Family / promotions — PN-128.

``validate_coupon`` (with cart ``items``) runs the coupon through
:func:`resolve_coupon_best_discount` so a coupon is only granted where it is
the single best discount:

* **Percentage coupon without a cap** → compared **per line** (money saved):
  coupon vs F&F (if the customer is eligible and the line is covered) vs the
  discount already on the line (promotion / manual). The coupon keeps only the
  lines it strictly wins; F&F wins ties; an existing promotion also keeps a tie
  (no churn).
* **Fixed-amount coupon, or percentage coupon with ``max_amount``** → its value
  is a cart total, so it is compared **as a whole**: coupon total vs the best
  non-coupon saving on the same lines. All-or-nothing.

The F&F side is the saving the customer would really get, **after** the
PN-129 caps (per order / monthly), so a cap-reduced F&F can lose to a coupon.

Lines currently stamped F&F are offered to the coupon engine as undiscounted
(otherwise the coupon would never see them — F&F lines count as "already
discounted"), so a stronger coupon can replace F&F.

When the coupon wins nothing the response is ``valid: False`` with a clear
English/Arabic message (e.g. "Friends & Family discount (30%) is better than
coupon SAVE15 (15%) …") and ``reason: better_discount_exists``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import frappe
from frappe import _
from frappe.utils import cstr, flt

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_money
from posnext_promotions.promotions.friends_and_family_apply import (
	_is_fnf_stamped,
	_restore_replaced_discount,
	restore_context,
	compute_friends_and_family_discounts,
	enrich_items_with_catalog,
)
from posnext_promotions.promotions.friends_and_family_eligibility import get_customer_eligibility

REASON_BETTER_DISCOUNT_EXISTS = "better_discount_exists"


def _neutral_copy(item, ctx):
	"""Line as the coupon engine should see it: an F&F stamp is not a blocker."""
	line = frappe._dict(dict(item))
	if _restore_replaced_discount(line, ctx):
		return line
	if _is_fnf_stamped(line):
		line.discount_source = None
		line.discount_percentage = 0
		line.discount_amount = 0
		line.is_already_discounted = 0
		price_list_rate = bd.line_price_list_rate(line)
		if price_list_rate:
			line.rate = price_list_rate
			line.amount = price_list_rate * bd.line_qty(line)
	return line


def _capped_fnf_candidates(items, customer, posting_date, pos_profile=None) -> dict:
	"""F&F the customer would get per line *without* the coupon — caps (PN-129) included.

	The coupon then competes with that (possibly cap-reduced) F&F saving. A capped
	line is shown by its money value: its % is neither the program's nor meaningful.
	"""
	ctx = restore_context(customer, posting_date, pos_profile)
	plan = compute_friends_and_family_discounts(
		customer=customer,
		items=[_neutral_copy(i, ctx) for i in items],
		posting_date=posting_date,
		pos_profile=pos_profile,
	)
	candidates = {}
	for line in plan.get("items") or []:
		if not line.get("applies"):
			continue
		candidates[line.get("idx")] = bd.Candidate(
			source=bd.SOURCE_FRIENDS_AND_FAMILY,
			amount=flt(line.get("discount_amount")),
			percentage=None if line.get("capped") else (flt(line.get("discount_percentage"), 2) or None),
		)
	return candidates


def _existing_candidate(item, coupon_code: str) -> bd.Candidate | None:
	"""Non-F&F discount already on the line (a promotion or manual discount)."""
	source = bd.classify_line_source(item)
	if source in (bd.SOURCE_NONE, bd.SOURCE_FREE_ITEM, bd.SOURCE_FRIENDS_AND_FAMILY):
		return None
	if source == bd.SOURCE_COUPON and bd.line_coupon_code(item) == coupon_code:
		return None  # the same coupon re-validated — not a competitor
	amount = bd.line_discount_value(item)
	if amount <= 0:
		return None
	pct = flt(item.get("discount_percentage") or 0)
	return bd.Candidate(
		source=source,
		amount=amount,
		percentage=pct if pct > 0 else None,
		code=bd.line_coupon_code(item) or None if source == bd.SOURCE_COUPON else None,
	)


def _coupon_line_amount(update: dict, item) -> float:
	"""What the coupon itself saves on the line (a stacked line's discount also holds the promotion)."""
	if update.get("stacked"):
		return flt(update.get("coupon_only_discount") or 0)
	pct = flt(update.get("discount_percentage") or 0)
	if pct > 0:
		return bd.line_gross(item) * pct / 100.0
	return flt(update.get("discount_amount") or 0)


def is_line_level_coupon(coupon) -> bool:
	return cstr(coupon.get("discount_type")) == "Percentage" and not flt(coupon.get("max_amount"))


def _coupon_percentage(coupon) -> float | None:
	return flt(coupon.get("discount_percentage")) if cstr(coupon.get("discount_type")) == "Percentage" else None


def _coupon_amounts(coupon, rows, tax_rate: float) -> list[float]:
	"""Coupon money per row. A "Grand Total" % coupon also saves the exclusive tax on it.

	``max_amount`` caps the total after the tax, as :func:`apply_coupon_discount` does.
	"""
	pct = _coupon_percentage(coupon)
	scale = 1.0 + tax_rate if pct is not None else 1.0
	amounts = [_coupon_line_amount(update, item) * scale for update, item in rows]
	cap = flt(coupon.get("max_amount"))
	total = sum(amounts)
	if pct is not None and cap and total > cap:
		amounts = [amount * cap / total for amount in amounts]
	return amounts


def _with_tax(candidate: bd.Candidate | None, tax_rate: float) -> bd.Candidate | None:
	"""A line-discount candidate in grand-total money: it also saves its exclusive tax."""
	if candidate is None or not tax_rate:
		return candidate
	return replace(candidate, amount=candidate.amount * (1.0 + tax_rate))


def _line_updates(result: dict, items) -> list:
	"""``(line update, its cart line)`` pairs of an ``apply_coupon_to_items`` result."""
	return [
		(update, items[update.get("line_key")])
		for update in result.get("line_updates") or []
		if update.get("line_key") is not None and update.get("line_key") < len(items)
	]


def coupon_value_on(coupon, lines, tax_rate: float = 0.0) -> float:
	"""The coupon's money on ``lines`` — the one value rule for the cart and the invoice."""
	from posnext_promotions.api.coupon_engine import apply_coupon_to_items

	lines = [frappe._dict(dict(line)) for line in lines or []]
	result = apply_coupon_to_items(coupon, lines)
	if not result.get("valid"):
		return 0.0
	rate = tax_rate if cstr(coupon.get("apply_on")) == "Grand Total" else 0.0
	return sum(_coupon_amounts(coupon, _line_updates(result, lines), rate))


@dataclass
class CouponDecision:
	"""Coupon vs F&F (and the discounts already on the lines) for one cart.

	``validate_coupon`` and ``calculate_coupon_discount`` both read this, so they
	always agree on the winner and on the coupon's value.
	"""

	coupon_code: str
	granularity: str
	result: dict  # apply_coupon_to_items on the lines without their F&F stamp
	items: list
	coupon_value: float = 0.0  # what the coupon gives on every line it can take
	line_decisions: list = field(default_factory=list)
	order_decision: bd.Decision | None = None
	kept: list = field(default_factory=list)  # line updates the coupon keeps ("replaces" set)
	kept_total: float = 0.0
	fnf_lines: int = 0  # lines where the customer has F&F without the coupon
	rejection: dict | None = None  # reason + messages when the coupon keeps nothing

	@property
	def coupon_wins(self) -> bool:
		return bool(self.kept)

	@property
	def summary(self) -> dict:
		return bd.summarize(self.line_decisions, self.order_decision)


def _rejection(decision: CouponDecision) -> dict:
	headline = decision.order_decision or next(
		(d for d in decision.line_decisions if d.winner_source != bd.SOURCE_COUPON), None
	)
	summary = decision.summary
	msgs = headline.messages() if headline else {"message": summary["message"], "en": "", "ar": ""}
	return {
		"reason": REASON_BETTER_DISCOUNT_EXISTS,
		"message": msgs["message"],
		"message_en": msgs["en"],
		"message_ar": msgs["ar"],
		"customer_message": msgs.get("customer"),
	}


@ff_money.in_cart_currency
def decide_coupon_vs_fnf(cart, coupon, customer=None, pos_profile=None) -> CouponDecision:
	"""The one coupon-vs-F&F resolver (PN-128).

	``cart``: ``items``, optional ``posting_date`` and ``taxes`` (the cart's tax rows;
	without them the POS Profile's tax template decides which taxes are exclusive).
	Granularity: an uncapped % coupon is compared per line, any other coupon on the
	order total. On the order the rival is every F&F line (an order coupon replaces
	F&F on the whole cart) plus the best other discount on the coupon's lines.
	"""
	from posnext_promotions.api.coupon_engine import apply_coupon_to_items, exclusive_tax_rate

	cart = cart or {}
	pos_profile = pos_profile or cart.get("pos_profile")
	posting_date = cart.get("posting_date")
	code = cstr(coupon.get("coupon_code")).upper()
	items = enrich_items_with_catalog([frappe._dict(dict(i)) for i in cart.get("items") or []])
	granularity = bd.GRANULARITY_LINE if is_line_level_coupon(coupon) else bd.GRANULARITY_ORDER

	ctx = restore_context(customer, posting_date, pos_profile)
	result = apply_coupon_to_items(coupon, [_neutral_copy(i, ctx) for i in items])
	decision = CouponDecision(coupon_code=code, granularity=granularity, result=result, items=items)
	if not result.get("valid"):
		return decision

	eligibility = get_customer_eligibility(customer, posting_date, pos_profile)
	program = eligibility.program if eligibility.eligible else None
	fnf_by_line = _capped_fnf_candidates(items, customer, posting_date, pos_profile) if program is not None else {}
	decision.fnf_lines = len(fnf_by_line)

	tax_rate = (
		exclusive_tax_rate(cart.get("taxes"), pos_profile) if cstr(coupon.get("apply_on")) == "Grand Total" else 0.0
	)
	if tax_rate:
		# Ruling 56: one unit for every candidate — what the customer saves including the
		# exclusive tax. A line discount also lowers the tax charged on that line.
		fnf_by_line = {idx: _with_tax(c, tax_rate) for idx, c in fnf_by_line.items()}
	updates = _line_updates(result, items)
	amounts = _coupon_amounts(coupon, updates, tax_rate)
	decision.coupon_value = sum(amounts)
	pct = _coupon_percentage(coupon)
	rows = [
		(
			update,
			item,
			bd.Candidate(source=bd.SOURCE_COUPON, amount=amount, percentage=pct, code=code),
			fnf_by_line.get(update.get("line_key")),
			_with_tax(_existing_candidate(item, code), tax_rate),
		)
		for (update, item), amount in zip(updates, amounts, strict=True)
	]

	def replaced(row):
		best = bd.pick_best([row[3], row[4]])
		return best.source if best else None

	kept_rows = []
	if granularity == bd.GRANULARITY_LINE:
		for row in rows:
			line = bd.decide([row[3], row[4], row[2]], idx=row[0].get("line_key"), item_code=row[1].get("item_code"))
			decision.line_decisions.append(line)
			if line.winner_source == bd.SOURCE_COUPON:
				kept_rows.append(row)
	else:
		row_keys = {row[0].get("line_key") for row in rows}
		# F&F on lines the coupon cannot take is lost too when the order coupon wins.
		fnf_part = sum(c.amount for idx, c in fnf_by_line.items() if idx not in row_keys)
		fnf_capped = any(c.percentage is None for c in fnf_by_line.values())
		other_part = 0.0
		other_source = None
		for row in rows:
			best = bd.pick_best([row[3], row[4]])
			if best is None:
				continue
			if best.source == bd.SOURCE_FRIENDS_AND_FAMILY:
				fnf_part += best.amount
			else:
				other_part += best.amount
				other_source = other_source or best.source
		rival_source = bd.SOURCE_FRIENDS_AND_FAMILY if fnf_part else (other_source or bd.SOURCE_NONE)
		rival = (
			bd.Candidate(
				source=rival_source,
				amount=fnf_part + other_part,
				percentage=flt(program.discount_percentage) if fnf_part and program and not fnf_capped else None,
			)
			if rival_source != bd.SOURCE_NONE
			else None
		)
		decision.order_decision = bd.decide(
			[rival, bd.Candidate(source=bd.SOURCE_COUPON, amount=decision.coupon_value, percentage=pct, code=code)],
			granularity=bd.GRANULARITY_ORDER,
		)
		if decision.order_decision.winner_source == bd.SOURCE_COUPON:
			kept_rows = list(rows)

	if not kept_rows:
		decision.rejection = _rejection(decision)
		return decision

	if len(kept_rows) != len(rows):
		kept_base = sum(bd.line_gross(row[1]) for row in kept_rows)
		min_amount = flt(coupon.get("min_amount"))
		if min_amount and kept_base < min_amount:
			decision.rejection = {
				"reason": REASON_BETTER_DISCOUNT_EXISTS,
				"message": _(
					"Coupon {0} only beats other discounts on items worth {1}, below its minimum of {2}."
				).format(code, ff_money.fmt(kept_base), ff_money.fmt(min_amount)),
			}
			return decision

	decision.kept = [{**row[0], "replaces": replaced(row)} for row in kept_rows]
	decision.kept_total = sum(row[2].amount for row in kept_rows)
	return decision


@ff_money.in_cart_currency
def resolve_coupon_best_discount(
	coupon, items, customer=None, posting_date=None, pos_profile=None, taxes=None
) -> dict:
	"""``validate_coupon`` result: the coupon only where it is the best single discount."""
	decision = decide_coupon_vs_fnf(
		{"items": items, "posting_date": posting_date, "taxes": taxes}, coupon, customer, pos_profile
	)
	result = dict(decision.result)
	result["coupon_value"] = flt(decision.coupon_value, 6)
	result["best_discount"] = decision.summary
	if not result.get("valid"):
		result["best_discount"] = bd.summarize([])
		return result

	if decision.rejection:
		result.update(
			{
				"valid": False,
				"message_en": None,
				"message_ar": None,
				"customer_message": None,
				**decision.rejection,
				"line_updates": [],
				"total_discount": 0,
			}
		)
		return result

	summary = result["best_discount"]
	result.update(
		{
			"line_updates": decision.kept,
			"eligible_item_codes": [decision.items[u.get("line_key")].get("item_code") for u in decision.kept],
			"total_discount": flt(decision.kept_total, 6),
			"message": summary["message"] if summary.get("suppressed") else result.get("message"),
			"message_en": summary.get("message_en"),
			"message_ar": summary.get("message_ar"),
			"customer_message": summary.get("customer_message"),
		}
	)
	return result
