# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.promotions.friends_and_family_eligibility import (
	benefit_provider,
	ensure_customer_flag_field,
)
from posnext_promotions.promotions.friends_and_family_log import LOG_DOCTYPE
from posnext_promotions.promotions.friends_and_family_testing import (
	approved_domain_rows,
	ff_settings_guard,
	give_verified_email,
)

HOOK_PATH = "posnext_promotions.promotions.friends_and_family_eligibility.benefit_provider"
CUSTOMER = "_Test FF Provider Customer"
DOMAIN = "acme.example"


class TestFFBenefitProvider(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()
		if not frappe.db.exists("Customer", CUSTOMER):
			group = frappe.db.get_value("Customer Group", {"is_group": 0})
			frappe.get_doc({"doctype": "Customer", "customer_name": CUSTOMER, "customer_group": group}).insert()

	def test_benefit_provider_shape(self):
		with ff_settings_guard():
			settings = frappe.get_single("Friends and Family Settings")
			settings.update({"enabled": 1, "approved_domains": approved_domain_rows(DOMAIN)})
			settings.save(ignore_permissions=True)
			give_verified_email(CUSTOMER, domain=DOMAIN)
			provider = benefit_provider()
			self.assertEqual(set(provider), {"is_member", "on_verification_change"})
			self.assertTrue(provider["is_member"](CUSTOMER, pos_profile=None))

	def test_verification_change_logged_domain_only(self):
		address = f"staff@{DOMAIN}"
		before = frappe.db.count(LOG_DOCTYPE)
		benefit_provider()["on_verification_change"](CUSTOMER, True, DOMAIN, "otp")
		self.assertEqual(frappe.db.count(LOG_DOCTYPE), before + 1)
		row = frappe.get_all(
			LOG_DOCTYPE, filters={"customer": CUSTOMER}, fields=["*"], order_by="creation desc", limit=1
		)[0]
		blob = " ".join(str(v) for v in row.values())
		self.assertIn(DOMAIN, row["details"])
		self.assertNotIn("staff@", blob)
		self.assertNotIn(address, blob)

	def test_hook_declared(self):
		self.assertIn(HOOK_PATH, frappe.get_hooks("pos_next_customer_benefit_providers"))
