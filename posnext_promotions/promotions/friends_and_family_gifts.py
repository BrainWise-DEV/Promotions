# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""F&F vs free-gift coexistence (Miraaya clarifications).

Rules
-----
* **Allow Free Gift with F&F** (Friends and Family Settings, global):
  controls Free Item, GWP, Cart Total Gift, and Brand Gift (Gift Pool counts as
  Free Item). Checked → those gifts may be awarded while F&F is applied.
  Unchecked → if F&F is applied on the cart, those gifts are suppressed.
* **Buy X Get Y (BXGY)** is *not* a Free Item type. It never combines with F&F
  (always suppressed when F&F is applied), regardless of the checkbox.
* Mixed carts follow the same cart-level rule: once F&F is applied, incompatible
  gifts are not awarded.

Classification maps Pricing Rule / Promotional Scheme fields onto Magento-style
gift kinds used in the clarification email.
"""

from __future__ import annotations

from frappe.utils import cint, cstr, flt

from posnext_promotions.promotions.friends_and_family import get_settings

# Magento-style kinds from the clarification.
GIFT_KIND_FREE_ITEM = "free_item"
GIFT_KIND_GWP = "gwp"
GIFT_KIND_CART_TOTAL = "cart_total_gift"
GIFT_KIND_BRAND = "brand_gift"
GIFT_KIND_BXGY = "bxgy"

PROMOTION_TYPE_GWP = "GWP"
PROMOTION_TYPE_GIFT_POOL = "Gift Pool"
DISCOUNT_SOURCE_FREE_ITEM = "free_item"
DISCOUNT_SOURCE_GWP = "gwp"

# Controlled by Settings.allow_free_gift_with_ff.
CONFIGURABLE_GIFT_KINDS = frozenset(
	{
		GIFT_KIND_FREE_ITEM,
		GIFT_KIND_GWP,
		GIFT_KIND_CART_TOTAL,
		GIFT_KIND_BRAND,
	}
)

# Always mutually exclusive with F&F.
ALWAYS_BLOCKED_WITH_FF = frozenset({GIFT_KIND_BXGY})

_GIFT_LINE_SOURCES = frozenset({DISCOUNT_SOURCE_GWP, DISCOUNT_SOURCE_FREE_ITEM})


def allow_free_gift_with_ff() -> bool:
	"""Global Settings checkbox (default off)."""
	return bool(cint(get_settings().get("allow_free_gift_with_ff")))


def classify_gift_rule(rule) -> str | None:
	"""Map a Pricing Rule / rule_map row to a gift kind, or ``None`` if not a gift."""
	if not rule:
		return None

	promo_type = cstr(rule.get("promotion_type") or "").strip()
	is_product = cstr(rule.get("price_or_product_discount") or "") == "Product"
	# Buy X Get Y first — even if the scheme is labelled GWP (common for B2G1).
	# Client: Free Item does NOT include BXGY; BXGY never combines with F&F.
	if is_product and cint(rule.get("same_item")):
		return GIFT_KIND_BXGY

	if promo_type == PROMOTION_TYPE_GWP:
		return GIFT_KIND_GWP
	if promo_type == PROMOTION_TYPE_GIFT_POOL:
		# Gift Pool is a free-item grant from a group pool — under the Free Item
		# family of the global Allow Free Gift checkbox.
		return GIFT_KIND_FREE_ITEM

	if not is_product:
		return None

	apply_on = cstr(rule.get("apply_on") or "").strip()
	if apply_on == "Transaction":
		return GIFT_KIND_CART_TOTAL
	if apply_on == "Brand":
		return GIFT_KIND_BRAND

	# Different-SKU free item on Item Code / Item Group.
	return GIFT_KIND_FREE_ITEM


def blocked_gift_kinds(*, fnf_applied: bool, allow_free_gift: bool | None = None) -> frozenset[str]:
	"""Gift kinds that must not be awarded when F&F is on the cart."""
	if not fnf_applied:
		return frozenset()
	blocked = set(ALWAYS_BLOCKED_WITH_FF)
	if allow_free_gift is None:
		allow_free_gift = allow_free_gift_with_ff()
	if not allow_free_gift:
		blocked |= CONFIGURABLE_GIFT_KINDS
	return frozenset(blocked)


def _rule_name_from_free_key(key) -> str:
	if isinstance(key, tuple | list) and len(key) > 1:
		return cstr(key[1])
	return ""


def _item_rule_names(item) -> list[str]:
	raw = item.get("pricing_rules")
	if isinstance(raw, list | tuple | set):
		return [cstr(r) for r in raw if cstr(r)]
	text = cstr(raw).strip()
	if not text:
		return []
	if text.startswith("["):
		# Rare JSON-ish echo; fall back to comma split of inner tokens.
		text = text.strip("[]").replace('"', "").replace("'", "")
	return [part.strip() for part in text.split(",") if part.strip()]


def _clear_line_gift_stamp(item) -> None:
	"""Undo a paid-line free-unit / GWP stamp so F&F can reclaim the line."""
	price_list_rate = flt(item.get("price_list_rate") or item.get("rate") or 0)
	if isinstance(item, dict):
		item["discount_amount"] = 0
		item["discount_percentage"] = 0
		item["free_qty"] = 0
		item["gwp_free_qty"] = 0
		item["discount_source"] = None
		item["rate"] = price_list_rate
		item["pricing_rules"] = ""
	else:
		item.discount_amount = 0
		item.discount_percentage = 0
		item.free_qty = 0
		item.gwp_free_qty = 0
		item.discount_source = None
		item.rate = price_list_rate
		item.pricing_rules = ""


def _line_has_gift_stamp(item) -> bool:
	if cint(item.get("is_free_item")):
		return True
	if flt(item.get("free_qty")) > 0 or flt(item.get("gwp_free_qty")) > 0:
		return True
	return cstr(item.get("discount_source")) in _GIFT_LINE_SOURCES and (
		flt(item.get("discount_amount")) > 0 or flt(item.get("discount_percentage")) > 0
	)


def suppress_incompatible_gifts(
	*,
	fnf_applied: bool,
	allow_free_gift: bool | None = None,
	prepared_items,
	free_items_map: dict,
	rule_map: dict,
	applied_rules: set | None = None,
) -> set[str]:
	"""Remove gift rows / stamps that cannot coexist with an applied F&F discount.

	Returns the set of Pricing Rule names that were suppressed.
	"""
	blocked = blocked_gift_kinds(fnf_applied=fnf_applied, allow_free_gift=allow_free_gift)
	if not blocked:
		return set()

	suppressed: set[str] = set()

	for key in list(free_items_map.keys()):
		rule_name = _rule_name_from_free_key(key)
		rule = rule_map.get(rule_name) if rule_name else None
		kind = classify_gift_rule(rule) if rule is not None else GIFT_KIND_FREE_ITEM
		if kind in blocked:
			free_items_map.pop(key, None)
			if rule_name:
				suppressed.add(rule_name)

	# Paid lines stamped with a blocked gift (bundled same-item / GWP line discount).
	for item in prepared_items or []:
		if cint(item.get("is_free_item")):
			continue
		if not _line_has_gift_stamp(item):
			continue
		rule_names = _item_rule_names(item)
		kinds = {classify_gift_rule(rule_map.get(name)) for name in rule_names if name in rule_map}
		kinds.discard(None)
		# Unknown gift stamp with no classifiable rule → treat as Free Item.
		if not kinds and cstr(item.get("discount_source")) in _GIFT_LINE_SOURCES:
			kinds = {GIFT_KIND_FREE_ITEM}
		if kinds & blocked:
			suppressed.update(rule_names)
			_clear_line_gift_stamp(item)

	if applied_rules is not None and suppressed:
		applied_rules.difference_update(suppressed)

	return suppressed
