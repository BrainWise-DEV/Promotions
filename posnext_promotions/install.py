# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Install / migrate helpers. POS Coupon extras are created only if that DocType exists."""

from __future__ import annotations

import frappe

# Module Def names this app owns. They may already exist on a site that ran
# staging pos_next (promotions lived there before the split).
#
# "POS Next Auth Gate" is deliberately absent: the authorization gate belongs to
# pos_next (its modules.txt, DocTypes and rehome patch). Deleting that Module Def
# here left sites without it when pos_next's migrate ran before this install
# (the Frappe Cloud order: update site, then install from the dashboard).
OWNED_MODULE_DEFS = ("POSNext Promotions",)


def before_install():
	"""Reclaim Module Defs left behind by pos_next so install can insert them."""
	for module in OWNED_MODULE_DEFS:
		if not frappe.db.exists("Module Def", module):
			continue
		frappe.delete_doc("Module Def", module, force=1, ignore_permissions=True)


APPLY_SCOPE_OPTIONS = "All Eligible Items\nItem Code\nItem Group\nBrand"

# Table fields are only created once their child DocType has been synced.
TABLE_FIELD_CHILD_DOCTYPES = {
	"excluded_brands": "POS Coupon Excluded Brand",
	"applicable_items": "POS Coupon Item",
	"applicable_item_groups": "POS Coupon Item Group",
	"applicable_brands": "POS Coupon Brand",
}

# Properties re-applied on every migrate so sites created before the scope tables
# pick up the new options / visibility without a manual Customize Form edit.
SYNCED_PROPERTIES = ("options", "depends_on", "hidden", "insert_after")
SYNCED_FIELDS = ("apply_scope", "applicable_brand", "applicable_item_group")

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
		"options": APPLY_SCOPE_OPTIONS,
		"default": "All Eligible Items",
		"insert_after": "scope_section",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "applicable_items",
		"label": "Applicable Items",
		"fieldtype": "Table",
		"options": "POS Coupon Item",
		"insert_after": "apply_scope",
		"depends_on": "eval:doc.apply_scope=='Item Code'",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "applicable_item_groups",
		"label": "Applicable Item Groups",
		"fieldtype": "Table",
		"options": "POS Coupon Item Group",
		"insert_after": "applicable_items",
		"depends_on": "eval:doc.apply_scope=='Item Group'",
	},
	{
		"dt": "POS Coupon",
		"fieldname": "applicable_brands",
		"label": "Applicable Brands",
		"fieldtype": "Table",
		"options": "POS Coupon Brand",
		"insert_after": "applicable_item_groups",
		"depends_on": "eval:doc.apply_scope=='Brand'",
	},
	# Superseded by the scope tables; kept hidden so v2_5_0 can migrate their values.
	{
		"dt": "POS Coupon",
		"fieldname": "applicable_brand",
		"label": "Applicable Brand",
		"fieldtype": "Link",
		"options": "Brand",
		"insert_after": "applicable_brands",
		"hidden": 1,
	},
	{
		"dt": "POS Coupon",
		"fieldname": "applicable_item_group",
		"label": "Applicable Collection",
		"fieldtype": "Link",
		"options": "Item Group",
		"insert_after": "applicable_brand",
		"hidden": 1,
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


# Tags the invoice lines a scoped coupon discounted so validate can re-check them.
COUPON_LINE_FIELDS = [
	{
		"dt": dt,
		"fieldname": "posnext_coupon_code",
		"label": "Coupon Code",
		"fieldtype": "Data",
		"read_only": 1,
		"no_copy": 1,
		"insert_after": "discount_amount",
	}
	for dt in ("Sales Invoice Item", "POS Invoice Item")
]


def after_migrate():
	_ensure_pos_coupon_extras()
	_ensure_coupon_line_fields()


def _ensure_coupon_line_fields():
	for spec in COUPON_LINE_FIELDS:
		if frappe.db.exists("Custom Field", f"{spec['dt']}-{spec['fieldname']}"):
			continue
		if not frappe.db.exists("DocType", spec["dt"]):
			continue
		frappe.get_doc({"doctype": "Custom Field", "module": "POSNext Promotions", **spec}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)
		frappe.clear_cache(doctype=spec["dt"])


def _ensure_pos_coupon_extras():
	if not frappe.db.exists("DocType", "POS Coupon"):
		return

	meta = frappe.get_meta("POS Coupon")
	for spec in POS_COUPON_EXTRA_FIELDS:
		fieldname = spec["fieldname"]
		custom_field_name = f"POS Coupon-{fieldname}"
		if frappe.db.exists("Custom Field", custom_field_name):
			if fieldname in SYNCED_FIELDS:
				_sync_custom_field(custom_field_name, spec)
			continue
		# Skip when the column already exists as a native POS Coupon field (staging pos_next).
		if meta.has_field(fieldname):
			if fieldname in SYNCED_FIELDS:
				_sync_native_field(fieldname, spec)
			continue
		child_doctype = TABLE_FIELD_CHILD_DOCTYPES.get(fieldname)
		if child_doctype and not frappe.db.exists("DocType", child_doctype):
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


def _sync_custom_field(custom_field_name, spec):
	doc = frappe.get_doc("Custom Field", custom_field_name)
	changed = False
	for prop in SYNCED_PROPERTIES:
		value = spec.get(prop, 0 if prop == "hidden" else None)
		if (doc.get(prop) or None) != (value or None):
			doc.set(prop, value)
			changed = True
	if changed:
		doc.save(ignore_permissions=True)


def _sync_native_field(fieldname, spec):
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	for prop, property_type in (("options", "Text"), ("depends_on", "Data"), ("hidden", "Check")):
		value = spec.get(prop, 0 if prop == "hidden" else None)
		if value is None:
			continue
		make_property_setter(
			"POS Coupon",
			fieldname,
			prop,
			value,
			property_type,
			validate_fields_for_doctype=False,
		)
