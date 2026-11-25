# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""F&F vs scheme / free-gift coexistence (Miraaya rules, Malak 9 Nov + meeting 16 Nov).

Rules
-----
* **Routine** (Promotional Scheme of type *Item Level Discount*) lines are *not
  eligible* for F&F, whatever the setting below: they keep their item-level
  discount. **Buy X Get Y (BXGY)** lines are not eligible either while *Allow
  Free Gift with F&F* is off. Other cart lines still get F&F. See
  :func:`ff_compatibility.block_ineligible_lines` and :func:`line_blocked_by_scheme`.
  BXGY covers (:func:`ff_classifier.classify_promotion`):

  - Product schemes with a same-item free row (Buy 1 Get 1, B2G1, …), even
    when labelled GWP;
  - Price Discount schemes that give 100% off (or Rate 0) a limited number of
    units (Min/Max price + a discounted-qty limit, e.g. dev-miraaya "buy one"),
    whatever their Promotion Type label (:func:`is_price_bxgy_rule`).
* **Allow Free Gift with F&F** (Friends and Family Settings, global, default
  off) controls Free Item, GWP, Gift Pool, Cart Total Gift and Brand Gift:

  - checked   → the gift and F&F both apply;
  - unchecked → a gift whose triggering paid line(s) received F&F is not
    awarded (GWP line discounts are not stamped on F&F lines). A cart-total
    (Transaction) gift is removed when F&F applied anywhere on the cart; its
    threshold is evaluated on the subtotal *after* F&F either way.

  - BXGY (Task 8s): checked → the BXGY line gets F&F *and* keeps its free row;
    unchecked → the line gets no F&F (as above). The BXGY free row is never
    dropped by the gift policy. A Price "buy one" has no free row: checked, its
    line enters the usual best-discount comparison with F&F.
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
	"""Global Settings checkbox (default off), read where the BXGY judge reads it (J1)."""
	from posnext_promotions.promotions import ff_config

	return bool(ff_config.resolve().allow_free_gift_with_ff.value)


def classify_gift_rule(rule) -> str | None:
	"""Map a Pricing Rule / rule_map row to a gift kind, or ``None`` if not a gift."""
	kind = classify_promotion(rule)
	return kind if kind == GIFT_KIND_BXGY or is_gift_kind(kind) else None


def scheme_blocking_kind(rule) -> str | None:
	"""``bxgy`` / ``item_level`` when lines under this rule must not get F&F.

	The one switch every judge reads (cart, F&F-only call, invoice validate, paid
	freeze, ``fnf_line_rates``). Item Level always blocks; BXGY blocks only while
	*Allow Free Gift with F&F* is off (Task 8s).
	"""
	from posnext_promotions.promotions import ff_config

	rule = _rule_values(rule)
	if not rule:
		return None
	if cint(rule.get("coupon_code_based")):
		# Coupons keep the best-discount comparison.
		return None
	kind = classify_promotion(rule)
	if kind == SCHEME_KIND_BXGY:
		return None if ff_config.resolve().allow_free_gift_with_ff.value else kind
	return kind if kind == SCHEME_KIND_ITEM_LEVEL else None


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


_SCOPE_MEMO = "posnext_ff_rule_scope"
_APPLY_ON_FIELDS = {"Item Code": "item_code", "Item Group": "item_group", "Brand": "brand"}


def rule_scope(rule_name) -> frappe._dict | None:
	"""A Pricing Rule's scope, its item group tree expanded once per request.

	``field`` / ``members``: the line field and values of the rule's own ``apply_on``
	(``field`` None for a Transaction rule: every line). ``other_field`` /
	``other_value``: ``apply_rule_on_other``'s, as ERPNext's
	``get_pricing_rule_items(other_items=True)`` reads it (one value, not expanded).
	Memoized on ``frappe.local`` by rule name and ``modified``: the untagged judge asks
	it of every blocking rule for every line. None for a missing rule.
	"""
	try:
		doc = frappe.get_cached_doc("Pricing Rule", rule_name)
	except frappe.DoesNotExistError:
		return None
	modified = cstr(doc.get("modified"))
	if not modified:
		# Never saved (no version to key on): not memoized.
		return _build_scope(doc)
	memo = getattr(frappe.local, _SCOPE_MEMO, None)
	if memo is None:
		memo = {}
		setattr(frappe.local, _SCOPE_MEMO, memo)
	key = (cstr(rule_name), modified)
	if key not in memo:
		memo[key] = _build_scope(doc)
	return memo[key]


def _build_scope(doc) -> frappe._dict:
	from posnext_promotions.api.gwp import _pricing_rule_apply_on_values

	apply_on = cstr(doc.get("apply_on")).strip()
	other_field = _APPLY_ON_FIELDS.get(cstr(doc.get("apply_rule_on_other")).strip())
	return frappe._dict(
		field=None if apply_on == "Transaction" else _APPLY_ON_FIELDS.get(apply_on, ""),
		members=frozenset(_pricing_rule_apply_on_values(doc)),
		other_field=other_field,
		other_value=cstr(doc.get(f"other_{other_field}")) if other_field else "",
	)


def _rule_scope_matches(item, rule_name) -> bool:
	"""The line is in the rule's own ``apply_on`` scope (:func:`rule_scope`)."""
	scope = rule_scope(rule_name)
	if scope is None:
		return False
	if scope.field is None:
		return True
	return bool(scope.field) and cstr(item.get(scope.field)) in scope.members


def rule_other_scope_matches(item, rule_name) -> bool:
	"""The line is the rule's ``apply_rule_on_other`` target (ERPNext prices it)."""
	scope = rule_scope(rule_name)
	return bool(scope and scope.other_field and scope.other_value) and (
		cstr(item.get(scope.other_field)) == scope.other_value
	)


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
	"""Remove gifts that may not coexist with F&F (setting off). BXGY is never removed (its
	lines get no F&F while the setting is off).

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
