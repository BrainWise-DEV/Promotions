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

from posnext_promotions.api.coupon_engine import _item_matches_scope

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

		if coupon.discount_type == "Percentage" and flt(row.discount_percentage) > flt(coupon.discount_percentage) + TOLERANCE:
			frappe.throw(
				_("Row {0}: discount {1}% exceeds POS Coupon {2} ({3}%)").format(
					row.idx, flt(row.discount_percentage), code, flt(coupon.discount_percentage)
				)
			)
		spent[code] += flt(row.discount_amount) * flt(row.qty)

	for code, total in spent.items():
		coupon = coupons[code]
		cap = flt(coupon.max_amount) if flt(coupon.max_amount) else None
		if coupon.discount_type == "Amount":
			cap = min(cap, flt(coupon.discount_amount)) if cap else flt(coupon.discount_amount)
		# Percentage coupons without a cap are priced as a percentage, not an amount.
		if cap and total > cap + TOLERANCE:
			frappe.throw(_("POS Coupon {0} discount {1} exceeds its limit of {2}").format(code, total, cap))
