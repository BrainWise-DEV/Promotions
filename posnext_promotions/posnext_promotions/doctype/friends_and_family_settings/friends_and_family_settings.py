# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FriendsandFamilySettings(Document):
	def validate(self):
		self._validate_percentage()
		self._validate_dates()
		self._validate_scope_tables()

	def _validate_percentage(self):
		percentage = self.discount_percentage
		if percentage is None:
			frappe.throw(frappe._("Discount Percentage is required"))
		if percentage < 0 or percentage > 100:
			frappe.throw(frappe._("Discount Percentage must be between 0 and 100"))

	def _validate_dates(self):
		if self.start_date and self.end_date and self.start_date > self.end_date:
			frappe.throw(frappe._("End Date cannot be before Start Date"))

	def _validate_scope_tables(self):
		# Clear unused tables when scope changes so saved state stays coherent.
		if self.discount_scope != "Brand":
			self.applicable_brands = []
		if self.discount_scope != "Item Group":
			self.applicable_item_groups = []
		if self.discount_scope != "Entire Cart":
			self.excluded_brands = []


def get_friends_and_family_settings():
	"""Return the singleton settings document (for the discount engine)."""
	return frappe.get_single("Friends and Family Settings")


def is_friends_and_family_active(on_date=None):
	"""True when the program is enabled and within optional start/end dates."""
	from frappe.utils import getdate, today

	settings = get_friends_and_family_settings()
	if not settings.enabled:
		return False
	on_date = getdate(on_date or today())
	if settings.start_date and on_date < getdate(settings.start_date):
		return False
	if settings.end_date and on_date > getdate(settings.end_date):
		return False
	return True
