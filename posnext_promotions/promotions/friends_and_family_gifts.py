# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""F&F vs scheme / free-gift coexistence (Miraaya rules, Malak 9 Nov + meeting 16 Nov / 17 Nov).

Rules
-----
* **Routine** (Promotional Scheme of type *Item Level Discount*) lines are never
  eligible for F&F, regardless of Settings. They keep their item-level discount.
  Other cart lines still get F&F. See :func:`mark_fnf_ineligible_lines` and
  :func:`line_blocked_by_scheme`.
* **Allow Free Gift with F&F** (Friends and Family Settings, global, default
  off) controls Free Item, GWP, Gift Pool, Cart Total Gift, Brand Gift, **and
  Buy X Get Y** (Product same-item free rows, and Price Discount schemes that
  give 100% off a limited number of units, e.g. "buy one"):

  - unchecked → a gift whose triggering paid line(s) received F&F is not
    awarded (GWP line discounts are not stamped on F&F lines). A cart-total
    (Transaction) gift is removed when F&F applied anywhere on the cart; its
    threshold is evaluated on the subtotal *after* F&F either way. BXGY lines
    never receive F&F — they keep their promotion.
  - checked   → those gifts and F&F both apply. A BXGY line is compared with
    F&F on effective discount % (promo discount including any same-item free
    row, divided by the gross of paid + free units). If that % is *strictly
    less* than the F&F percentage, the promo is removed (free row / unit
    discount dropped) and the paid qty takes F&F, with no stacking. Otherwise
    the promo stays and the line gets no F&F. This is the same "best discount,
    no stacking" idea, except a tie keeps the promo.

* The hidden per-scheme ``apply_with_ff`` field is left untouched (reserved for a
  future per-promotion override).
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr, flt

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

PROMOTION_TYPE_GWP = "GWP"
PROMOTION_TYPE_GIFT_POOL = "Gift Pool"
PROMOTION_TYPE_ITEM_LEVEL = "Item Level Discount"
ACCUMULATIVE_MODE = "Accumulative"
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


def allow_free_gift_with_ff() -> bool:
	"""Global Settings checkbox (default off)."""
	return bool(cint(get_settings().get("allow_free_gift_with_ff")))


def _rule_values(rule_or_name):
	"""Rule row with the fields classification needs (loads from DB when missing)."""
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
	cache = getattr(frappe.local, "_fnf_rule_values_cache", None)
	if cache is None:
		cache = {}
		frappe.local._fnf_rule_values_cache = cache
	if name not in cache:
		values = None
		if frappe.db.exists("Pricing Rule", name):
			values = frappe.db.get_value("Pricing Rule", name, list(_RULE_FIELDS), as_dict=True)
		cache[name] = values
	values = cache[name]
	if values is None:
		return row
	merged = frappe._dict(values)
	if row is not None:
		for key, value in dict(row).items():
			merged.setdefault(key, value)
	return merged


def clear_rule_cache() -> None:
	if hasattr(frappe.local, "_fnf_rule_values_cache"):
		frappe.local._fnf_rule_values_cache = {}


def classify_gift_rule(rule) -> str | None:
	"""Map a Pricing Rule / rule_map row to a gift kind, or ``None`` if not a gift."""
	rule = _rule_values(rule)
	if not rule:
		return None

	promo_type = cstr(rule.get("promotion_type") or "").strip()
	is_product = cstr(rule.get("price_or_product_discount") or "") == "Product"
	# Buy X Get Y first — even if the scheme is labelled GWP (common for B2G1).
	if is_product and cint(rule.get("same_item")):
		return GIFT_KIND_BXGY

	if promo_type == PROMOTION_TYPE_GWP:
		return GIFT_KIND_GWP
	if promo_type == PROMOTION_TYPE_GIFT_POOL:
		return GIFT_KIND_FREE_ITEM

	if not is_product:
		return None

	apply_on = cstr(rule.get("apply_on") or "").strip()
	if apply_on == "Transaction":
		return GIFT_KIND_CART_TOTAL
	if apply_on == "Brand":
		return GIFT_KIND_BRAND
	return GIFT_KIND_FREE_ITEM


def is_price_bxgy_rule(rule) -> bool:
	"""Price Discount that gives 100% off a limited number of units (e.g. "buy one").

	Identified as a Min/Max rule with a positive qty limit and a 100% (or free
	Rate) discount on those units — not a flat Item Level (Routine) percentage.
	"""
	rule = _rule_values(rule)
	if not rule:
		return False
	if cstr(rule.get("price_or_product_discount") or "Price") != "Price":
		return False
	if cstr(rule.get("apply_discount_on_price")) not in ("Min", "Max"):
		return False
	if flt(rule.get("min_or_max_discount_qty_limit")) <= 0:
		return False
	kind = cstr(rule.get("rate_or_discount") or "Discount Percentage")
	if kind == "Discount Percentage":
		return abs(flt(rule.get("discount_percentage")) - 100.0) < 0.0001
	if kind == "Rate":
		return flt(rule.get("rate")) <= 0
	return False


def scheme_blocking_kind(rule) -> str | None:
	"""``bxgy`` / ``item_level`` when lines under this rule must not get F&F."""
	rule = _rule_values(rule)
	if not rule:
		return None
	if cint(rule.get("coupon_code_based")):
		# Coupons keep the best-discount comparison.
		return None
	if classify_gift_rule(rule) == GIFT_KIND_BXGY or is_price_bxgy_rule(rule):
		return SCHEME_KIND_BXGY
	if (
		cstr(rule.get("promotion_type")).strip() == PROMOTION_TYPE_ITEM_LEVEL
		and cstr(rule.get("price_or_product_discount") or "Price") == "Price"
		and cstr(rule.get("apply_on")).strip() != "Transaction"
		and cstr(rule.get("apply_discount_on_price")) != ACCUMULATIVE_MODE
	):
		return SCHEME_KIND_ITEM_LEVEL
	return None


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
	"""Rule names that make this line ineligible for F&F (BXGY / Routine)."""
	if cint(item.get("is_free_item")):
		return []
	names = _split_rule_names(item.get(FIELD_FNF_BLOCKED_BY))
	for rule_name in _item_rule_names(item):
		if rule_name not in names and scheme_blocking_kind(rule_name):
			names.append(rule_name)
	return names


def mark_fnf_ineligible_lines(prepared_items, rule_map) -> set[int]:
	"""Flag paid lines matched by a BXGY / Routine scheme before F&F runs.

	BXGY free units are valued *after* F&F, so the paid line can look
	undiscounted at that point (the qty-4 switch to F&F). A line is flagged
	when the BXGY rule's scope covers it and the line qty reaches the rule's
	``min_qty`` — independently of whether the free units were already stamped.
	Routine (item-level) lines carry the rule in ``pricing_rules`` already.

	Returns the indices flagged.
	"""
	bxgy_rules = [name for name, row in (rule_map or {}).items() if scheme_blocking_kind(row or name) == SCHEME_KIND_BXGY]
	flagged: set[int] = set()
	for idx, item in enumerate(prepared_items or []):
		if cint(item.get("is_free_item")):
			_set(item, FIELD_FNF_BLOCKED_BY, "")
			continue
		names: list[str] = [
			rule_name for rule_name in _item_rule_names(item) if scheme_blocking_kind(rule_map.get(rule_name) or rule_name)
		]
		qty = _line_qty(item)
		for rule_name in bxgy_rules:
			if rule_name in names:
				continue
			row = _rule_values(rule_map.get(rule_name) or rule_name) or {}
			min_qty = flt(row.get("min_qty"))
			if qty <= 0 or (min_qty and qty < min_qty):
				continue
			if _rule_scope_matches(item, rule_name):
				names.append(rule_name)
		_set(item, FIELD_FNF_BLOCKED_BY, ",".join(names))
		if names:
			flagged.add(idx)
	return flagged


def fnf_stamped_indices(prepared_items) -> set[int]:
	return {
		idx
		for idx, item in enumerate(prepared_items or [])
		if not cint(item.get("is_free_item"))
		and cstr(item.get("discount_source")) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY
	}


_BXGY_SNAPSHOT_KEYS = (
	"discount_source",
	"discount_percentage",
	"discount_amount",
	"rate",
	"amount",
	"pricing_rules",
	"applied_promotional_schemes",
	FIELD_FNF_BLOCKED_BY,
	"is_already_discounted",
	"free_qty",
	"gwp_free_qty",
)


def line_bxgy_rules(item, rule_map=None) -> list[str]:
	"""BXGY Pricing Rule names flagged on or attached to this paid line."""
	names: list[str] = []
	# Preserve flag-then-pricing_rules order without duplicates.
	ordered = _split_rule_names(item.get(FIELD_FNF_BLOCKED_BY)) + [
		n for n in _item_rule_names(item) if n not in _split_rule_names(item.get(FIELD_FNF_BLOCKED_BY))
	]
	for name in ordered:
		if name in names:
			continue
		row = (rule_map or {}).get(name) or name
		if scheme_blocking_kind(row) == SCHEME_KIND_BXGY:
			names.append(name)
	return names


def matching_bxgy_free_rows(item, bxgy_names, free_items_map) -> list[tuple]:
	"""``(free_items_map key, row)`` same-item free rows granted by these BXGY rules."""
	code = cstr(item.get("item_code"))
	found: list[tuple] = []
	for key, row in list((free_items_map or {}).items()):
		rule_name = _rule_name_from_free_key(key)
		if not rule_name or rule_name not in bxgy_names:
			continue
		row_code = cstr(row.get("item_code") if row is not None else "")
		key_code = cstr(key[0]) if isinstance(key, tuple | list) and key else ""
		if row_code == code or key_code == code:
			found.append((key, row))
	return found


def bxgy_promo_breakdown(item, free_rows=None) -> tuple[float, float, float]:
	"""Promo money, gross of paid + free units, and effective discount %.

	The numerator is the paid-line promo discount plus the list value of any
	same-item free rows. The denominator is the list value of every unit
	(paid + free). This is the best-discount comparison used when Allow Free
	Gift with F&F is on.
	"""
	price = flt(item.get("price_list_rate") or item.get("rate") or 0)
	paid_qty = _line_qty(item)
	free_value = 0.0
	for row in free_rows or []:
		qty = _line_qty(row)
		row_price = flt(row.get("price_list_rate") or 0)
		if row_price <= 0:
			row_price = price
		free_value += row_price * qty
	paid_gross = price * paid_qty
	gross = paid_gross + free_value
	pct = flt(item.get("discount_percentage") or 0)
	if pct and paid_gross:
		line_discount = paid_gross * pct / 100.0
	else:
		discount_amount = flt(item.get("discount_amount") or 0)
		# Min/Max materialises a per-unit discount_amount when the blended
		# percentage is missing; a value no larger than the unit price on a
		# multi-qty line is treated as per-unit.
		if paid_qty > 1 and discount_amount > 0 and price > 0 and discount_amount <= price + 1e-6:
			line_discount = discount_amount * paid_qty
		else:
			line_discount = min(discount_amount, paid_gross) if paid_gross else discount_amount
	promo = line_discount + free_value
	if gross <= 0:
		return 0.0, 0.0, 0.0
	return promo, gross, promo / gross * 100.0


def bxgy_effective_discount_percent(item, free_rows=None) -> float:
	"""Effective promo % on a BXGY line after the free item / unit discount."""
	return bxgy_promo_breakdown(item, free_rows)[2]


def _snapshot_bxgy_item(item) -> dict:
	return {key: item.get(key) for key in _BXGY_SNAPSHOT_KEYS}


def _restore_bxgy_item(item, snapshot) -> None:
	for key, value in (snapshot or {}).items():
		_set(item, key, value)


def _clear_bxgy_promo(item, free_keys, free_items_map) -> None:
	"""Drop the BXGY discount / same-item free rows so F&F can take the paid qty."""
	_clear_line_gift_stamp(item)
	if item.get("applied_promotional_schemes"):
		_set(item, "applied_promotional_schemes", [])
	_set(item, "is_already_discounted", 0)
	_set(item, FIELD_FNF_BLOCKED_BY, "")
	for key in free_keys:
		(free_items_map or {}).pop(key, None)


def strip_weaker_bxgy_promos(
	*,
	prepared_items,
	free_items_map: dict,
	rule_map: dict | None = None,
	applied_rules: set | None = None,
	fnf_percentage: float | None = None,
	allow_free_gift: bool | None = None,
) -> list[dict]:
	"""Strip a BXGY promo whose effective % is strictly below the F&F %.

	No-op when Allow Free Gift with F&F is off (BXGY stays blocked and keeps
	the promo). Routine lines are never touched. Returns snapshots so the
	caller can restore the promo if F&F does not actually stamp the line
	(coverage / caps).
	"""
	if allow_free_gift is None:
		allow_free_gift = allow_free_gift_with_ff()
	if not allow_free_gift:
		return []
	if fnf_percentage is None:
		fnf_percentage = flt(get_settings().get("discount_percentage"))
	fnf_percentage = flt(fnf_percentage)
	if fnf_percentage <= 0:
		return []

	snapshots: list[dict] = []
	for idx, item in enumerate(prepared_items or []):
		if cint(item.get("is_free_item")):
			continue
		names = line_bxgy_rules(item, rule_map)
		if not names:
			continue
		matches = matching_bxgy_free_rows(item, names, free_items_map)
		free_rows = [row for _key, row in matches]
		promo, gross, effective = bxgy_promo_breakdown(item, free_rows)
		# Tie or better promo keeps the scheme — F&F is not applied.
		if gross <= 0 or effective >= fnf_percentage:
			continue
		snapshots.append(
			{
				"idx": idx,
				"item": _snapshot_bxgy_item(item),
				"free": [(key, frappe._dict(dict(row))) for key, row in matches],
				"bxgy_names": names,
				"promo_amount": promo,
				"applied": [name for name in names if applied_rules is not None and name in applied_rules],
			}
		)
		_clear_bxgy_promo(item, [key for key, _row in matches], free_items_map)

	if applied_rules is not None and snapshots:
		still_used: set[str] = set()
		for item in prepared_items or []:
			still_used.update(_item_rule_names(item))
		still_used.update(_rule_name_from_free_key(key) for key in (free_items_map or {}))
		for snap in snapshots:
			for name in snap["bxgy_names"]:
				if name not in still_used:
					applied_rules.discard(name)
	return snapshots


def restore_bxgy_when_fnf_does_not_beat(
	snapshots,
	prepared_items,
	free_items_map: dict,
	applied_rules: set | None = None,
	ff_result: dict | None = None,
) -> bool:
	"""Put the BXGY promo back when F&F did not stamp the line after the strip.

	Coverage, exclusions or a fully exhausted cap can leave the line with no
	F&F; the customer should keep the original promo in that case.
	"""
	if not snapshots:
		return False
	ff_by_idx = {line.get("idx"): line for line in (ff_result or {}).get("items") or []}
	restored = False
	for snap in snapshots:
		idx = snap["idx"]
		if idx is None or idx >= len(prepared_items or []):
			continue
		item = prepared_items[idx]
		ff_line = ff_by_idx.get(idx) or {}
		if ff_line.get("applies") and cstr(item.get("discount_source")) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
			continue
		_restore_bxgy_item(item, snap["item"])
		for key, row in snap.get("free") or []:
			free_items_map[key] = row
		if applied_rules is not None:
			applied_rules.update(snap.get("applied") or [])
		restored = True
	return restored


def _rule_name_from_free_key(key) -> str:
	if isinstance(key, tuple | list) and len(key) > 1:
		return cstr(key[1])
	return ""


def _clear_line_gift_stamp(item) -> None:
	"""Undo a paid-line free-unit / GWP stamp so F&F can reclaim the line."""
	price_list_rate = flt(item.get("price_list_rate") or item.get("rate") or 0)
	for key, value in (
		("discount_amount", 0),
		("discount_percentage", 0),
		("free_qty", 0),
		("gwp_free_qty", 0),
		("discount_source", None),
		("rate", price_list_rate),
		("pricing_rules", ""),
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


def _trigger_got_fnf(rule_name, prepared_items, fnf_indices) -> bool:
	for idx in fnf_indices:
		if idx >= len(prepared_items):
			continue
		item = prepared_items[idx]
		if rule_name in _item_rule_names(item) or _rule_scope_matches(item, rule_name):
			return True
	return False


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
	fnf_indices = set(fnf_indices or ())
	if not fnf_indices:
		return set()
	if allow_free_gift is None:
		allow_free_gift = allow_free_gift_with_ff()
	if allow_free_gift:
		return set()

	suppressed: set[str] = set()
	for key in list(free_items_map.keys()):
		rule_name = _rule_name_from_free_key(key)
		kind = classify_gift_rule(rule_map.get(rule_name) or rule_name) if rule_name else GIFT_KIND_FREE_ITEM
		if kind not in CONFIGURABLE_GIFT_KINDS:
			continue
		if kind == GIFT_KIND_CART_TOTAL or not rule_name or _trigger_got_fnf(rule_name, prepared_items, fnf_indices):
			free_items_map.pop(key, None)
			if rule_name:
				suppressed.add(rule_name)

	# GWP / free-unit stamps placed on a line that had F&F (gift passes run after F&F).
	for idx in fnf_indices:
		if idx >= len(prepared_items):
			continue
		item = prepared_items[idx]
		if not _line_has_gift_stamp(item):
			continue
		rule_names = _item_rule_names(item)
		kinds = {classify_gift_rule(rule_map.get(n) or n) for n in rule_names}
		kinds.discard(None)
		if not kinds and cstr(item.get("discount_source")) in _GIFT_LINE_SOURCES:
			kinds = {GIFT_KIND_FREE_ITEM}
		if kinds & CONFIGURABLE_GIFT_KINDS and GIFT_KIND_BXGY not in kinds:
			suppressed.update(rule_names)
			_clear_line_gift_stamp(item)

	if suppressed:
		# Detach the dropped gift rules from their trigger lines so the POS does
		# not show them as applied without a gift.
		for item in prepared_items or []:
			if cint(item.get("is_free_item")):
				continue
			names = _item_rule_names(item)
			kept = [n for n in names if n not in suppressed]
			if len(kept) != len(names):
				_set(item, "pricing_rules", ",".join(kept))
		if applied_rules is not None:
			still_used = {rule for (_code, rule) in free_items_map}
			applied_rules.difference_update(suppressed - still_used)

	return suppressed
