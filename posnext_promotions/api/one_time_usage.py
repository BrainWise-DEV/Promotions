# Copyright (c) 2026, BrainWise and contributors
"""One-time promotional offer usage on Sales Invoice submit/cancel."""

import json
from contextlib import contextmanager

import frappe
from frappe.utils import cstr, now

# The invoice whose own redemptions do not count as earlier ones (server-side only:
# frappe.flags is request state no client can set).
OWN_INVOICE_FLAG = "posnext_one_time_own_invoice"


def own_invoice() -> str:
	"""The invoice being validated under :func:`judging_invoice`, else ''."""
	return cstr(frappe.flags.get(OWN_INVOICE_FLAG))


@contextmanager
def judging_invoice(name):
	"""Within: one-time lookups ignore usage rows and stamps of invoice ``name``."""
	previous = frappe.flags.get(OWN_INVOICE_FLAG)
	frappe.flags[OWN_INVOICE_FLAG] = cstr(name)
	try:
		yield
	finally:
		frappe.flags[OWN_INVOICE_FLAG] = previous


def redeemed_rules(customer, exclude_invoice=None) -> set:
	"""One-time rules ``customer`` already redeemed: a usage row or a stamp on one of
	their submitted, non-return Sales Invoices, as pos_next decides.

	Rows and stamps of ``exclude_invoice`` (the invoice being validated) are not an
	earlier redemption: pos_next redeems before it submits, so that invoice's own row
	exists while it is checked (D-R3-04 b). Every other invoice still counts.
	"""
	if not customer:
		return set()
	exclude = cstr(exclude_invoice)
	redeemed = {
		row.pricing_rule
		for row in frappe.get_all(
			"One Time Customer Offer Usage",
			filters={"customer": customer},
			fields=["pricing_rule", "sales_invoice"],
		)
		if not exclude or cstr(row.sales_invoice) != exclude
	}
	filters = {
		"customer": customer,
		"docstatus": 1,
		"is_return": 0,
		"pos_applied_one_time_rules": ["is", "set"],
	}
	if exclude:
		filters["name"] = ["!=", exclude]
	for stamp in frappe.get_all("Sales Invoice", filters=filters, pluck="pos_applied_one_time_rules"):
		try:
			redeemed.update(json.loads(stamp or "[]"))
		except (ValueError, TypeError):
			continue
	return redeemed


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
