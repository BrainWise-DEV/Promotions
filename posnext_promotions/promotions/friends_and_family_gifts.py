# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""F&F vs scheme / free-gift coexistence (Miraaya rules, Malak 9 Nov + meeting 16 Nov).

Rules
-----
* **Buy X Get Y (BXGY)** and **Routine** (Promotional Scheme of type *Item Level
  Discount*) are *not eligible* for F&F. Lines matched by such a scheme never
  receive F&F — they keep their promotion (free units / item-level discount).
  Other cart lines still get F&F. See
  :func:`ff_compatibility.block_ineligible_lines` and :func:`line_blocked_by_scheme`.
* **Allow Free Gift with F&F** (Friends and Family Settings, global, default
  off) controls Free Item, GWP, Gift Pool, Cart Total Gift and Brand Gift:

  - checked   → the gift and F&F both apply;
  - unchecked → a gift whose triggering paid line(s) received F&F is not
    awarded (GWP line discounts are not stamped on F&F lines). A cart-total
    (Transaction) gift is removed when F&F applied anywhere on the cart; its
    threshold is evaluated on the subtotal *after* F&F either way.

  BXGY is never touched by this setting (its lines carry no F&F).
* The hidden per-scheme ``apply_with_ff`` field is left untouched (reserved for a
  future per-promotion override).
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr, flt

from posnext_promotions.promotions.ff_classifier import (  # noqa: F401
	ACCUMULATIVE_MODE,
	PROMOTION_TYPE_GIFT_POOL,
	PROMOTION_TYPE_GWP,
	PROMOTION_TYPE_ITEM_LEVEL,
	_rule_values,
	classify_promotion,
	is_gift_kind,
	is_price_bxgy_rule,
)
from posnext_promotions.promotions.friends_and_family import get_settings

# Magento-style kinds from the clarification.
GIFT_KIND_FREE_ITEM = "free_item"
GIFT_KIND_GWP = "gwp"
GIFT_KIND_CART_TOTAL = "cart_total_gift"
GIFT_KIND_BRAND = "brand_gift"
GIFT_KIND_BXGY = "bxgy"

# Scheme kinds whose lines are not eligible for F&F.
SCHEME_KIND_BXGY = GIFT_KIND_BXGY
SCHEME_KIND_ITEM_LEVEL = "item_level"

DISCOUNT_SOURCE_FREE_ITEM = "free_item"
DISCOUNT_SOURCE_GWP = "gwp"
DISCOUNT_SOURCE_FRIENDS_AND_FAMILY = "friends_and_family"

# Cart-line flag set by apply_offers before F&F runs (comma separated rule names).
FIELD_FNF_BLOCKED_BY = "fnf_blocked_by_scheme"

# Controlled by Settings.allow_free_gift_with_ff.
CONFIGURABLE_GIFT_KINDS = frozenset(
	{
		GIFT_KIND_FREE_ITEM,
		GIFT_KIND_GWP,
		GIFT_KIND_CART_TOTAL,
		GIFT_KIND_BRAND,
	}
)

_GIFT_LINE_SOURCES = frozenset({DISCOUNT_SOURCE_GWP, DISCOUNT_SOURCE_FREE_ITEM})

def allow_free_gift_with_ff() -> bool:
	"""Global Settings checkbox (default off)."""
	return bool(cint(get_settings().get("allow_free_gift_with_ff")))


def classify_gift_rule(rule) -> str | None:
	"""Map a Pricing Rule / rule_map row to a gift kind, or ``None`` if not a gift."""
	kind = classify_promotion(rule)
	return kind if kind == GIFT_KIND_BXGY or is_gift_kind(kind) else None


def scheme_blocking_kind(rule) -> str | None:
	"""``bxgy`` / ``item_level`` when lines under this rule must not get F&F."""
	rule = _rule_values(rule)
	if not rule:
		return None
	if cint(rule.get("coupon_code_based")):
		# Coupons keep the best-discount comparison.
		return None
	kind = classify_promotion(rule)
	return kind if kind in (SCHEME_KIND_BXGY, SCHEME_KIND_ITEM_LEVEL) else None


def _split_rule_names(raw) -> list[str]:
	if not raw:
		return []
	if isinstance(raw, list | tuple | set):
		return [cstr(r) for r in raw if cstr(r)]
	text = cstr(raw).strip()
	if text.startswith("["):
		text = text.strip("[]").replace('"', "").replace("'", "")
	return [part.strip() for part in text.split(",") if part.strip()]


def _item_rule_names(item) -> list[str]:
	return _split_rule_names(item.get("pricing_rules"))


def _set(item, key, value) -> None:
	if isinstance(item, dict):
		item[key] = value
	elif hasattr(item, "set"):
		item.set(key, value)
	else:
		setattr(item, key, value)


def _line_qty(item) -> float:
	return flt(item.get("qty") or item.get("quantity") or 0)


def _rule_scope_matches(item, rule_name) -> bool:
	from posnext_promotions.api.gwp import item_matches_pricing_rule_apply_on

	try:
		full_rule = frappe.get_cached_doc("Pricing Rule", rule_name)
	except frappe.DoesNotExistError:
		return False
	return item_matches_pricing_rule_apply_on(item, full_rule)


def line_blocked_by_scheme(item) -> list[str]:
	"""Rule names that make this line ineligible for F&F (BXGY / Routine).

	The verdict :func:`ff_compatibility.block_ineligible_lines` signed for the line as
	it is now. Otherwise (never judged, changed since, or a client-sent
	``fnf_blocked_by_scheme``) the full judge runs again on a copy, without a doc:
	the line's tags *and* every untagged BXGY whose scope covers it, any company,
	valid today — never narrower than the doc judge (Rulings 119, 121, 122).
	"""
	from posnext_promotions.promotions import ff_compatibility

	if cint(item.get("is_free_item")):
		return []
	verdict = ff_compatibility.judged_verdict(item)
	if verdict is not None:
		return verdict
	copy = frappe._dict(item.as_dict() if callable(getattr(item, "as_dict", None)) else dict(item))
	ff_compatibility.block_ineligible_lines([copy])
	return _split_rule_names(copy.get(FIELD_FNF_BLOCKED_BY))


def mark_fnf_ineligible_lines(prepared_items, rule_map) -> set[int]:
	"""Wrapper: see :func:`ff_compatibility.block_ineligible_lines`. Returns the positions flagged."""
	from posnext_promotions.promotions import ff_compatibility

	return ff_compatibility.block_ineligible_lines(prepared_items, rule_map or {})


def fnf_stamped_indices(prepared_items) -> set[int]:
	return {
		idx
		for idx, item in enumerate(prepared_items or [])
		if not cint(item.get("is_free_item"))
		and cstr(item.get("discount_source")) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY
	}


def _rule_name_from_free_key(key) -> str:
	if isinstance(key, tuple | list) and len(key) > 1:
		return cstr(key[1])
	return ""


def _clear_line_gift_stamp(item, rule_names=None) -> None:
	"""Undo a paid-line free-unit / GWP stamp so F&F can reclaim the line.

	``rule_names``: the gift rules to detach; the line keeps its other rules
	(e.g. a Price rule). ``None`` detaches every rule.
	"""
	price_list_rate = flt(item.get("price_list_rate") or item.get("rate") or 0)
	drop = None if rule_names is None else set(rule_names)
	kept = [] if drop is None else [name for name in _item_rule_names(item) if name not in drop]
	for key, value in (
		("discount_amount", 0),
		("discount_percentage", 0),
		("free_qty", 0),
		("gwp_free_qty", 0),
		("discount_source", None),
		("rate", price_list_rate),
		("pricing_rules", ",".join(kept)),
	):
		_set(item, key, value)


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
	fnf_indices,
	allow_free_gift: bool | None = None,
	prepared_items,
	free_items_map: dict,
	rule_map: dict,
	applied_rules: set | None = None,
) -> set[str]:
	"""Remove gifts that may not coexist with F&F (setting off). BXGY is never removed.

	``fnf_indices``: paid lines that received F&F *before* the gift passes ran.
	Returns the Pricing Rule names suppressed.
	"""
	from posnext_promotions.promotions import ff_compatibility

	fnf_indices = set(fnf_indices or ())
	if not fnf_indices:
		return set()
	if allow_free_gift is None:
		allow_free_gift = allow_free_gift_with_ff()
	if allow_free_gift:
		return set()
	keys = ff_compatibility.row_key_list(prepared_items)
	fnf_keys = {keys[idx] for idx in fnf_indices if idx < len(keys)}
	snapshot = ff_compatibility.normalize(prepared_items, free_items_map)
	suppressed, dropped = ff_compatibility._decide_gifts(snapshot, fnf_keys, rule_map or {})
	for gift in dropped:
		free_items_map.pop(gift.ref, None)
	if suppressed and applied_rules is not None:
		still_used = {rule for (_code, rule) in free_items_map}
		applied_rules.difference_update(suppressed - still_used)
	return suppressed
