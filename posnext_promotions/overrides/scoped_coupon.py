# Copyright (c) 2026, BrainWise and contributors
"""Server-side re-check of lines the POS discounted with a scoped POS Coupon.

The POS client tags each such line with ``posnext_coupon_code``. The client
prices the lines from ``validate_coupon``, but nothing stops a stale or altered
client (or an offline invoice synced later) from tagging lines the coupon does
not cover, so the invoice is checked here before it is saved.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from posnext_promotions.api.coupon_engine import (
	PROMOTION_CONFLICT_ADD,
	_item_matches_scope,
	get_promotion_conflict_mode,
)

SCOPED = ("Item Code", "Item Group", "Brand")
# Currency rounding on the client can leave a line a few cents above its allocation.
TOLERANCE = 0.05


def validate_scoped_coupon_lines(doc, method=None):
	tagged = [
		row
		for row in doc.get("items") or []
		if row.get("posnext_coupon_code") and not cint(row.get("is_free_item"))
	]
	if not tagged:
		return

	coupons = {}
	for code in {row.posnext_coupon_code.upper() for row in tagged}:
		name = frappe.db.get_value("POS Coupon", {"coupon_code": code})
		if not name:
			frappe.throw(_("POS Coupon {0} does not exist").format(code))
		coupons[code] = frappe.get_cached_doc("POS Coupon", name)

	spent = {code: 0.0 for code in coupons}
	for row in tagged:
		code = row.posnext_coupon_code.upper()
		coupon = coupons[code]
		if cint(coupon.get("disabled")):
			frappe.throw(_("Row {0}: POS Coupon {1} is disabled").format(row.idx, code))
		if (doc.get("coupon_code") or "").upper() != code:
			frappe.throw(_("Row {0}: POS Coupon {1} is not the coupon applied to this invoice").format(row.idx, code))

		scope = coupon.get("apply_scope") or "All Eligible Items"
		if scope in SCOPED:
			item = {
				"item_code": row.item_code,
				"brand": row.get("brand")
				or frappe.db.get_value("Item", row.item_code, "brand"),
				"item_group": row.get("item_group")
				or frappe.db.get_value("Item", row.item_code, "item_group"),
			}
			if not _item_matches_scope(coupon, item):
				frappe.throw(
					_("Row {0}: item {1} is not covered by POS Coupon {2}").format(row.idx, row.item_code, code)
				)

		pct = flt(coupon.discount_percentage) if coupon.discount_type == "Percentage" else 0
		# "Add on top": the row also carries its own discount, so its total is no measure of the coupon.
		stacked = get_promotion_conflict_mode(coupon) == PROMOTION_CONFLICT_ADD
		if pct and not stacked and flt(row.discount_percentage) > pct + TOLERANCE:
			frappe.throw(
				_("Row {0}: discount {1}% exceeds POS Coupon {2} ({3}%)").format(
					row.idx, flt(row.discount_percentage), code, pct
				)
			)
		spent[code] += _coupon_share(row, pct if stacked else None)

	for code, total in spent.items():
		coupon = coupons[code]
		cap = flt(coupon.max_amount) if flt(coupon.max_amount) else None
		if coupon.discount_type == "Amount":
			cap = min(cap, flt(coupon.discount_amount)) if cap else flt(coupon.discount_amount)
		# Percentage coupons without a cap are priced as a percentage, not an amount.
		if cap and total > cap + TOLERANCE:
			frappe.throw(_("POS Coupon {0} discount {1} exceeds its limit of {2}").format(code, total, cap))


def _coupon_share(row, stacked_pct=None) -> float:
	"""Coupon discount on the row (line total). ``stacked_pct``: None when not stacked.

	A stacked percentage coupon takes pct of what the line's own discount left
	(line - pre), so with line D = pre + share its share is pct * (base - D) / (1 - pct).
	A stacked Amount coupon's share can't be derived: the POS saves it in
	``posnext_coupon_discount``. Either way never more than the row's discount.
	"""
	total = flt(row.discount_amount) * flt(row.qty)
	if stacked_pct is None:
		return total
	if not stacked_pct:
		# The rest of the row's discount is judged by pos_next as the line's own (POS Settings limits).
		return min(total, max(flt(row.get("posnext_coupon_discount")), 0))
	base = flt(row.price_list_rate) * flt(row.qty)
	if base <= 0 or stacked_pct >= 100:
		return total
	p = stacked_pct / 100
	return min(total, max(base - total, 0) * p / (1 - p))


# Scope -> (child table, legacy single field or None, label for the message)
SCOPE_TABLES = {
	"Item Code": ("applicable_items", None, _("Applicable Items")),
	"Item Group": ("applicable_item_groups", "applicable_item_group", _("Applicable Item Groups")),
	"Brand": ("applicable_brands", "applicable_brand", _("Applicable Brands")),
}


def validate_coupon_scope(doc, method=None):
	"""POS Coupon save: a restricted scope needs at least one Item / Item Group / Brand.

	A coupon saved with an empty list matches no cart line, so every cart would be
	rejected. Only checked on save; coupons already stored empty keep loading.
	"""
	scope = doc.get("apply_scope") or "All Eligible Items"
	spec = SCOPE_TABLES.get(scope)
	if not spec:
		return
	table, legacy, label = spec
	if any(row.get(_ROW_FIELD[table]) for row in (doc.get(table) or [])):
		return
	if legacy and doc.get(legacy):
		return
	frappe.throw(
		_("Scope is {0}: add at least one row to {1} or change the scope to All Eligible Items").format(
			frappe.bold(_(scope)), frappe.bold(label)
		),
		title=_("Coupon scope is empty"),
	)


_ROW_FIELD = {
	"applicable_items": "item_code",
	"applicable_item_groups": "item_group",
	"applicable_brands": "brand",
}
