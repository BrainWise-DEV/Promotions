"""Force Promotional Scheme.apply_with_ff = 1 and sync the Custom Field (default 1, hidden).

F&F must always compete with scheme discounts via best-discount; the checkbox is
no longer exposed in the UI. Idempotent.
"""

import frappe


def execute():
	if frappe.db.has_column("Promotional Scheme", "apply_with_ff"):
		frappe.db.sql("UPDATE `tabPromotional Scheme` SET apply_with_ff = 1 WHERE IFNULL(apply_with_ff, 0) = 0")

	cf_name = frappe.db.get_value(
		"Custom Field", {"dt": "Promotional Scheme", "fieldname": "apply_with_ff"}, "name"
	)
	if cf_name:
		frappe.db.set_value(
			"Custom Field",
			cf_name,
			{"default": "1", "hidden": 1},
			update_modified=False,
		)
		frappe.clear_cache(doctype="Promotional Scheme")
