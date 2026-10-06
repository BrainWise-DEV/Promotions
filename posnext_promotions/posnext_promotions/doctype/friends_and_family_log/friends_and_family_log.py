# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class FriendsandFamilyLog(Document):
	"""Append-only F&F audit entry (PN-131). Written by the system, never edited."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Friends and Family Log entries cannot be edited"), frappe.PermissionError)
