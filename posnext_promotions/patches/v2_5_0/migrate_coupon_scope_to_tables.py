# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Move single-value POS Coupon scope (applicable_brand / applicable_item_group) into scope tables."""

import frappe

_LEGACY_SCOPES = (
	# (apply_scope, legacy column, child doctype, parentfield, child column)
	("Brand", "applicable_brand", "POS Coupon Brand", "applicable_brands", "brand"),
	("Item Group", "applicable_item_group", "POS Coupon Item Group", "applicable_item_groups", "item_group"),
)


def execute():
	if not frappe.db.exists("DocType", "POS Coupon"):
		return

	from posnext_promotions.install import _ensure_pos_coupon_extras

	_ensure_pos_coupon_extras()

	if not frappe.db.has_column("POS Coupon", "apply_scope"):
		return

	for scope, legacy_column, child_doctype, parentfield, child_column in _LEGACY_SCOPES:
		if not frappe.db.has_column("POS Coupon", legacy_column):
			continue
		if not frappe.db.exists("DocType", child_doctype):
			continue

		coupons = frappe.get_all(
			"POS Coupon",
			filters={"apply_scope": scope, legacy_column: ["is", "set"]},
			fields=["name", legacy_column],
		)
		for coupon in coupons:
			value = coupon.get(legacy_column)
			if frappe.db.exists(
				child_doctype,
				{"parent": coupon.name, "parenttype": "POS Coupon", "parentfield": parentfield},
			):
				continue
			child = frappe.get_doc(
				{
					"doctype": child_doctype,
					"parent": coupon.name,
					"parenttype": "POS Coupon",
					"parentfield": parentfield,
					"idx": 1,
					child_column: value,
				}
			)
			child.db_insert()

	frappe.clear_cache(doctype="POS Coupon")
