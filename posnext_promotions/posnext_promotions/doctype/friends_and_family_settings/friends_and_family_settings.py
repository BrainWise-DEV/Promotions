# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate

from posnext_promotions.promotions import friends_and_family
from posnext_promotions.promotions import friends_and_family_eligibility as eligibility


class FriendsandFamilySettings(Document):
	def validate(self):
		self._validate_percentage()
		self._validate_dates()
		self._validate_scope_tables()
		self._validate_exclusion_tables()
		self._validate_eligibility()
		self._validate_caps()

	def on_update(self):
		# get_cached_doc is cleared by Document.save; document for callers that
		# the next apply_offers / apply_friends_and_family_discount call reloads
		# inclusions and exclusions without a restart.
		frappe.clear_cache(doctype=friends_and_family.SETTINGS_DOCTYPE)

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
		# Clear unused inclusion tables when scope changes so saved state stays coherent.
		if self.discount_scope != friends_and_family.SCOPE_BRAND:
			self.applicable_brands = []
		if self.discount_scope != friends_and_family.SCOPE_ITEM_GROUP:
			self.applicable_item_groups = []
		if self.discount_scope != friends_and_family.SCOPE_ITEM_CODE:
			self.applicable_items = []
		# Brand exclusions only apply to Entire Cart.
		if self.discount_scope != friends_and_family.SCOPE_ENTIRE_CART:
			self.excluded_brands = []

		if not cint(self.enabled):
			return
		if self.discount_scope == friends_and_family.SCOPE_BRAND and not self.applicable_brands:
			frappe.throw(_("Add at least one Applicable Brand for the Brand scope"))
		if self.discount_scope == friends_and_family.SCOPE_ITEM_GROUP and not self.applicable_item_groups:
			frappe.throw(_("Add at least one Applicable Item Group for the Item Group scope"))
		if self.discount_scope == friends_and_family.SCOPE_ITEM_CODE and not self.applicable_items:
			frappe.throw(_("Add at least one Applicable Item for the Item Code scope"))

	def _validate_exclusion_tables(self):
		"""Deduplicate exclusion rows; exclusions win over every scope."""
		self._dedupe_link_table("excluded_items", "item_code", _("Excluded Item"))
		self._dedupe_link_table("excluded_item_groups", "item_group", _("Excluded Item Group"))
		self._dedupe_link_table("excluded_brands", "brand", _("Excluded Brand"))
		if self.discount_scope == friends_and_family.SCOPE_ITEM_CODE:
			self._dedupe_link_table("applicable_items", "item_code", _("Applicable Item"))

	def _dedupe_link_table(self, table_field: str, value_field: str, label):
		seen = set()
		for row in list(self.get(table_field) or []):
			value = row.get(value_field)
			if not value:
				continue
			if value in seen:
				frappe.throw(_("{0} {1} is listed more than once").format(label, frappe.bold(value)))
			seen.add(value)

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


	def _validate_caps(self):
		"""PN-129: caps are money amounts; 0 (or blank) means no cap."""
		for fieldname, label in (
			("max_discount_per_order", _("Max Discount per Order")),
			("max_discount_per_customer_month", _("Max Discount per Customer per Month")),
		):
			value = flt(self.get(fieldname))
			if value < 0:
				frappe.throw(_("{0} cannot be negative").format(label))
			self.set(fieldname, value)


def get_friends_and_family_settings():
	"""Return the singleton settings document."""
	return friends_and_family.get_settings()


def is_friends_and_family_active(on_date=None):
	"""True when the program is enabled, configured and within its start/end dates."""
	return friends_and_family.get_active_program(on_date) is not None
