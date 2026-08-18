# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Install / migrate helpers. POS Coupon extras are created only if that DocType exists."""

from __future__ import annotations

import frappe

# Module Def names this app owns. They may already exist on a site that ran
# staging pos_next (auth gate lived there before the split).
OWNED_MODULE_DEFS = ("POSNext Promotions", "POS Next Auth Gate")


def before_install():
	"""Reclaim Module Defs left behind by pos_next so install can insert them."""
	for module in OWNED_MODULE_DEFS:
		if not frappe.db.exists("Module Def", module):
			continue
		frappe.delete_doc("Module Def", module, force=1, ignore_permissions=True)


POS_COUPON_EXTRA_FIELDS = [
	{
		"dt": "POS Coupon",
		"fieldname": "scope_section",
		"label": "Scope and Exclusions",
		"fieldtype": "Section Break",
		"insert_after": "apply_on",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "apply_scope",
		"label": "Apply Scope",
		"fieldtype": "Select",
		"options": "All Eligible Items\nBrand\nItem Group",
		"default": "All Eligible Items",
		"insert_after": "scope_section",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "applicable_brand",
		"label": "Applicable Brand",
		"fieldtype": "Link",
		"options": "Brand",
		"insert_after": "apply_scope",
		"depends_on": "eval:doc.apply_scope=='Brand'",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "applicable_item_group",
		"label": "Applicable Collection",
		"fieldtype": "Link",
		"options": "Item Group",
		"insert_after": "applicable_brand",
		"depends_on": "eval:doc.apply_scope=='Item Group'",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "excluded_brands",
		"label": "Excluded Brands",
		"fieldtype": "Table",
		"options": "POS Coupon Excluded Brand",
		"insert_after": "applicable_item_group",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "maximum_use_per_customer",
		"label": "Uses Per Customer",
		"fieldtype": "Int",
		"insert_after": "one_use",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "exclude_already_discounted_items",
		"label": "Exclude Already Discounted Items",
		"fieldtype": "Check",
		"default": "1",
		"insert_after": "maximum_use_per_customer",
	},
]


def after_migrate():
	_ensure_pos_coupon_extras()


def _ensure_pos_coupon_extras():
	if not frappe.db.exists("DocType", "POS Coupon"):
		return

	meta = frappe.get_meta("POS Coupon")
	for spec in POS_COUPON_EXTRA_FIELDS:
		fieldname = spec["fieldname"]
		# Skip when the column already exists as a native POS Coupon field
		# (staging pos_next) or as a Custom Field from a previous migrate.
		if meta.has_field(fieldname) or frappe.db.exists("Custom Field", f"POS Coupon-{fieldname}"):
			continue
		if fieldname == "excluded_brands" and not frappe.db.exists(
			"DocType", "POS Coupon Excluded Brand"
		):
			continue
		try:
			frappe.get_doc(
				{
					"doctype": "Custom Field",
					"module": "POSNext Promotions",
					**spec,
				}
			).insert(ignore_permissions=True, ignore_if_duplicate=True)
		except frappe.ValidationError:
			frappe.clear_last_message()
			continue
	frappe.clear_cache(doctype="POS Coupon")
