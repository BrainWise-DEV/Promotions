# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

from posnext_promotions.promotions import friends_and_family_messages as ff_messages

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class FriendsandFamilyMessageEvent(Document):
	def validate(self):
		self.event_code = (self.event_code or "").strip()
		if not _CODE_RE.match(self.event_code):
			frappe.throw(
				_("Event Code {0} must be snake_case (lowercase letters, digits and _; starting with a letter)").format(
					frappe.bold(self.event_code)
				)
			)
		standard = self.event_code in ff_messages.STANDARD_EVENTS
		if standard and not self.flags.allow_standard and self.is_new():
			frappe.throw(_("{0} is a standard event and is created automatically").format(frappe.bold(self.event_code)))
		self.is_standard = 1 if standard else 0

	def on_update(self):
		ff_messages.clear_cache()

	def on_trash(self):
		if self.is_standard and not frappe.flags.in_migrate:
			frappe.throw(_("Standard events are used by the Friends & Family engine and cannot be deleted; disable instead"))
		ff_messages.clear_cache()
