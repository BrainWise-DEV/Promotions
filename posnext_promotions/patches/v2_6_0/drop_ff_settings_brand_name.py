"""Drop the stored value of the removed Friends and Family Settings.brand_name.

``{brand}`` in Friends and Family Message texts now always uses the default
Company name. Singles keep orphan rows after a field is removed, so delete it.
Idempotent.
"""

import frappe


def execute():
	frappe.db.delete("Singles", {"doctype": "Friends and Family Settings", "field": "brand_name"})
	frappe.cache().delete_value("posnext_promotions:ff_customer_messages")
