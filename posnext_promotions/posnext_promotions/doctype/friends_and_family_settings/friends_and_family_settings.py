# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate

from posnext_promotions.promotions import friends_and_family
from posnext_promotions.promotions import friends_and_family_eligibility as eligibility


class FriendsandFamilySettings(Document):
	def validate(self):
		self._validate_percentage()
		self._validate_dates()
		self._validate_scope_tables()
		self._validate_eligibility()

	def _validate_percentage(self):
		percentage = self.discount_percentage
		if percentage is None:
			frappe.throw(_("Discount Percentage is required"))
		if percentage < 0 or percentage > 100:
			frappe.throw(_("Discount Percentage must be between 0 and 100"))
		if cint(self.enabled) and percentage <= 0:
			frappe.throw(_("Discount Percentage must be greater than 0 while the program is enabled"))

	def _validate_dates(self):
		if self.start_date and self.end_date and getdate(self.start_date) > getdate(self.end_date):
			frappe.throw(_("End Date cannot be before Start Date"))

	def _validate_scope_tables(self):
		# Clear unused tables when scope changes so saved state stays coherent.
		if self.discount_scope != friends_and_family.SCOPE_BRAND:
			self.applicable_brands = []
		if self.discount_scope != friends_and_family.SCOPE_ITEM_GROUP:
			self.applicable_item_groups = []
		if self.discount_scope != friends_and_family.SCOPE_ENTIRE_CART:
			self.excluded_brands = []

		if not cint(self.enabled):
			return
		if self.discount_scope == friends_and_family.SCOPE_BRAND and not self.applicable_brands:
			frappe.throw(_("Add at least one Applicable Brand for the Brand scope"))
		if self.discount_scope == friends_and_family.SCOPE_ITEM_GROUP and not self.applicable_item_groups:
			frappe.throw(_("Add at least one Applicable Item Group for the Item Group scope"))

	def _validate_eligibility(self):
		if self.eligibility_basis not in eligibility.BASES:
			self.eligibility_basis = eligibility.BASIS_GROUP_OR_FLAG

		if self.eligibility_basis == eligibility.BASIS_FLAG:
			self.eligible_customer_groups = []
		else:
			seen = set()
			for row in list(self.eligible_customer_groups or []):
				if row.customer_group in seen:
					frappe.throw(
						_("Customer Group {0} is listed more than once").format(frappe.bold(row.customer_group))
					)
				seen.add(row.customer_group)

		if (
			cint(self.enabled)
			and self.eligibility_basis == eligibility.BASIS_GROUP
			and not self.eligible_customer_groups
		):
			frappe.throw(_("Add at least one Eligible Customer Group when eligibility is by Customer Group"))


def get_friends_and_family_settings():
	"""Return the singleton settings document."""
	return friends_and_family.get_settings()


def is_friends_and_family_active(on_date=None):
	"""True when the program is enabled, configured and within its start/end dates."""
	return friends_and_family.get_active_program(on_date) is not None
