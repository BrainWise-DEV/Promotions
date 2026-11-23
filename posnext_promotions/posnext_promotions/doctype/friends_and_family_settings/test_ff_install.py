# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""after_install parity with after_migrate, and the exported contract constant."""

import frappe
from frappe.tests.utils import FrappeTestCase

import posnext_promotions
from posnext_promotions import install
from posnext_promotions.promotions import ff_ledger, friends_and_family_messages
from posnext_promotions.promotions.friends_and_family_caps import FIELD_DISCOUNT_SOURCE
from posnext_promotions.promotions.friends_and_family_eligibility import CUSTOMER_FLAG_FIELD_SPEC


class TestFFInstall(FrappeTestCase):
	def test_after_install_hook_declared(self):
		hooks = frappe.get_hooks("after_install", app_name="posnext_promotions")
		self.assertIn("posnext_promotions.install.after_install", hooks)

	def test_after_install_idempotent(self):
		install.after_install()
		install.after_install()
		for dt in ("Sales Invoice Item", "POS Invoice Item"):
			if frappe.db.exists("DocType", dt):
				self.assertTrue(frappe.get_meta(dt).has_field(FIELD_DISCOUNT_SOURCE), dt)
		codes = set(frappe.get_all("Friends and Family Message Event", pluck="name"))
		self.assertEqual(codes, set(friends_and_family_messages.STANDARD_EVENTS))
		table = f"tab{ff_ledger.LEDGER_DOCTYPE}"
		self.assertTrue(frappe.db.has_index(table, ff_ledger.UNIQUE_KEY))
		self.assertTrue(frappe.get_meta("Customer").has_field(CUSTOMER_FLAG_FIELD_SPEC["fieldname"]))

	def test_contract_constant(self):
		self.assertEqual(posnext_promotions.FF_CONTRACT, 5)
