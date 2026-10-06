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

Lines currently stamped F&F are offered to the coupon engine as undiscounted
(otherwise the coupon would never see them — F&F lines count as "already
discounted"), so a stronger coupon can replace F&F.

When the coupon wins nothing the response is ``valid: False`` with a clear
English/Arabic message (e.g. "Friends & Family discount (30%) is better than
coupon SAVE15 (15%) …") and ``reason: better_discount_exists``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions.friends_and_family import COVER_OK
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
	_is_discountable,
	_is_protected_line,
	enrich_items_with_catalog,
)
from posnext_promotions.promotions.friends_and_family_eligibility import get_customer_eligibility

REASON_BETTER_DISCOUNT_EXISTS = "better_discount_exists"


def _neutral_copy(item):
	"""Line as the coupon engine should see it: an F&F stamp is not a blocker."""
	line = frappe._dict(dict(item))
	if cstr(line.get("discount_source")) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
		line.discount_source = None
		line.discount_percentage = 0
		line.discount_amount = 0
		line.is_already_discounted = 0
		price_list_rate = bd.line_price_list_rate(line)
		if price_list_rate:
			line.rate = price_list_rate
			line.amount = price_list_rate * bd.line_qty(line)
	return line


def _fnf_candidate(item, program) -> bd.Candidate | None:
	if program is None or _is_protected_line(item) or not _is_discountable(item):
		return None
	if program.covers_reason(item) != COVER_OK:
		return None
	pct = flt(program.discount_percentage)
	return bd.Candidate(
		source=bd.SOURCE_FRIENDS_AND_FAMILY, amount=bd.line_gross(item) * pct / 100.0, percentage=pct
	)


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
	pct = flt(update.get("discount_percentage") or 0)
	if pct > 0:
		return bd.line_gross(item) * pct / 100.0
	return flt(update.get("discount_amount") or 0)


def is_line_level_coupon(coupon) -> bool:
	return cstr(coupon.get("discount_type")) == "Percentage" and not flt(coupon.get("max_amount"))


def resolve_coupon_best_discount(coupon, items, customer=None, posting_date=None) -> dict:
	"""Coupon result restricted to where the coupon is the best single discount."""
	from posnext_promotions.api.coupon_engine import apply_coupon_to_items

	code = cstr(coupon.get("coupon_code")).upper()
	items = enrich_items_with_catalog([frappe._dict(dict(i)) for i in items or []])
	eligibility = get_customer_eligibility(customer, posting_date)
	program = eligibility.program if eligibility.eligible else None

	result = apply_coupon_to_items(coupon, [_neutral_copy(i) for i in items])
	if not result.get("valid"):
		result["best_discount"] = bd.summarize([])
		return result

	updates = list(result.get("line_updates") or [])
	pct = flt(coupon.get("discount_percentage")) if cstr(coupon.get("discount_type")) == "Percentage" else None
	rows = []
	for update in updates:
		idx = update.get("line_key")
		if idx is None or idx >= len(items):
			continue
		item = items[idx]
		rows.append(
			(
				update,
				item,
				bd.Candidate(
					source=bd.SOURCE_COUPON, amount=_coupon_line_amount(update, item), percentage=pct, code=code
				),
				_fnf_candidate(item, program),
				_existing_candidate(item, code),
			)
		)

	decisions = []
	order_decision = None
	if is_line_level_coupon(coupon):
		kept = []
		for update, item, coupon_c, fnf_c, existing_c in rows:
			decision = bd.decide(
				[fnf_c, existing_c, coupon_c], idx=update.get("line_key"), item_code=item.get("item_code")
			)
			decisions.append(decision)
			if decision.winner_source == bd.SOURCE_COUPON:
				replaced = bd.pick_best([fnf_c, existing_c])
				kept.append({**update, "replaces": replaced.source if replaced else None})
	else:
		coupon_total = sum(r[2].amount for r in rows)
		fnf_part = 0.0
		other_part = 0.0
		other_source = None
		for _update, _item, _coupon_c, fnf_c, existing_c in rows:
			best = bd.pick_best([fnf_c, existing_c])
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
				percentage=flt(program.discount_percentage) if fnf_part and program else None,
			)
			if rival_source != bd.SOURCE_NONE
			else None
		)
		order_decision = bd.decide(
			[rival, bd.Candidate(source=bd.SOURCE_COUPON, amount=coupon_total, percentage=pct, code=code)],
			granularity=bd.GRANULARITY_ORDER,
		)
		if order_decision.winner_source == bd.SOURCE_COUPON:
			kept = [
				{**r[0], "replaces": (bd.pick_best([r[3], r[4]]).source if bd.pick_best([r[3], r[4]]) else None)}
				for r in rows
			]
		else:
			kept = []

	summary = bd.summarize(decisions, order_decision)
	result["best_discount"] = summary

	if not kept:
		headline = order_decision or next((d for d in decisions if d.winner_source != bd.SOURCE_COUPON), None)
		msgs = headline.messages() if headline else {"message": summary["message"], "en": "", "ar": ""}
		result.update(
			{
				"valid": False,
				"reason": REASON_BETTER_DISCOUNT_EXISTS,
				"message": msgs["message"],
				"message_en": msgs["en"],
				"message_ar": msgs["ar"],
				"line_updates": [],
				"total_discount": 0,
			}
		)
		return result

	kept_keys = {u.get("line_key") for u in kept}
	if len(kept) != len(rows):
		kept_base = sum(bd.line_gross(items[k]) for k in kept_keys)
		min_amount = flt(coupon.get("min_amount"))
		if min_amount and kept_base < min_amount:
			result.update(
				{
					"valid": False,
					"reason": REASON_BETTER_DISCOUNT_EXISTS,
					"message": _(
						"Coupon {0} only beats other discounts on items worth {1}, below its minimum of {2}."
					).format(code, frappe.format_value(kept_base, {"fieldtype": "Currency"}), min_amount),
					"line_updates": [],
					"total_discount": 0,
				}
			)
			return result

	total = sum(r[2].amount for r in rows if r[0].get("line_key") in kept_keys)
	result.update(
		{
			"line_updates": kept,
			"eligible_item_codes": [items[k].get("item_code") for k in kept_keys],
			"total_discount": flt(total, 6),
			"message": summary["message"] if summary.get("suppressed") else result.get("message"),
			"message_en": summary.get("message_en"),
			"message_ar": summary.get("message_ar"),
		}
	)
	return result
