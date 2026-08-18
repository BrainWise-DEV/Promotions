# Copyright (c) 2026, BrainWise and contributors
"""One-time promotional offer usage on Sales Invoice submit/cancel."""

import json

import frappe
from frappe.utils import now


def record_one_time_offer_usage(doc, method=None):
	if doc.get("is_return") or not doc.get("customer"):
		return

	raw = doc.get("pos_applied_one_time_rules")
	if not raw:
		return
	try:
		rule_names = json.loads(raw)
	except (ValueError, TypeError):
		return
	if not rule_names:
		return

	for rule in rule_names:
		try:
			frappe.get_doc(
				{
					"doctype": "One Time Customer Offer Usage",
					"customer": doc.customer,
					"pricing_rule": rule,
					"sales_invoice": doc.name,
					"redemption_date": now(),
				}
			).insert(ignore_permissions=True, ignore_if_duplicate=True)
		except frappe.DuplicateEntryError:
			pass


def release_one_time_offer_usage(doc, method=None):
	if frappe.db.exists("DocType", "One Time Customer Offer Usage"):
		frappe.db.delete("One Time Customer Offer Usage", {"sales_invoice": doc.name})
