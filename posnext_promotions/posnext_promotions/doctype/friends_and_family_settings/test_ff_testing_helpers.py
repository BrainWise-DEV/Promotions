# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Tests for the F&F test helpers themselves."""

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.promotions.friends_and_family_testing import (
	FF_SETTINGS,
	approved_domain_rows,
	ff_settings_guard,
)


class TestFFSettingsGuard(FrappeTestCase):
	def test_guard_reverts_field_and_child_rows(self):
		def state():
			doc = frappe.get_doc(FF_SETTINGS)
			return (
				doc.enabled,
				[(r.email_domain, r.company, r.enabled) for r in doc.approved_domains],
			)

		before = state()
		with ff_settings_guard():
			doc = frappe.get_doc(FF_SETTINGS)
			doc.enabled = 0 if before[0] else 1
			doc.set("approved_domains", [])
			for row in approved_domain_rows("guardtest.example", "Guard Co"):
				doc.append("approved_domains", row)
			doc.save(ignore_permissions=True)
			self.assertNotEqual(state(), before)
		frappe.db.commit()
		self.assertEqual(state(), before)
