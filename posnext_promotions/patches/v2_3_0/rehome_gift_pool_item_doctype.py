# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Move POS Gift Pool Item off pos_next onto this app's module."""

import frappe


def execute():
	if not frappe.db.exists("DocType", "POS Gift Pool Item"):
		return

	frappe.db.set_value(
		"DocType",
		"POS Gift Pool Item",
		{"module": "POSNext Promotions", "custom": 0},
		update_modified=False,
	)
	frappe.clear_cache(doctype="POS Gift Pool Item")
	frappe.clear_cache(doctype="Promotional Scheme")
