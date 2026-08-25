# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""POS promotional apply_offers engine (standalone; no pos_next imports)."""

from __future__ import unicode_literals

import json
import math
from functools import lru_cache

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import cint, cstr, flt, nowdate, nowtime

from posnext_promotions.promotions.engine import (
	CROSS_CART_MODES,
	append_pricing_rule,
	build_matrix_type_map,
	filter_auto_discount_from_header,
	remove_pricing_rule,
	rule_promotion_type,
	run_line_discount_passes,
)
from posnext_promotions.promotions.schedule import (
	filter_rules_by_schedule,
	get_inactive_rules,
	resolve_moment,
)

try:
	from erpnext.accounts.doctype.pricing_rule.pricing_rule import (
		apply_pricing_rule as erpnext_apply_pricing_rule,
	)
	from erpnext.accounts.doctype.pricing_rule.utils import (
		apply_pricing_rule_on_transaction as erpnext_apply_pricing_rule_on_transaction,
	)
	from erpnext.accounts.doctype.pricing_rule.utils import (
		get_applied_pricing_rules as erpnext_get_applied_pricing_rules,
	)
	from posnext_promotions.overrides.pricing_rule import apply_min_max_price_discounts
	from posnext_promotions.api.promotion_exclusions import (
		DISCOUNT_SOURCE_AUTO,
		DISCOUNT_SOURCE_FREE_ITEM,
		DISCOUNT_SOURCE_GWP,
		DISCOUNT_SOURCE_MANUAL,
		PROMOTION_TYPE_AUTO,
		PROMOTION_TYPE_ITEM_LEVEL,
		get_rule_promotion_types,
		mark_item_discount_flags,
	)
	from posnext_promotions.api.gwp import (
		GWP_BASIS_MAX,
		PROMOTION_TYPE_GWP,
		calculate_gwp_discount_amount,
		distribute_gwp_free_units_for_basis,
		get_gwp_same_item_free_qty,
		get_gwp_slab_free_qty,
		item_matches_pricing_rule_apply_on,
		should_aggregate_gwp_quantities,
	)
	from posnext_promotions.api.product_free import (
		compute_product_recursive_free_qty,
		should_aggregate_product_quantities,
	)
	from posnext_promotions.api.gift_pool import (
		PROMOTION_TYPE_GIFT_POOL,
		allocate_gift_pool_free_items,
		expanded_groups,
		get_scheme_gift_pool_qtys,
		get_scheme_gift_pools,
	)
except Exception:  # pragma: no cover
	import traceback

	frappe.log_error(traceback.format_exc(), "POSNext Promotions pricing imports failed")
	erpnext_apply_pricing_rule = None
	erpnext_get_applied_pricing_rules = None
	erpnext_apply_pricing_rule_on_transaction = None
	apply_min_max_price_discounts = None
	PROMOTION_TYPE_AUTO = "Auto Discount"
	PROMOTION_TYPE_ITEM_LEVEL = "Item Level Discount"
	PROMOTION_TYPE_GWP = "GWP"
	PROMOTION_TYPE_GIFT_POOL = "Gift Pool"
	calculate_gwp_discount_amount = None
	get_gwp_slab_free_qty = None
	get_gwp_same_item_free_qty = None
	distribute_gwp_free_units_for_basis = None
	should_aggregate_gwp_quantities = None
	item_matches_pricing_rule_apply_on = None
	compute_product_recursive_free_qty = None
	should_aggregate_product_quantities = None
	GWP_BASIS_MAX = "Max Price"
	DISCOUNT_SOURCE_AUTO = "auto_discount"
	DISCOUNT_SOURCE_GWP = "gwp"
	DISCOUNT_SOURCE_FREE_ITEM = "free_item"
	DISCOUNT_SOURCE_MANUAL = "manual_discount"
	get_rule_promotion_types = None
	mark_item_discount_flags = None
	allocate_gift_pool_free_items = None
	get_scheme_gift_pools = None
	get_scheme_gift_pool_qtys = None
	expanded_groups = None


def _item_matches_pricing_rule(item, rule) -> bool:
	if not item_matches_pricing_rule_apply_on:
		return False
	return item_matches_pricing_rule_apply_on(item, rule)


def _item_matches_gwp_rule(item, full_rule) -> bool:
	return not item.get("is_free_item") and _item_matches_pricing_rule(item, full_rule)


def _item_pricing_rule_names(item) -> list[str]:
	raw_rules = item.get("pricing_rules") or ""
	if erpnext_get_applied_pricing_rules:
		return erpnext_get_applied_pricing_rules(raw_rules)
	if isinstance(raw_rules, str):
		if raw_rules.startswith("["):
			return json.loads(raw_rules)
		return [part.strip() for part in raw_rules.split(",") if part.strip()]
	if isinstance(raw_rules, list | tuple | set):
		return [cstr(part) for part in raw_rules if cstr(part)]
	return []


def _floor_free_item_qty(qty) -> int:
	"""Free gift lines are always whole units (floor, never round up)."""
	return max(0, int(math.floor(flt(qty))))


def _stamp_gwp_line_discount(item_doc, line_free_qty, price_list_rate) -> None:
	line_free_qty = _floor_free_item_qty(line_free_qty)
	purchased_qty = flt(item_doc.get("qty") or item_doc.get("quantity") or 0)
	price_list_rate = flt(price_list_rate)
	line_discount = calculate_gwp_discount_amount(line_free_qty, price_list_rate)
	if line_discount <= 0 or purchased_qty <= 0:
		return

	item_doc.discount_amount = line_discount
	item_doc.discount_percentage = 0
	item_doc.gwp_free_qty = line_free_qty
	item_doc.free_qty = line_free_qty
	item_doc.discount_source = DISCOUNT_SOURCE_GWP
	item_doc.rate = flt(price_list_rate - (line_discount / purchased_qty))


def _stamp_bundled_same_item_free_discount(item_doc, line_free_qty, price_list_rate) -> None:
	"""Apply same-item free gift on the purchased line (1 of N units free, no extra row)."""
	line_free_qty = _floor_free_item_qty(line_free_qty)
	purchased_qty = flt(item_doc.get("qty") or item_doc.get("quantity") or 0)
	price_list_rate = flt(price_list_rate)
	line_discount = calculate_gwp_discount_amount(line_free_qty, price_list_rate)
	if line_discount <= 0 or purchased_qty <= 0:
		return

	item_doc.discount_amount = line_discount
	item_doc.discount_percentage = 0
	item_doc.gwp_free_qty = 0
	item_doc.free_qty = line_free_qty
	item_doc.discount_source = DISCOUNT_SOURCE_FREE_ITEM
	item_doc.rate = flt(price_list_rate - (line_discount / purchased_qty))


def _make_free_item_doc(item_code, qty, rule_name, full_rule, promotional_scheme=None, **extra):
	"""Build a free-item payload for ``free_items_map``."""
	item_data = frappe.get_cached_value(
		"Item", item_code, ["item_name", "description", "stock_uom"], as_dict=1
	) or {}
	uom = full_rule.free_item_uom or item_data.get("stock_uom") or "Nos"
	payload = {
		"item_code": item_code,
		"item_name": item_data.get("item_name") or item_code,
		"description": item_data.get("description"),
		"qty": _floor_free_item_qty(qty),
		"pricing_rules": rule_name,
		"rate": flt(full_rule.free_item_rate or 0),
		"price_list_rate": flt(full_rule.free_item_rate or 0),
		"is_free_item": 1,
		"uom": uom,
		"stock_uom": item_data.get("stock_uom") or uom,
		"applied_promotional_scheme": promotional_scheme,
	}
	payload.update(extra)
	return frappe._dict(payload)


def _recompute_recursive_product_free_items(prepared_items, free_items_map, rule_map, applied_rules=None) -> None:
	"""Recompute recursive product discounts with POS Next rules.

	1. Same-item frees are always evaluated per cart line: each SKU that meets
	   min_qty gets free units from itself (no Min/Max Price redistribution).

	2. Other free-item + Item Group / Brand aggregates purchased qty. ERPNext
	   cannot do this for recursive rules (``mixed_conditions`` + ``is_recursive``
	   is rejected). Item Code other-item scopes evaluate each line alone and
	   sum free gifts.

	3. Same-item free qty uses the included cycle
	   (recurse_for + free_qty; qty 3 → 1 free / pay 2). Bundled on the paid
	   line as amount discount + free badge — no percentage.

	4. When free qty is 0, strip the rule from lines / applied_rules so the UI
	   does not show "Applied" without a discount (e.g. qty 2 with min_qty 2
	   but cycle needs 3).
	"""
	if not compute_product_recursive_free_qty or not should_aggregate_product_quantities:
		return

	recursive_rule_names = []
	for rule_name, details in rule_map.items():
		if rule_promotion_type(details) in (PROMOTION_TYPE_GWP, PROMOTION_TYPE_GIFT_POOL):
			continue
		full_rule = frappe.get_cached_doc("Pricing Rule", rule_name)
		if full_rule.price_or_product_discount != "Product":
			continue
		if not cint(full_rule.is_recursive):
			continue
		recursive_rule_names.append(rule_name)

	if not recursive_rule_names:
		return

	# Drop ERPNext free rows for these rules — quantities / aggregation are wrong.
	for key in list(free_items_map.keys()):
		if key[1] in recursive_rule_names:
			free_items_map.pop(key, None)

	for rule_name in recursive_rule_names:
		full_rule = frappe.get_cached_doc("Pricing Rule", rule_name)
		scheme_item_count = len(full_rule.get("items") or [])
		same_item = cint(full_rule.same_item)
		# Same-item: always per line (each SKU that meets min_qty gets its own free).
		# Never dump free units onto cheapest/most-expensive via Min/Max Price basis.
		# Other free item + Item Group/Brand: still aggregate purchased qty.
		aggregate = (
			should_aggregate_product_quantities(full_rule.apply_on, scheme_item_count)
			and not same_item
		)
		slab_free = flt(full_rule.free_qty) or 1
		recurse_for = flt(full_rule.recurse_for)
		apply_over = flt(full_rule.apply_recursion_over)
		promotional_scheme = rule_map[rule_name].get("promotional_scheme")

		matching_lines = [
			item_doc
			for item_doc in prepared_items
			if _item_matches_gwp_rule(item_doc, full_rule)
		]
		if not matching_lines:
			continue

		def _clear_rule_application():
			for item_doc in matching_lines:
				remove_pricing_rule(item_doc, rule_name)
				# Avoid "Applied" / pricing_rule badge with zero free units.
				if not (item_doc.get("pricing_rules") or "").strip():
					if item_doc.get("discount_source") in (None, "", "pricing_rule", DISCOUNT_SOURCE_FREE_ITEM):
						item_doc.discount_source = None
						item_doc.free_qty = 0
			if applied_rules is not None:
				applied_rules.discard(rule_name)

		if aggregate:
			# Other free item + Item Group/Brand: aggregate purchased qty.
			line_qtys = [
				flt(item_doc.get("qty") or item_doc.get("quantity") or 0)
				for item_doc in matching_lines
			]
			total_qty = sum(line_qtys)
			free_total = compute_product_recursive_free_qty(
				total_qty,
				slab_free,
				recurse_for,
				apply_over,
				same_item=False,
				is_recursive=True,
				min_qty=full_rule.min_qty,
				max_qty=full_rule.max_qty,
			)
			if free_total <= 0:
				_clear_rule_application()
				continue

			free_item_code = full_rule.free_item
			if not free_item_code:
				_clear_rule_application()
				continue

			for item_doc in matching_lines:
				append_pricing_rule(item_doc, rule_name)

			free_items_map[(free_item_code, rule_name)] = _make_free_item_doc(
				free_item_code,
				free_total,
				rule_name,
				full_rule,
				promotional_scheme,
			)
			continue

		# Per-line: same-item always; Item Code other-item also.
		# Each line that meets min_qty earns free from itself / its own gift count.
		line_gave_free = False
		for item_doc in matching_lines:
			line_qty = flt(item_doc.get("qty") or item_doc.get("quantity") or 0)
			free_total = compute_product_recursive_free_qty(
				line_qty,
				slab_free,
				recurse_for,
				apply_over,
				same_item=bool(same_item),
				is_recursive=True,
				min_qty=full_rule.min_qty,
				max_qty=full_rule.max_qty,
			)
			if free_total <= 0:
				remove_pricing_rule(item_doc, rule_name)
				continue

			append_pricing_rule(item_doc, rule_name)
			gift_code = item_doc.item_code if same_item else full_rule.free_item
			if not gift_code:
				remove_pricing_rule(item_doc, rule_name)
				continue

			existing = free_items_map.get((gift_code, rule_name))
			if existing:
				existing.qty = _floor_free_item_qty(flt(existing.get("qty")) + free_total)
			else:
				free_items_map[(gift_code, rule_name)] = _make_free_item_doc(
					gift_code,
					free_total,
					rule_name,
					full_rule,
					promotional_scheme,
				)
			line_gave_free = True

		if not line_gave_free and applied_rules is not None:
			applied_rules.discard(rule_name)

def _apply_bundled_same_item_free_discounts(prepared_items, free_items_map, rule_map) -> None:
	"""Bundle non-GWP same-item free gifts onto the paid line instead of a separate row."""
	if not calculate_gwp_discount_amount:
		return

	for key in list(free_items_map.keys()):
		item_code, rule_name = key
		if rule_name not in rule_map:
			continue
		if rule_promotion_type(rule_map[rule_name]) in (PROMOTION_TYPE_GWP, PROMOTION_TYPE_GIFT_POOL):
			continue

		full_rule = frappe.get_cached_doc("Pricing Rule", rule_name)
		if full_rule.price_or_product_discount != "Product" or not full_rule.same_item:
			continue

		free_item_doc = free_items_map.pop(key)
		free_qty = _floor_free_item_qty(free_item_doc.get("qty"))
		if free_qty <= 0:
			continue

		for item_doc in prepared_items:
			if item_doc.get("is_free_item"):
				continue
			if item_doc.get("item_code") != item_code:
				continue
			if not _item_matches_gwp_rule(item_doc, full_rule) and rule_name not in _item_pricing_rule_names(
				item_doc
			):
				continue

			price_list_rate = flt(
				item_doc.get("price_list_rate") or item_doc.get("rate") or 0
			)
			_stamp_bundled_same_item_free_discount(item_doc, free_qty, price_list_rate)
			append_pricing_rule(item_doc, rule_name)
			break


def _apply_gwp_line_discounts(prepared_items, free_items_map, rule_map, applied_rules=None) -> None:
	"""Apply GWP: same-SKU gifts as a free row; mixed SKUs as line discounts.

	Same item (one SKU in the cart): free units must be extra scanned items,
	not carved from min_qty. Buy 2 get 1 free requires 3 scans — 2 paid + 1
	free row with a free-item badge.

	Multi-item / item-group with mixed SKUs: total qty must fall between
	min_qty and max_qty; free_qty is discounted on cheapest (Max Price basis)
	or most expensive (Min Price basis) lines.
	"""
	if not calculate_gwp_discount_amount or not get_gwp_slab_free_qty:
		return

	gwp_rule_names = [
		name for name, details in rule_map.items() if rule_promotion_type(details) == PROMOTION_TYPE_GWP
	]

	for rule_name in gwp_rule_names:
		full_rule = frappe.get_cached_doc("Pricing Rule", rule_name)
		if full_rule.price_or_product_discount != "Product":
			continue

		for key in list(free_items_map.keys()):
			if key[1] == rule_name:
				free_items_map.pop(key, None)

		scheme_item_count = len(full_rule.get("items") or [])
		aggregate = should_aggregate_gwp_quantities(full_rule.apply_on, scheme_item_count)
		paid_qty_basis = full_rule.get("gwp_paid_qty_basis") or GWP_BASIS_MAX
		slab_free_qty = flt(full_rule.free_qty or 0)
		promotional_scheme = rule_map[rule_name].get("promotional_scheme")

		matching_lines = [
			item_doc
			for item_doc in prepared_items
			if _item_matches_pricing_rule(item_doc, full_rule)
		]
		if not matching_lines:
			continue

		paid_lines = [item_doc for item_doc in matching_lines if not item_doc.get("is_free_item")]
		if not paid_lines:
			continue

		item_codes = {cstr(item_doc.get("item_code")) for item_doc in matching_lines if item_doc.get("item_code")}
		same_item_split = len(item_codes) == 1 and bool(get_gwp_same_item_free_qty)

		if same_item_split:
			line_qtys = [
				flt(item_doc.get("qty") or item_doc.get("quantity") or 0) for item_doc in matching_lines
			]
			total_qty = sum(line_qtys)
			total_free = get_gwp_same_item_free_qty(
				slab_free_qty, total_qty, full_rule.min_qty, full_rule.max_qty
			)
			if total_free <= 0:
				for item_doc in paid_lines:
					remove_pricing_rule(item_doc, rule_name)
				if applied_rules is not None:
					applied_rules.discard(rule_name)
				continue

			gift_code = next(iter(item_codes))
			for item_doc in paid_lines:
				append_pricing_rule(item_doc, rule_name)

			free_items_map[(gift_code, rule_name)] = _make_free_item_doc(
				gift_code,
				total_free,
				rule_name,
				full_rule,
				promotional_scheme,
				discount_source=DISCOUNT_SOURCE_GWP,
				gwp_same_item_row=1,
			)
			continue

		if aggregate:
			line_qtys = [
				flt(item_doc.get("qty") or item_doc.get("quantity") or 0) for item_doc in paid_lines
			]
			line_prices = [
				flt(item_doc.get("price_list_rate") or item_doc.get("rate") or 0)
				for item_doc in paid_lines
			]
			total_qty = sum(line_qtys)
			total_free = get_gwp_slab_free_qty(
				slab_free_qty, total_qty, full_rule.min_qty, full_rule.max_qty
			)
			if total_free <= 0:
				continue

			free_per_line = distribute_gwp_free_units_for_basis(
				line_qtys, line_prices, total_free, paid_qty_basis
			)
			for item_doc, line_free in zip(paid_lines, free_per_line, strict=False):
				append_pricing_rule(item_doc, rule_name)
				if line_free <= 0:
					continue
				price_list_rate = flt(
					item_doc.get("price_list_rate") or item_doc.get("rate") or 0
				)
				_stamp_gwp_line_discount(item_doc, line_free, price_list_rate)
			continue

		if slab_free_qty <= 0:
			continue

		for item_doc in paid_lines:
			if rule_name not in _item_pricing_rule_names(item_doc) and not _item_matches_gwp_rule(
				item_doc, full_rule
			):
				continue

			line_qty = flt(item_doc.get("qty") or item_doc.get("quantity") or 0)
			line_free = get_gwp_slab_free_qty(
				slab_free_qty, line_qty, full_rule.min_qty, full_rule.max_qty
			)
			if line_free <= 0:
				continue

			price_list_rate = flt(
				item_doc.get("price_list_rate") or item_doc.get("rate") or 0
			)
			_stamp_gwp_line_discount(item_doc, line_free, price_list_rate)
			append_pricing_rule(item_doc, rule_name)


def _gift_pool_available_qty(pool_codes, warehouse, paid_items, pos_profile):
	"""Remaining warehouse qty per pool item after paid cart demand.

	Returns ``None`` when stock should not constrain gifts (negative stock
	allowed, or no warehouse). Missing keys mean unlimited stock (non-stock
	or allow-negative items).
	"""
	if not warehouse or not _should_block(pos_profile):
		return None

	allowed_negative = _get_item_negative_stock_allow_set(
		[{"item_code": code} for code in pool_codes]
		+ [{"item_code": item.get("item_code")} for item in (paid_items or [])]
	)
	paid_demand = _paid_stock_demand_by_item_warehouse(paid_items, warehouse)
	available = {}
	for code in pool_codes:
		if not code or not _item_is_stock_item(code) or code in allowed_negative:
			continue
		stock = _get_available_stock({"item_code": code, "warehouse": warehouse})
		available[code] = stock - paid_demand.get((code, warehouse), 0)
	return available


def _apply_gift_pool_free_items(
	prepared_items, free_items_map, rule_map, applied_rules=None, warehouse=None, pos_profile=None
) -> None:
	"""Replace ERPNext's single free_item with the ordered Gift Pool.

	For each configured item group: paid units are items in that group that are
	not in the pool. Any paid quantity grants a total of ``free_qty`` units
	spread across the pool item codes — not one free unit per paid unit.
	Out-of-stock pool items are skipped so the next in-stock code is granted.
	"""
	if not allocate_gift_pool_free_items or not get_scheme_gift_pools:
		return

	gift_pool_rules = [
		(name, details)
		for name, details in rule_map.items()
		if rule_promotion_type(details) == PROMOTION_TYPE_GIFT_POOL
	]
	if not gift_pool_rules:
		return

	for rule_name, details in gift_pool_rules:
		for key in list(free_items_map.keys()):
			if key[1] == rule_name:
				free_items_map.pop(key, None)

		scheme_name = details.get("promotional_scheme") if hasattr(details, "get") else getattr(
			details, "promotional_scheme", None
		)
		pools = get_scheme_gift_pools(scheme_name)
		qtys = get_scheme_gift_pool_qtys(scheme_name) if get_scheme_gift_pool_qtys else {}
		if not pools:
			if applied_rules is not None:
				applied_rules.discard(rule_name)
			continue

		full_rule = frappe.get_cached_doc("Pricing Rule", rule_name)
		promotional_scheme = scheme_name
		rule_gave_free = False

		for item_group, pool_codes in pools.items():
			group_set = expanded_groups(item_group) if expanded_groups else {item_group}
			pool_set = set(pool_codes)
			matching_lines = []
			paid_qty = 0
			for item_doc in prepared_items:
				if item_doc.get("is_free_item"):
					continue
				if cstr(item_doc.get("item_group")) not in group_set:
					continue
				if cstr(item_doc.get("item_code")) in pool_set:
					continue
				matching_lines.append(item_doc)
				paid_qty += _floor_free_item_qty(
					item_doc.get("qty") or item_doc.get("quantity") or 0
				)

			if paid_qty <= 0 or not matching_lines:
				continue

			granted = allocate_gift_pool_free_items(
				paid_qty,
				pool_codes,
				qtys.get(item_group, 1),
				available_qty=_gift_pool_available_qty(
					pool_codes, warehouse, prepared_items, pos_profile
				),
			)
			if not granted:
				continue

			for item_doc in matching_lines:
				append_pricing_rule(item_doc, rule_name)

			for gift_code, gift_qty in granted.items():
				existing = free_items_map.get((gift_code, rule_name))
				if existing:
					existing.qty = _floor_free_item_qty(flt(existing.get("qty")) + gift_qty)
				else:
					free_items_map[(gift_code, rule_name)] = _make_free_item_doc(
						gift_code,
						gift_qty,
						rule_name,
						full_rule,
						promotional_scheme,
					)
			rule_gave_free = True

		if not rule_gave_free and applied_rules is not None:
			applied_rules.discard(rule_name)


# ==========================================
# Helper Functions
# ==========================================


def _get_available_stock(item):
	"""Return available stock qty for an item row."""
	warehouse = item.get("warehouse")
	batch_no = item.get("batch_no")
	item_code = item.get("item_code")

	if not item_code or not warehouse:
		return 0

	if batch_no:
		return get_batch_qty(batch_no, warehouse) or 0

	# Get stock from Bin
	bin_qty = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")
	return flt(bin_qty) or 0


def _collect_stock_errors(items):
	"""Return list of items exceeding available stock.

	Respects per-item allow_negative_stock if the field exists on Item.
	"""
	allowed_items = _get_item_negative_stock_allow_set(items)
	errors = []
	for d in items:
		if flt(d.get("qty")) < 0:
			continue

		available = _get_available_stock(d)
		requested = flt(d.get("stock_qty") or (flt(d.get("qty")) * flt(d.get("conversion_factor") or 1)))

		if requested > available:
			if d.get("item_code") in allowed_items:
				continue
			errors.append(
				{
					"item_code": d.get("item_code"),
					"warehouse": d.get("warehouse"),
					"requested_qty": requested,
					"available_qty": available,
				}
			)

	return errors


def _item_is_stock_item(item_code):
	"""Return True when Item is stock-tracked (not a service / non-stock gift)."""
	if not item_code:
		return False
	return cint(frappe.get_cached_value("Item", item_code, "is_stock_item"))


def _paid_stock_demand_by_item_warehouse(paid_items, default_warehouse):
	"""Sum stock qty of non-free lines, keyed by (item_code, warehouse)."""
	demand = {}
	for item in paid_items or []:
		if item.get("is_free_item"):
			continue
		item_code = item.get("item_code")
		if not item_code:
			continue
		warehouse = item.get("warehouse") or default_warehouse
		qty = flt(item.get("stock_qty") or 0)
		if not qty:
			qty = flt(item.get("qty") or item.get("quantity") or 0) * flt(
				item.get("conversion_factor") or 1
			)
		if qty <= 0:
			continue
		key = (item_code, warehouse)
		demand[key] = demand.get(key, 0) + qty
	return demand


def _free_item_requested_stock_qty(free_item_doc):
	qty = flt(free_item_doc.get("qty") or free_item_doc.get("quantity") or 0)
	return qty * flt(free_item_doc.get("conversion_factor") or 1)


def _should_block(pos_profile):
	"""Return True when insufficient stock should omit free gifts.

	Honours Stock Settings, per-profile POS Settings, and the POS Profile
	custom field used by POS Next. Missing doctypes/fields default to blocking.
	"""
	allow_negative = cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock") or 0)
	if allow_negative:
		return False

	if not pos_profile:
		return True

	try:
		pos_settings_allow_negative = cint(
			frappe.db.get_value("POS Settings", {"pos_profile": pos_profile}, "allow_negative_stock") or 0
		)
		if pos_settings_allow_negative:
			return False
	except Exception:
		pass

	try:
		block_sale = cint(
			frappe.db.get_value("POS Profile", pos_profile, "posa_block_sale_beyond_available_qty") or 1
		)
		return bool(block_sale)
	except Exception:
		return True


def _filter_out_of_stock_free_items(
	free_items_map,
	warehouse,
	paid_items,
	pos_profile,
	rule_map=None,
	applied_rules=None,
	prepared_items=None,
):
	"""Drop free gifts that cannot be fulfilled from warehouse stock.

	Paid lines of the same SKU consume stock first. If remaining qty cannot
	cover the gift, it is omitted so checkout of paid items can continue.
	"""
	skipped = []
	if not free_items_map:
		return skipped
	if not _should_block(pos_profile):
		return skipped

	allowed_negative = _get_item_negative_stock_allow_set(
		[{"item_code": doc.get("item_code")} for doc in free_items_map.values()]
		+ [{"item_code": item.get("item_code")} for item in (paid_items or [])]
	)
	paid_demand = _paid_stock_demand_by_item_warehouse(paid_items, warehouse)
	leftover = {}
	skipped_rule_names = set()

	for key in list(free_items_map.keys()):
		free_doc = free_items_map[key]
		item_code = free_doc.get("item_code")
		if not item_code or not _item_is_stock_item(item_code):
			continue
		if item_code in allowed_negative:
			continue

		item_warehouse = free_doc.get("warehouse") or warehouse
		requested = _free_item_requested_stock_qty(free_doc)
		if requested <= 0:
			continue

		stock_key = (item_code, item_warehouse)
		if stock_key not in leftover:
			available = _get_available_stock(
				{
					"item_code": item_code,
					"warehouse": item_warehouse,
					"batch_no": free_doc.get("batch_no"),
				}
			)
			leftover[stock_key] = available - paid_demand.get(stock_key, 0)

		if leftover[stock_key] < requested:
			# Same-SKU GWP free units are carved from already-scanned paid qty,
			# so they do not need extra warehouse stock beyond the paid demand.
			if cint(free_doc.get("gwp_same_item_row")) and paid_demand.get(stock_key, 0) >= requested:
				continue
			free_items_map.pop(key, None)
			skipped.append(
				{
					"item_code": item_code,
					"item_name": free_doc.get("item_name") or item_code,
					"warehouse": item_warehouse,
				}
			)
			rule_name = key[1] if isinstance(key, tuple) and len(key) > 1 else free_doc.get("pricing_rules")
			if rule_name:
				skipped_rule_names.add(rule_name)
			continue

		leftover[stock_key] -= requested

	if applied_rules is not None and skipped_rule_names:
		remaining_free_rules = {key[1] for key in free_items_map if isinstance(key, tuple) and len(key) > 1}
		for rule_name in skipped_rule_names:
			if rule_name in remaining_free_rules:
				continue
			is_product = False
			if rule_map and rule_name in rule_map:
				is_product = (rule_map[rule_name].get("price_or_product_discount") or "") == "Product"
			else:
				is_product = (
					frappe.get_cached_value("Pricing Rule", rule_name, "price_or_product_discount") == "Product"
				)
			if not is_product:
				continue
			applied_rules.discard(rule_name)
			if prepared_items:
				for item_doc in prepared_items:
					remove_pricing_rule(item_doc, rule_name)

	return skipped


def _free_item_row_names_blocked_by_stock(invoice_doc, errors):
	"""Return child-row names of free items (or free bundles) that failed stock check."""
	error_codes = {err.get("item_code") for err in (errors or []) if err.get("item_code")}
	if not error_codes:
		return set()

	free_rows = [row for row in invoice_doc.get("items") or [] if cint(row.get("is_free_item"))]
	if not free_rows:
		return set()

	free_names = {row.name for row in free_rows if row.name}
	remove_names = set()

	for row in free_rows:
		if row.get("item_code") in error_codes and row.name:
			remove_names.add(row.name)

	for packed in invoice_doc.get("packed_items") or []:
		if packed.get("item_code") not in error_codes:
			continue
		parent_name = packed.get("parent_detail_docname")
		if parent_name in free_names:
			remove_names.add(parent_name)

	return remove_names


def _strip_out_of_stock_free_items_from_invoice(invoice_doc, errors):
	"""Remove OOS free-item rows so paid items can still be submitted."""
	remove_names = _free_item_row_names_blocked_by_stock(invoice_doc, errors)
	if not remove_names:
		return []

	removed_codes = []
	for row in list(invoice_doc.get("items") or []):
		if row.name in remove_names:
			removed_codes.append(row.get("item_code") or row.name)
			invoice_doc.remove(row)

	if invoice_doc.get("packed_items"):
		for packed in list(invoice_doc.packed_items):
			if packed.get("parent_detail_docname") in remove_names:
				invoice_doc.remove(packed)

	return removed_codes


@lru_cache(maxsize=1)
def _item_has_allow_negative_stock_field():
	"""Check whether Item doctype has an allow_negative_stock field."""
	try:
		return frappe.get_meta("Item").has_field("allow_negative_stock")
	except Exception:
		return False


def _get_item_negative_stock_allow_set(items):
	"""Return set of item codes that allow negative stock at Item level."""
	if not items or not _item_has_allow_negative_stock_field():
		return set()

	item_codes = list({d.get("item_code") for d in items if d.get("item_code")})
	if not item_codes:
		return set()

	return set(
		frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes], "allow_negative_stock": 1},
			pluck="name",
		)
		or []
	)



def _evaluate_transaction_offers(
	invoice,
	profile,
	pricing_items,
	customer,
	customer_group,
	territory,
	posting_date,
	currency,
	price_list,
	rule_map,
	selected_offer_names,
):
	"""Run ERPNext's transaction-level pricing engine and collect free items.

	ERPNext routes `apply_on = "Transaction"` rules through a different entry
	point (`apply_pricing_rule_on_transaction`) than the per-item engine. That
	function mutates a real Sales Invoice document in place — appending free
	item rows via `doc.append("items", ...)` — so we build a transient,
	never-saved Sales Invoice document for evaluation only.

	Returns {"free_items": dict keyed by (item_code, rule_name), "applied_rules": set}.
	"""
	if not erpnext_apply_pricing_rule_on_transaction or not pricing_items:
		return {"free_items": {}, "applied_rules": set()}

	# `pricing_items` carry net (post item-discount) rates by the time the caller
	# reaches this point, so `total` is the discounted subtotal — the same basis
	# ERPNext's desk flow uses for transaction-scoped rules.
	total_qty = sum(flt(it.qty) for it in pricing_items)
	total = sum(flt(it.qty) * flt(it.rate) for it in pricing_items)
	if total <= 0:
		return {"free_items": {}, "applied_rules": set()}

	doc = frappe.new_doc("Sales Invoice")
	doc.update(
		{
			"is_pos": 1,
			"company": profile.company,
			"currency": currency,
			"conversion_rate": 1,
			"selling_price_list": price_list,
			"price_list_currency": currency,
			"plc_conversion_rate": 1,
			"customer": customer,
			"customer_group": customer_group,
			"territory": territory,
			"transaction_date": posting_date,
			"posting_date": posting_date,
			"pos_profile": invoice.get("pos_profile"),
			"coupon_code": invoice.get("coupon_code") or None,
		}
	)
	doc.flags.ignore_mandatory = True

	for prep in pricing_items:
		doc.append(
			"items",
			{
				"item_code": prep.item_code,
				"item_name": prep.item_name,
				"item_group": prep.item_group,
				"brand": prep.brand,
				"qty": prep.qty,
				"stock_qty": prep.stock_qty,
				"conversion_factor": prep.conversion_factor,
				"uom": prep.uom,
				"stock_uom": prep.stock_uom,
				"rate": prep.rate,
				"price_list_rate": prep.price_list_rate,
				"base_rate": prep.base_rate,
				"base_price_list_rate": prep.base_price_list_rate,
				"amount": flt(prep.rate) * flt(prep.qty),
				"warehouse": prep.warehouse,
			},
		)

	# filter_pricing_rules_for_qty_amount reads these straight off the doc
	# (erpnext/accounts/doctype/pricing_rule/utils.py:572).
	doc.total_qty = total_qty
	doc.total = total

	initial_item_count = len(doc.items)
	pre_addl_pct = flt(doc.get("additional_discount_percentage") or 0)
	pre_discount_amt = flt(doc.get("discount_amount") or 0)
	try:
		erpnext_apply_pricing_rule_on_transaction(doc)
	except Exception:
		# A misconfigured transaction-scoped rule must not break the per-item
		# discounts that have already been computed by the caller.
		frappe.log_error(frappe.get_traceback(), "POS Apply Offers (Transaction Rules)")
		return {
			"free_items": {},
			"applied_rules": set(),
			"additional_discount_percentage": 0,
			"discount_amount": 0,
			"apply_discount_on": None,
		}

	free_items = {}
	applied_rules = set()
	for row in doc.items[initial_item_count:]:
		if not getattr(row, "is_free_item", 0):
			continue
		rule_name = row.get("pricing_rules")
		if not rule_name or rule_name not in rule_map:
			continue
		if selected_offer_names and rule_name not in selected_offer_names:
			continue
		fid = frappe._dict(row.as_dict())
		fid.applied_promotional_scheme = rule_map[rule_name].promotional_scheme
		free_items[(row.item_code, rule_name)] = fid
		applied_rules.add(rule_name)

	# Capture header-level discount that ERPNext's apply_pricing_rule_on_transaction
	# set on the doc when a Price-type Transaction rule fired. ERPNext writes one of
	# additional_discount_percentage / discount_amount onto the doc (see
	# erpnext/accounts/doctype/pricing_rule/utils.py:578-616) but does not surface
	# which rule fired. We detect "fired" by diffing the doc fields against the
	# pre-call snapshot and attribute the application to every selected, in-scope
	# transaction-level Price rule in rule_map. The frontend treats the response
	# additional_discount_percentage / discount_amount as authoritative for the
	# header, so attribution mismatches only affect the UI badge, not totals.
	post_addl_pct = flt(doc.get("additional_discount_percentage") or 0)
	post_discount_amt = flt(doc.get("discount_amount") or 0)
	apply_discount_on = doc.get("apply_discount_on") or None

	header_discount_changed = post_addl_pct != pre_addl_pct or post_discount_amt != pre_discount_amt
	header_rules = set()
	if header_discount_changed:
		for rule_name, details in rule_map.items():
			if selected_offer_names and rule_name not in selected_offer_names:
				continue
			if details.get("price_or_product_discount") != "Price":
				continue
			if frappe.db.get_value("Pricing Rule", rule_name, "apply_on") != "Transaction":
				continue
			header_rules.add(rule_name)

	# Both halves of a transaction-scoped scheme must honour the same gate.
	# apply_pricing_rule_on_transaction runs its own SQL over every enabled
	# transaction rule and never consults `rule_map` or the cashier's
	# selection. The free item rows it appends are filtered against both above,
	# so leaving the header discount unfiltered lets one half of a scheme apply
	# while the other is suppressed — and lets a Pricing Rule the UI cannot even
	# offer (an orphan left behind by a deleted Promotional Scheme slab, say)
	# silently discount the sale.
	#
	# Coupon-driven rules are exempt: they are gated by the coupon code itself
	# and are deliberately kept out of `rule_map` (the top-up filters on
	# coupon_code_based = 0), so an empty `header_rules` says nothing about them.
	if header_discount_changed and not header_rules and not invoice.get("coupon_code"):
		post_addl_pct = 0.0
		post_discount_amt = 0.0
		apply_discount_on = None
	else:
		applied_rules.update(header_rules)

	return {
		"free_items": free_items,
		"applied_rules": applied_rules,
		"additional_discount_percentage": post_addl_pct,
		"discount_amount": post_discount_amt,
		"apply_discount_on": apply_discount_on,
	}


@frappe.whitelist()
def apply_offers(invoice_data, selected_offers=None):
	"""Calculate and apply promotional offers using ERPNext Pricing Rules.

	Args:
	        invoice_data (str | dict): Sales Invoice payload used for offer evaluation.
	        selected_offers (str | list | None): Optional collection of Pricing Rule names.
	                When provided, results are filtered to only include these rules.
	                ERPNext handles all conflict resolution based on priority.
	"""
	try:
		if isinstance(invoice_data, str):
			invoice_data = json.loads(invoice_data or "{}")

		invoice = frappe._dict(invoice_data or {})
		items = invoice.get("items") or []

		if isinstance(selected_offers, str):
			try:
				selected_offers = json.loads(selected_offers)
			except ValueError:
				selected_offers = [selected_offers]

		if isinstance(selected_offers, list | tuple | set):
			selected_offer_names = {cstr(name) for name in selected_offers if cstr(name)}
		else:
			selected_offer_names = set()

		if not items:
			return {"items": []}

		if not invoice.get("pos_profile") or not erpnext_apply_pricing_rule:
			# Either no POS profile supplied or ERPNext promotional engine unavailable
			return {"items": items}

		profile = frappe.get_cached_doc("POS Profile", invoice.get("pos_profile"))

		# Respect POS Profile's ignore_pricing_rule setting
		if profile.ignore_pricing_rule:
			return {"items": items}

		# Batch fetch all item details in a single query (reduces N queries to 1)
		item_codes = list({item.get("item_code") for item in items if item.get("item_code")})
		item_details_map = {}
		if item_codes:
			item_records = frappe.get_all(
				"Item",
				filters={"name": ["in", item_codes]},
				fields=["name", "item_name", "item_group", "brand", "stock_uom"],
			)
			item_details_map = {r.name: r for r in item_records}

		pricing_items = []
		index_map = []
		prepared_items = [frappe._dict(row) for row in items]

		for idx, item in enumerate(prepared_items):
			item_code = item.get("item_code")
			qty = flt(item.get("qty") or item.get("quantity") or 0)

			if not item_code or qty <= 0:
				continue

			# Use batch-fetched item details
			cached = item_details_map.get(item_code)
			if cached:
				item.item_name = item.get("item_name") or cached.item_name
				item.item_group = item.get("item_group") or cached.item_group
				item.brand = item.get("brand") or cached.brand
				item.stock_uom = item.get("stock_uom") or cached.stock_uom

			# Backfill catalog attributes onto the line itself. The cart payload
			# carries no item_group / brand, and rule scope matching reads them
			# straight off prepared_items — without this, every Item Group and
			# Brand rule silently matches nothing.
			if cached:
				if not item.get("item_group"):
					item.item_group = cached.item_group
				if not item.get("brand"):
					item.brand = cached.brand

			if item.get("is_free_item"):
				continue

			conversion_factor = flt(item.get("conversion_factor") or 1) or 1
			price_list_rate = flt(item.get("price_list_rate") or item.get("rate") or 0)

			pricing_items.append(
				frappe._dict(
					{
						"doctype": "Sales Invoice Item",
						"name": item.get("name") or f"POS-{idx}",
						"item_code": item_code,
						"item_name": (cached.item_name if cached else item.get("item_name")),
						"item_group": (cached.item_group if cached else item.get("item_group")),
						"brand": (cached.brand if cached else item.get("brand")),
						"qty": qty,
						"stock_qty": qty * conversion_factor,
						"conversion_factor": conversion_factor,
						"uom": item.get("uom")
						or item.get("stock_uom")
						or (cached.stock_uom if cached else None),
						"stock_uom": item.get("stock_uom") or (cached.stock_uom if cached else None),
						"price_list_rate": price_list_rate,
						"base_price_list_rate": price_list_rate,
						"rate": flt(item.get("rate") or price_list_rate),
						"base_rate": flt(item.get("rate") or price_list_rate),
						"discount_percentage": 0,
						"discount_amount": 0,
						"warehouse": item.get("warehouse") or profile.warehouse,
						"parenttype": invoice.get("doctype") or "Sales Invoice",
					}
				)
			)
			index_map.append(idx)

			# Clear previously applied promotional metadata if the
			# current quantity can no longer satisfy the rule.
			# Preserve manual cashier discounts across offer re-evaluation.
			is_manual = item.get("discount_source") == DISCOUNT_SOURCE_MANUAL
			if not is_manual and not item.get("pricing_rules"):
				is_manual = flt(item.get("discount_percentage")) > 0 or flt(item.get("discount_amount")) > 0
				if is_manual:
					item.discount_source = DISCOUNT_SOURCE_MANUAL

			if not is_manual:
				item.discount_percentage = 0
				item.discount_amount = 0
				item.pricing_rules = []
				item.applied_promotional_schemes = []
			else:
				item.pricing_rules = []
				item.applied_promotional_schemes = []

		if not pricing_items:
			return {"items": items}

		company_currency = frappe.get_cached_value("Company", profile.company, "default_currency")

		# Get customer details if customer is provided
		customer = invoice.get("customer")
		customer_group = invoice.get("customer_group")
		territory = invoice.get("territory")

		if customer and not customer_group:
			# Fetch customer_group from customer
			try:
				customer_data = frappe.get_cached_value(
					"Customer", customer, ["customer_group", "territory"], as_dict=1
				)
				if customer_data:
					customer_group = customer_data.get("customer_group")
					if not territory:
						territory = customer_data.get("territory")
			except Exception as e:
				# Customer lookup failed, will use defaults
				frappe.log_error(f"Failed to fetch customer data for {customer}: {e}", "Customer Data Lookup")

		# If still no customer_group, use default
		if not customer_group:
			customer_group = "All Customer Groups"

		pricing_args = frappe._dict(
			{
				"doctype": invoice.get("doctype") or "Sales Invoice",
				"name": invoice.get("name") or "POS-INVOICE",
				"is_pos": 1,
				"company": profile.company,
				"transaction_date": invoice.get("posting_date") or nowdate(),
				"posting_date": invoice.get("posting_date") or nowdate(),
				# Lets the engine's schedule gate judge the sale at the time it
				# happened rather than the time it is processed — an offline sale
				# synced hours later must keep the discount it earned.
				"posting_time": invoice.get("posting_time") or nowtime(),
				"currency": invoice.get("currency") or profile.get("currency") or company_currency,
				"conversion_rate": flt(invoice.get("conversion_rate") or 1) or 1,
				"plc_conversion_rate": flt(invoice.get("plc_conversion_rate") or 1) or 1,
				"price_list": invoice.get("price_list") or profile.get("selling_price_list"),
				"customer": customer,
				"customer_group": customer_group,
				"territory": territory,
				"items": pricing_items,
			}
		)

		schedule_moment = resolve_moment(pricing_args)

		# Call ERPNext pricing engine - it handles all conflicts based on priority
		#
		# Why we pass pricing_args twice:
		# - 1st param (args): ERPNext extracts and pops 'items' from this, then processes each item individually
		# - 2nd param (doc): Used by 'mixed_conditions' pricing rules to access the FULL items list
		#                    for quantity accumulation across different items in the same group
		#
		# Example: A rule "Buy 2 from Demo Item Group, get 10% off" with mixed_conditions=1
		# needs to see ALL items (1 Book + 1 Camera) to know total qty=2, not just each item's qty=1
		#
		# See: erpnext/accounts/doctype/pricing_rule/utils.py -> get_qty_and_rate_for_mixed_conditions()
		pricing_results = erpnext_apply_pricing_rule(pricing_args, doc=pricing_args) or []

		if not pricing_results and not selected_offer_names:
			return {"items": items}

		raw_rule_names = set()
		for result in pricing_results:
			if not result:
				continue
			rules = []
			if erpnext_get_applied_pricing_rules:
				rules = erpnext_get_applied_pricing_rules(result.get("pricing_rules"))
			else:
				raw_rules = result.get("pricing_rules") or []
				if isinstance(raw_rules, str):
					if raw_rules.startswith("["):
						rules = json.loads(raw_rules)
					else:
						rules = [r.strip() for r in raw_rules.split(",") if r.strip()]
				elif isinstance(raw_rules, list | tuple | set):
					rules = list(raw_rules)
			raw_rule_names.update(rules)

		# Build a map of applicable pricing rules from the ERPNext engine results.
		#
		# ERPNext has two types of pricing rules:
		#
		# 1. Promotional Scheme Rules (promotional_scheme is set):
		#    - Created automatically when a Promotional Scheme is saved
		#    - The scheme acts as a "template" that generates one or more Pricing Rules
		#    - Example: "Summer Sale" scheme creates "PRLE-0001", "PRLE-0002" rules
		#
		# 2. Standalone Pricing Rules (promotional_scheme is empty):
		#    - Created directly as Pricing Rule documents
		#    - Not linked to any Promotional Scheme
		#    - Example: A direct "10% off Item X" rule created in Pricing Rule doctype
		#
		# We include BOTH types for POS, but exclude coupon_code_based rules
		# (those require explicit coupon entry and are handled separately).
		#
		# Walk-in / default customer can't be tracked one-time, so one-time
		# rules never apply to anonymous sales (see the loop below).
		default_customer = profile.get("customer")

		rule_map = {}
		if raw_rule_names:
			rule_records = frappe.get_all(
				"Pricing Rule",
				filters={"name": ["in", list(raw_rule_names)]},
				fields=[
					"name",
					"promotional_scheme",
					"coupon_code_based",
					"one_time_per_customer",
					"promotional_scheme_id",
					"price_or_product_discount",
					"promotion_type",
				],
			)
			for record in rule_records:
				# Skip coupon-based rules (require explicit coupon code entry)
				if record.coupon_code_based:
					continue

				# One-time-per-customer rules: skip for anonymous sales and for
				# customers who have already redeemed this rule. Dropping the rule
				# here keeps its discount out of the per-item loop below (which only
				# applies rules present in rule_map), mirroring the coupon skip above.
				if record.one_time_per_customer:
					if not customer or customer == default_customer:
						continue
					if frappe.db.exists("One Time Customer Offer Usage", f"{customer}::{record.name}"):
						continue

				# Include both promotional scheme rules and standalone pricing rules
				rule_map[record.name] = record

		# Top up rule_map with transaction-scoped rules. The per-item engine
		# never surfaces apply_on="Transaction" rules, so without this they
		# would be dropped at the `if not rule_map: return` check below.
		# ERPNext's own SQL inside apply_pricing_rule_on_transaction handles
		# date/currency/pos_only filtering, so a broad superset is sufficient.
		if erpnext_apply_pricing_rule_on_transaction:
			txn_rule_records = frappe.get_all(
				"Pricing Rule",
				filters={
					"disable": 0,
					"apply_on": "Transaction",
					"company": profile.company,
					"selling": 1,
					"coupon_code_based": 0,
				},
				fields=[
					"name",
					"promotional_scheme",
					"coupon_code_based",
					"promotional_scheme_id",
					"price_or_product_discount",
					"promotion_type",
				],
			)
			for record in txn_rule_records:
				rule_map.setdefault(record.name, record)

		if selected_offer_names:
			# Restrict available rules to the ones explicitly selected from the UI.
			rule_map = {name: details for name, details in rule_map.items() if name in selected_offer_names}
			missing_selected = selected_offer_names - set(rule_map.keys())
			if missing_selected:
				extra_records = frappe.get_all(
					"Pricing Rule",
					filters={"name": ["in", list(missing_selected)]},
					fields=[
						"name",
						"promotional_scheme",
						"coupon_code_based",
						"one_time_per_customer",
						"promotional_scheme_id",
						"price_or_product_discount",
						"promotion_type",
					],
				)
				for record in extra_records:
					if record.coupon_code_based:
						continue
					rule_map[record.name] = record

		rule_map = filter_rules_by_schedule(rule_map, when=schedule_moment)

		if not rule_map:
			return {"items": items}

		applied_rules = set()
		# Deduplicate free items using a dict keyed by (item_code, pricing_rule).
		# ERPNext's apply_pricing_rule() returns one result per cart item and for
		# mixed_conditions rules attaches the same free_item_data to every matching
		# item's result. ERPNext's own apply_pricing_rule_for_free_items() deduplicates
		# the same way: {(item_code, pricing_rules): data for data in free_item_data}.
		free_items_map = {}

		for result, item_index in zip(pricing_results, index_map, strict=False):
			if not result:
				continue

			if erpnext_get_applied_pricing_rules:
				rule_names = erpnext_get_applied_pricing_rules(result.get("pricing_rules"))
			else:
				raw_rules = result.get("pricing_rules") or []
				if isinstance(raw_rules, str):
					if raw_rules.startswith("["):
						rule_names = json.loads(raw_rules)
					else:
						rule_names = [r.strip() for r in raw_rules.split(",") if r.strip()]
				elif isinstance(raw_rules, list | tuple | set):
					rule_names = list(raw_rules)
				else:
					rule_names = []

			applicable_rule_names = [
				name
				for name in rule_names or []
				if name in rule_map and rule_promotion_type(rule_map[name]) != PROMOTION_TYPE_AUTO
			]

			if not applicable_rule_names:
				continue

			applied_rules.update(applicable_rule_names)

			item_doc = prepared_items[item_index]
			qty = flt(item_doc.get("qty") or item_doc.get("quantity") or 0)
			price_list_rate = flt(
				result.get("price_list_rate") or item_doc.get("price_list_rate") or item_doc.get("rate") or 0
			)

			# Get discount from result or fetch from pricing rule
			discount_percentage = flt(result.get("discount_percentage") or 0)
			per_unit_discount = flt(result.get("discount_amount") or 0)

			# If ERPNext didn't calculate discount (validate_applied_rule=1),
			# we need to fetch and apply it manually
			if not discount_percentage and not per_unit_discount and applicable_rule_names:
				for rule_name in applicable_rule_names:
					rule_doc = rule_map.get(rule_name)
					if not rule_doc:
						continue

					# Fetch full pricing rule to get discount values
					full_rule = frappe.get_cached_doc("Pricing Rule", rule_name)

					# Cross-cart modes are deferred on purpose — re-deriving their
					# discount from the rule here would undo that:
					#   Min/Max      — would discount every matching item, defeating
					#                  the "cheapest / most expensive" ranking.
					#   Accumulative — would stack the rule's own discount_percentage
					#                  underneath the accumulated per-scope total.
					if full_rule.get("apply_discount_on_price") in CROSS_CART_MODES:
						continue

					if full_rule.rate_or_discount == "Discount Percentage" and full_rule.discount_percentage:
						discount_percentage += flt(full_rule.discount_percentage)
					elif full_rule.rate_or_discount == "Discount Amount" and full_rule.discount_amount:
						per_unit_discount += flt(full_rule.discount_amount)
					elif full_rule.rate_or_discount == "Rate" and full_rule.rate:
						# Apply fixed rate
						price_list_rate = flt(full_rule.rate)

			line_discount_amount = 0
			if discount_percentage and qty and price_list_rate:
				line_discount_amount = price_list_rate * qty * discount_percentage / 100
			elif per_unit_discount and qty:
				line_discount_amount = per_unit_discount * qty
			else:
				line_discount_amount = per_unit_discount

			if not discount_percentage and line_discount_amount and qty and price_list_rate:
				base_amount = price_list_rate * qty
				if base_amount:
					discount_percentage = (line_discount_amount / base_amount) * 100

			item_doc.discount_percentage = discount_percentage
			item_doc.discount_amount = line_discount_amount
			item_doc.price_list_rate = price_list_rate
			item_doc.rate = flt(item_doc.get("rate") or price_list_rate)
			# ERPNext expects pricing_rules as comma-separated string, not a list
			item_doc.pricing_rules = ",".join(applicable_rule_names) if applicable_rule_names else ""

			item_doc.applied_promotional_schemes = list(
				{
					rule_map[name].promotional_scheme
					for name in applicable_rule_names
					if rule_map[name].promotional_scheme
				}
			)

			for free_item in result.get("free_item_data") or []:
				rule_name = free_item.get("pricing_rules")
				if not rule_name or rule_name not in rule_map:
					continue
				if rule_promotion_type(rule_map[rule_name]) in (PROMOTION_TYPE_GWP, PROMOTION_TYPE_GIFT_POOL):
					continue
				free_item_doc = frappe._dict(free_item)
				free_item_doc.qty = _floor_free_item_qty(free_item_doc.get("qty"))
				free_item_doc.applied_promotional_scheme = rule_map[rule_name].promotional_scheme
				free_items_map[(free_item.get("item_code"), rule_name)] = free_item_doc

		# Line-level discount passes, in order, through the shared accumulator so
		# they compose instead of overwriting each other. See
		# posnext_promotions.promotions.engine.
		applied_rules.update(run_line_discount_passes(prepared_items, rule_map, selected_offer_names))

		# Apply Min/Max ("cheapest/most-expensive item") price rules. These were
		# deferred by the per-item engine (see posnext_promotions.overrides.pricing_rule) and
		# need a cross-item ranking pass over the whole cart. The mock doc has no
		# calculate_taxes_and_totals(); the post-processor materialises rate/amount
		# on each discounted item directly.
		if apply_min_max_price_discounts:
			mock_doc = frappe._dict(
				{
					"doctype": invoice.get("doctype") or "Sales Invoice",
					"items": prepared_items,
					"selling_price_list": pricing_args.price_list,
					"company": pricing_args.company,
					"customer": pricing_args.customer,
				}
			)
			min_max_allowed = set(rule_map) if selected_offer_names else None
			apply_min_max_price_discounts(mock_doc, allowed_rules=min_max_allowed)

		# Surface Min/Max rules in the response so the frontend tracks them as applied.
		if erpnext_get_applied_pricing_rules:
			for prepared_item in prepared_items:
				if not prepared_item.get("pricing_rules"):
					continue
				for pr_name in erpnext_get_applied_pricing_rules(prepared_item.get("pricing_rules")):
					if pr_name in rule_map:
						applied_rules.add(pr_name)

		# Fold the item-level discounts decided above back into `pricing_items`
		# before the transaction-scoped engine runs. `pricing_items` was built
		# pre-discount (rate == list rate, discount fields zeroed) to feed the
		# per-item engine; leaving it that way makes the transaction engine see
		# the *gross* cart total, which both lets min_amt-gated rules fire on a
		# cart that never reached the threshold and sizes their percentage off
		# the undiscounted base.
		#
		# Prefer `discount_percentage` when set. Same-item free gifts stamp an
		# absolute `discount_amount` with percentage 0 — convert that to a net
		# rate so transaction min_amt rules still see the post-discount total.
		for pricing_item, item_index in zip(pricing_items, index_map, strict=False):
			prepared_item = prepared_items[item_index]
			list_rate = flt(prepared_item.get("price_list_rate")) or flt(pricing_item.price_list_rate)
			discount_pct = flt(prepared_item.get("discount_percentage") or 0)
			line_qty = flt(prepared_item.get("qty") or prepared_item.get("quantity") or pricing_item.qty or 0)
			discount_amt = flt(prepared_item.get("discount_amount") or 0)

			if discount_pct:
				net_rate = list_rate * (1 - discount_pct / 100.0) if list_rate else flt(pricing_item.rate)
			elif discount_amt and line_qty:
				net_rate = max(0, list_rate - (discount_amt / line_qty))
			else:
				net_rate = list_rate if list_rate else flt(pricing_item.rate)

			pricing_item.price_list_rate = list_rate
			pricing_item.base_price_list_rate = list_rate
			pricing_item.rate = net_rate
			pricing_item.base_rate = net_rate
			pricing_item.discount_percentage = discount_pct

		# Evaluate apply_on="Transaction" rules through ERPNext's separate
		# transaction-level engine. The per-item engine above does not see
		# them, so without this step "Entire Transaction" promotional schemes
		# (free product based on cart total) would never apply.
		txn_result = _evaluate_transaction_offers(
			invoice,
			profile,
			pricing_items,
			customer,
			customer_group,
			territory,
			invoice.get("posting_date") or nowdate(),
			pricing_args.currency,
			pricing_args.price_list,
			rule_map,
			selected_offer_names,
		)
		txn_result = filter_auto_discount_from_header(txn_result, rule_map, selected_offer_names)
		# Per-item results win on collisions because they already carry full
		# discount metadata from the per-item engine result.
		for key, free_item_doc in txn_result.get("free_items", {}).items():
			free_item_doc.qty = _floor_free_item_qty(free_item_doc.get("qty"))
			free_items_map.setdefault(key, free_item_doc)
		applied_rules.update(txn_result.get("applied_rules", set()))

		# Recursive product free must run after per-item + txn free rows are
		# collected, then bundle same-item gifts / apply GWP line discounts.
		_recompute_recursive_product_free_items(
			prepared_items, free_items_map, rule_map, applied_rules
		)
		_apply_bundled_same_item_free_discounts(prepared_items, free_items_map, rule_map)
		_apply_gwp_line_discounts(prepared_items, free_items_map, rule_map, applied_rules)
		_apply_gift_pool_free_items(
			prepared_items,
			free_items_map,
			rule_map,
			applied_rules,
			warehouse=profile.warehouse,
			pos_profile=invoice.get("pos_profile"),
		)

		if mark_item_discount_flags:
			# Same neutralised view the pipeline used. With the raw map an
			# Accumulative rule reads as a plain Item Level Discount, so a line it
			# merely *matched* without discounting (an unpriced one, say) would be
			# stamped is_already_discounted and lose its coupon eligibility.
			mark_item_discount_flags(prepared_items, build_matrix_type_map(rule_map))

		for free_item_doc in free_items_map.values():
			free_item_doc.qty = _floor_free_item_qty(free_item_doc.get("qty"))

		skipped_free_items = _filter_out_of_stock_free_items(
			free_items_map,
			profile.warehouse,
			prepared_items,
			invoice.get("pos_profile"),
			rule_map=rule_map,
			applied_rules=applied_rules,
			prepared_items=prepared_items,
		)

		return {
			"items": [dict(item) for item in prepared_items],
			"free_items": [dict(item) for item in free_items_map.values()],
			"skipped_free_items": skipped_free_items,
			"applied_pricing_rules": sorted(applied_rules),
			# Header-level (transaction-scope) discount surfaced from
			# _evaluate_transaction_offers. Frontend should apply these to the
			# invoice header (additionalDiscount + apply_discount_on) when
			# present. Both fields are zero when no transaction-level Price
			# rule fired.
			"additional_discount_percentage": flt(txn_result.get("additional_discount_percentage") or 0),
			"discount_amount": flt(txn_result.get("discount_amount") or 0),
			"apply_discount_on": txn_result.get("apply_discount_on"),
		}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Apply Offers Error")
		frappe.throw(_("Error applying offers: {0}").format(str(e)))
