# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class FriendsandFamilyUsage(Document):
	"""One F&F ledger row per invoice line. Written by the system, never edited."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Friends and Family Usage rows cannot be edited"), frappe.PermissionError)


def on_doctype_update():
	from posnext_promotions.promotions.ff_ledger import ensure_ledger_unique_key

	ensure_ledger_unique_key()
