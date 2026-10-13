# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from posnext_promotions.promotions import friends_and_family_messages as ff_messages


class FriendsandFamilyMessage(Document):
	def validate(self):
		self.message_ar = (self.message_ar or "").strip()
		self.message_en = (self.message_en or "").strip()

	def on_update(self):
		ff_messages.clear_cache()

	def on_trash(self):
		ff_messages.clear_cache()
