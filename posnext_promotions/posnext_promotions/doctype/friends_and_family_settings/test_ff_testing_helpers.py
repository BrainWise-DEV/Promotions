# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Tests for the F&F test helpers themselves."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.promotions import friends_and_family_testing as testing
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
		self.assertEqual(state(), before)

	def test_failed_restore_does_not_mask_body_error(self):
		def broken_restore(_snapshot):
			raise RuntimeError("restore failed")

		with patch.object(testing, "_restore_ff_settings", broken_restore), patch.object(frappe, "logger") as logger:
			with self.assertRaisesRegex(ValueError, "body failed"):
				with ff_settings_guard():
					raise ValueError("body failed")
		logger.return_value.exception.assert_called_once()
