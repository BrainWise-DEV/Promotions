"""Hide Friends & Family Group/Flag eligibility UI; eligibility is email-domain only.

Hides Settings fields (eligibility_basis, eligible_customer_groups) and the
Customer ``custom_is_friends_and_family`` checkbox. Idempotent.
"""

import frappe


def execute():
	from posnext_promotions.promotions.friends_and_family_eligibility import (
		FLAG_FIELD,
		ensure_customer_flag_field,
	)

	ensure_customer_flag_field()

	for fieldname in ("eligibility_basis", "eligible_customer_groups", "eligibility_section", "column_break_eligibility"):
		# DocType fields are in the JSON; Property Setter keeps Desk in sync without migrate wait.
		if not frappe.db.exists(
			"Property Setter",
			{"doc_type": "Friends and Family Settings", "field_name": fieldname, "property": "hidden"},
		):
			frappe.get_doc(
				{
					"doctype": "Property Setter",
					"doctype_or_field": "DocField",
					"doc_type": "Friends and Family Settings",
					"field_name": fieldname,
					"property": "hidden",
					"property_type": "Check",
					"value": "1",
				}
			).insert(ignore_permissions=True)

	cf_name = frappe.db.get_value("Custom Field", {"dt": "Customer", "fieldname": FLAG_FIELD}, "name")
	if cf_name:
		frappe.db.set_value(
			"Custom Field",
			cf_name,
			{"hidden": 1, "in_standard_filter": 0},
			update_modified=False,
		)

	frappe.clear_cache(doctype="Friends and Family Settings")
	frappe.clear_cache(doctype="Customer")
