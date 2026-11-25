# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class FriendsandFamilyUsageTotal(Document):
	"""Per customer and period F&F running total. Written by the system, never edited."""

	def validate(self):
		frappe.throw(_("Friends and Family Usage Total rows are maintained by the system"), frappe.PermissionError)


def on_doctype_update():
	from posnext_promotions.promotions.ff_ledger import ensure_ledger_unique_key

	ensure_ledger_unique_key()
