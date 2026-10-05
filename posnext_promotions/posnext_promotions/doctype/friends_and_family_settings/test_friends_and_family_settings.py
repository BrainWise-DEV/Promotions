# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestFriendsandFamilySettings(FrappeTestCase):
	def test_default_percentage_and_scope(self):
		doc = frappe.get_single("Friends and Family Settings")
		doc.enabled = 1
		doc.discount_percentage = 30
		doc.discount_scope = "Entire Cart"
		doc.start_date = "2026-10-01"
		doc.end_date = "2026-12-31"
		doc.save()
		doc.reload()
		self.assertEqual(doc.discount_percentage, 30)
		self.assertEqual(doc.discount_scope, "Entire Cart")

	def test_end_date_before_start_raises(self):
		doc = frappe.get_single("Friends and Family Settings")
		doc.discount_percentage = 30
		doc.discount_scope = "Entire Cart"
		doc.start_date = "2026-12-01"
		doc.end_date = "2026-11-01"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_percentage_out_of_range_raises(self):
		doc = frappe.get_single("Friends and Family Settings")
		doc.discount_scope = "Entire Cart"
		doc.start_date = None
		doc.end_date = None
		doc.discount_percentage = 150
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_scope_clears_unused_tables(self):
		doc = frappe.get_single("Friends and Family Settings")
		doc.discount_percentage = 30
		doc.discount_scope = "Brand"
		doc.append("applicable_brands", {"brand": "Test Brand"})
		# Brand may not exist — use a mock-friendly approach: only clear logic
		doc.discount_scope = "Entire Cart"
		doc.validate()
		self.assertEqual(doc.applicable_brands, [])
