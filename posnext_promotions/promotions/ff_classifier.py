# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""One classifier for every promotion (Pricing Rule) kind F&F has to reason about.

Kinds are ``ff_constants.PROMOTION_KINDS``. ``classify_promotion`` is the single
source of truth; ``friends_and_family_gifts.classify_gift_rule`` and
``scheme_blocking_kind`` are thin wrappers that map kinds back to their older
return values.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr, flt

PROMOTION_TYPE_GWP = "GWP"
PROMOTION_TYPE_GIFT_POOL = "Gift Pool"
PROMOTION_TYPE_ITEM_LEVEL = "Item Level Discount"
ACCUMULATIVE_MODE = "Accumulative"

GIFT_KINDS = frozenset({"free_item", "gwp", "cart_total_gift", "brand_gift"})

_RULE_FIELDS = (
	"name",
	"promotion_type",
	"price_or_product_discount",
	"same_item",
	"apply_on",
	"min_qty",
	"coupon_code_based",
	"apply_discount_on_price",
	"min_or_max_discount_qty_limit",
	"rate_or_discount",
	"discount_percentage",
	"rate",
)


def _rule_values(rule_or_name):
	"""Rule row with the fields classification needs (from the cached Pricing Rule when missing)."""
	if not rule_or_name:
		return None
	if isinstance(rule_or_name, str):
		name, row = rule_or_name, None
	else:
		row = rule_or_name
		name = cstr(row.get("name"))
		if all(field in row for field in _RULE_FIELDS):
			return row
	if not name:
		return row
	# None for a missing rule. The document cache is cleared on save and db.set_value.
	values = frappe.get_cached_value("Pricing Rule", name, list(_RULE_FIELDS), as_dict=True)
	if values is None:
		return row
	merged = frappe._dict(values)
	if row is not None:
		for key, value in dict(row).items():
			merged.setdefault(key, value)
	return merged


def is_price_bxgy_rule(rule_or_name) -> bool:
	"""Price Discount Buy X Get Y: 100% off a limited number of units (e.g. "buy one").

	A Min/Max price rule with a positive discounted-qty limit whose discount makes
	those units free (100% or a zero Rate), whatever its Promotion Type label.
	A Transaction rule is a header discount, never Buy X Get Y.
	"""
	rule = _rule_values(rule_or_name)
	if not rule:
		return False
	if cstr(rule.get("price_or_product_discount") or "Price") != "Price":
		return False
	if cstr(rule.get("apply_on")).strip() == "Transaction":
		return False
	if cstr(rule.get("apply_discount_on_price")) not in ("Min", "Max"):
		return False
	if flt(rule.get("min_or_max_discount_qty_limit")) <= 0:
		return False
	kind = cstr(rule.get("rate_or_discount") or "Discount Percentage")
	if kind == "Discount Percentage":
		return flt(rule.get("discount_percentage")) >= 100.0 - 0.0001
	if kind == "Rate":
		return flt(rule.get("rate")) <= 0
	return False


def is_gift_kind(kind: str) -> bool:
	return kind in GIFT_KINDS


def classify_promotion(rule_or_name) -> str:
	"""Return one of ``PROMOTION_KINDS`` for a Pricing Rule row or name."""
	rule = _rule_values(rule_or_name)
	if not rule:
		return "unclassified"

	promo_type = cstr(rule.get("promotion_type") or "").strip()
	discount_type = cstr(rule.get("price_or_product_discount") or "")
	is_product = discount_type == "Product"
	apply_on = cstr(rule.get("apply_on") or "").strip()

	# Buy X Get Y first, even if the scheme is labelled GWP (common for B2G1).
	if is_product and cint(rule.get("same_item")):
		return "bxgy"
	# Price "buy one" (100% off a limited qty) is Buy X Get Y too, whatever its label.
	if is_price_bxgy_rule(rule):
		return "bxgy"
	# Routine: a blank discount type counts as Price here (as it always did).
	if (
		promo_type == PROMOTION_TYPE_ITEM_LEVEL
		and (discount_type or "Price") == "Price"
		and apply_on != "Transaction"
		and cstr(rule.get("apply_discount_on_price")) != ACCUMULATIVE_MODE
		and not cint(rule.get("coupon_code_based"))
	):
		return "item_level"
	if promo_type == PROMOTION_TYPE_GWP:
		return "gwp"
	if promo_type == PROMOTION_TYPE_GIFT_POOL:
		return "free_item"
	if not discount_type:
		return "unclassified"
	if not is_product:
		return "price"
	# A Product rule with an unknown apply_on stays free_item on purpose.
	if apply_on == "Transaction":
		return "cart_total_gift"
	if apply_on == "Brand":
		return "brand_gift"
	return "free_item"


def unclassified_rules(rule_names: list[str]) -> list[str]:
	"""Names among ``rule_names`` the classifier cannot place."""
	return [name for name in rule_names if classify_promotion(name) == "unclassified"]
