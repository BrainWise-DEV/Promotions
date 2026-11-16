# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from posnext_promotions.api.friends_and_family import (
	check_friends_and_family_eligibility,
	get_friends_and_family_program,
)
from posnext_promotions.promotions import friends_and_family_eligibility as eligibility_module
from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
	REASON_EMAIL_DOMAIN_NOT_APPROVED,
	REASON_EMAIL_NOT_VERIFIED,
	REASON_EMAIL_VERIFIED,
	REASON_NO_APPROVED_DOMAINS,
	REASON_CUSTOMER_DISABLED,
	REASON_CUSTOMER_NOT_FOUND,
	REASON_NO_CUSTOMER,
	REASON_PROGRAM_INACTIVE,
	ensure_customer_flag_field,
	get_customer_eligibility,
	get_program_for_customer,
)
from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows, give_verified_email

DATE = "2026-10-05"
OTHER_GROUP = "_Test FF Other Customer Group"
ELIGIBLE_CUSTOMER = "_Test FF Domain Customer"
OTHER_DOMAIN_CUSTOMER = "_Test FF Other Domain Customer"
UNVERIFIED_CUSTOMER = "_Test FF Unverified Customer"
DISABLED_CUSTOMER = "_Test FF Disabled Domain Customer"
MANAGER_USER = "test_ff_manager@example.com"
CASHIER_USER = "test_ff_cashier@example.com"


def _ensure_user(email, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.add_roles(*roles)


class TestFriendsandFamilyEligibility(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()
		root = frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ""}) or (
			"All Customer Groups"
		)
		if not frappe.db.exists("Customer Group", OTHER_GROUP):
			frappe.get_doc(
				{
					"doctype": "Customer Group",
					"customer_group_name": OTHER_GROUP,
					"parent_customer_group": root,
					"is_group": 0,
				}
			).insert()
		for name, disabled in (
			(ELIGIBLE_CUSTOMER, 0),
			(OTHER_DOMAIN_CUSTOMER, 0),
			(UNVERIFIED_CUSTOMER, 0),
			(DISABLED_CUSTOMER, 1),
		):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc(
					{"doctype": "Customer", "customer_name": name, "customer_group": OTHER_GROUP}
				).insert()
			frappe.db.set_value("Customer", name, {"disabled": disabled, FLAG_FIELD: 0})
		give_verified_email(ELIGIBLE_CUSTOMER)
		give_verified_email(DISABLED_CUSTOMER)
		frappe.db.set_value("Customer", OTHER_DOMAIN_CUSTOMER, "email_id", "other@gmail.com")
		if eligibility_module.has_verification_fields():
			frappe.db.set_value(
				"Customer",
				OTHER_DOMAIN_CUSTOMER,
				{
					eligibility_module.VERIFIED_FLAG_FIELD: 0,
					eligibility_module.VERIFIED_EMAIL_FIELD: None,
				},
			)
			from posnext_promotions.promotions.friends_and_family_testing import TEST_EMAIL_DOMAIN

			frappe.db.set_value(
				"Customer",
				UNVERIFIED_CUSTOMER,
				{
					"email_id": f"unverified@{TEST_EMAIL_DOMAIN}",
					eligibility_module.VERIFIED_FLAG_FIELD: 0,
					eligibility_module.VERIFIED_EMAIL_FIELD: None,
				},
			)
		else:
			frappe.db.set_value("Customer", UNVERIFIED_CUSTOMER, "email_id", "unverified@fftest.example")
		_ensure_user(MANAGER_USER, ["Sales Manager", "Sales User"])
		_ensure_user(CASHIER_USER, ["Sales User"])

	def tearDown(self):
		frappe.set_user("Administrator")
		give_verified_email(ELIGIBLE_CUSTOMER)
		give_verified_email(DISABLED_CUSTOMER)
		frappe.db.set_value("Customer", OTHER_DOMAIN_CUSTOMER, "email_id", "other@gmail.com")
		frappe.db.set_value("Customer", UNVERIFIED_CUSTOMER, "email_id", "unverified@fftest.example")
		if eligibility_module.has_verification_fields():
			for name in (OTHER_DOMAIN_CUSTOMER, UNVERIFIED_CUSTOMER):
				frappe.db.set_value(
					"Customer",
					name,
					{
						eligibility_module.VERIFIED_FLAG_FIELD: 0,
						eligibility_module.VERIFIED_EMAIL_FIELD: None,
					},
				)

	def _configure(self, **values):
		doc = frappe.get_single("Friends and Family Settings")
		doc.update(
			{
				"enabled": 1,
				"approved_domains": approved_domain_rows(),
				"discount_percentage": 30,
				"discount_scope": "Entire Cart",
				"start_date": None,
				"end_date": None,
				"applicable_brands": [],
				"applicable_item_groups": [],
				"applicable_items": [],
				"excluded_brands": [],
				"excluded_items": [],
				"excluded_item_groups": [],
				"eligibility_basis": "Customer Group or Flag",
				"eligible_customer_groups": [],
			}
		)
		doc.update(values)
		doc.save()
		return doc

	def _check(self, customer):
		return get_customer_eligibility(customer, DATE)

	def test_duplicate_group_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(
				eligible_customer_groups=[
					{"customer_group": OTHER_GROUP},
					{"customer_group": OTHER_GROUP},
				]
			)

	def test_verified_approved_domain_is_eligible(self):
		self._configure()
		result = self._check(ELIGIBLE_CUSTOMER)
		self.assertTrue(result.eligible)
		self.assertEqual(result.reason, REASON_EMAIL_VERIFIED)
		self.assertTrue(result.ff_member)
		self.assertEqual(get_program_for_customer(ELIGIBLE_CUSTOMER, DATE).discount_percentage, 30)

	def test_other_domain_is_not_eligible(self):
		self._configure()
		result = self._check(OTHER_DOMAIN_CUSTOMER)
		self.assertFalse(result.eligible)
		self.assertEqual(result.reason, REASON_EMAIL_DOMAIN_NOT_APPROVED)
		self.assertFalse(result.ff_member)
		self.assertIsNone(get_program_for_customer(OTHER_DOMAIN_CUSTOMER, DATE))

	def test_unverified_approved_domain_is_not_eligible_but_ff_member(self):
		if not eligibility_module.has_verification_fields():
			self.skipTest("POSNext email verification fields are not installed")
		self._configure()
		result = self._check(UNVERIFIED_CUSTOMER)
		self.assertFalse(result.eligible)
		self.assertEqual(result.reason, REASON_EMAIL_NOT_VERIFIED)
		self.assertTrue(result.ff_member)
		self.assertTrue(eligibility_module.is_customer_ff_member(UNVERIFIED_CUSTOMER))

	def test_disabled_customer_is_not_eligible(self):
		self._configure()
		self.assertEqual(self._check(DISABLED_CUSTOMER).reason, REASON_CUSTOMER_DISABLED)

	def test_missing_and_blank_customer(self):
		self._configure()
		self.assertEqual(self._check("_Test FF Does Not Exist").reason, REASON_CUSTOMER_NOT_FOUND)
		self.assertEqual(self._check("").reason, REASON_NO_CUSTOMER)
		self.assertEqual(self._check(None).reason, REASON_NO_CUSTOMER)

	def test_inactive_program_blocks_eligible_customer(self):
		self._configure(enabled=0)
		self.assertEqual(self._check(ELIGIBLE_CUSTOMER).reason, REASON_PROGRAM_INACTIVE)
		self._configure(end_date="2026-10-01")
		self.assertEqual(self._check(ELIGIBLE_CUSTOMER).reason, REASON_PROGRAM_INACTIVE)

	def test_api_payload_for_eligible_customer(self):
		self._configure(discount_percentage=25)
		payload = check_friends_and_family_eligibility(ELIGIBLE_CUSTOMER, DATE)
		self.assertTrue(payload["eligible"])
		self.assertEqual(payload["reason"], REASON_EMAIL_VERIFIED)
		self.assertEqual(payload["discount_percentage"], 25)
		self.assertTrue(payload["program_active"])
		self.assertEqual(payload["program"]["discount_scope"], "Entire Cart")
		self.assertTrue(payload["message"])

	def test_api_payload_for_ineligible_customer(self):
		self._configure()
		payload = check_friends_and_family_eligibility(OTHER_DOMAIN_CUSTOMER, DATE)
		self.assertFalse(payload["eligible"])
		self.assertEqual(payload["discount_percentage"], 0)
		self.assertIsNone(payload["program"])
		self.assertTrue(payload["program_active"])

		self._configure(enabled=0)
		payload = check_friends_and_family_eligibility(ELIGIBLE_CUSTOMER, DATE)
		self.assertFalse(payload["eligible"])
		self.assertFalse(payload["program_active"])

	def test_program_payload_carries_domain_eligibility_rules(self):
		self._configure()
		rules = get_friends_and_family_program(DATE)["eligibility"]
		self.assertEqual(rules["basis"], "approved_email_domain")
		self.assertEqual(rules["customer_groups"], [])
		self.assertIsNone(rules["flag_field"])
		self.assertIn("fftest.example", rules["approved_domains"])
		self.assertTrue(rules["requires_verified_email"])

	def test_cashier_cannot_set_flag(self):
		frappe.set_user(CASHIER_USER)
		customer = frappe.get_doc("Customer", OTHER_DOMAIN_CUSTOMER)
		customer.set(FLAG_FIELD, 1)
		with self.assertRaises(frappe.PermissionError):
			customer.save(ignore_permissions=True)

	def test_manager_can_set_flag(self):
		frappe.set_user(MANAGER_USER)
		customer = frappe.get_doc("Customer", OTHER_DOMAIN_CUSTOMER)
		customer.set(FLAG_FIELD, 1)
		customer.save(ignore_permissions=True)
		frappe.set_user("Administrator")
		frappe.db.set_value("Customer", OTHER_DOMAIN_CUSTOMER, FLAG_FIELD, 0)

	def test_flag_field_is_hidden_after_sync(self):
		cf = frappe.db.get_value("Custom Field", {"dt": "Customer", "fieldname": FLAG_FIELD}, "name")
		frappe.db.set_value("Custom Field", cf, "hidden", 0)
		ensure_customer_flag_field()
		self.assertEqual(cint(frappe.db.get_value("Custom Field", cf, "hidden")), 1)

	def test_domain_rows_normalised_and_validated(self):
		doc = self._configure(
			approved_domains=[
				{"email_domain": "  @Miraaya.COM ", "company": " Miraaya "},
				{"email_domain": "https://www.Partner.co.uk/", "enabled": 1},
			]
		)
		self.assertEqual([r.email_domain for r in doc.approved_domains], ["miraaya.com", "partner.co.uk"])
		self.assertEqual(doc.approved_domains[0].company, "Miraaya")
		for bad in ("miraaya", "user@miraaya.com", "mira aya.com", "-bad.com", "miraaya.c0m", "@", "miraaya..com"):
			with self.assertRaises(frappe.ValidationError, msg=bad):
				self._configure(approved_domains=[{"email_domain": bad}])
		with self.assertRaises(frappe.ValidationError):
			self._configure(approved_domains=[{"email_domain": "miraaya.com"}, {"email_domain": "@MIRAAYA.com"}])

	def test_enabled_program_requires_an_enabled_domain(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(approved_domains=[])
		with self.assertRaises(frappe.ValidationError):
			self._configure(approved_domains=[{"email_domain": "miraaya.com", "enabled": 0}])
		doc = self._configure(enabled=0, approved_domains=[])
		self.assertFalse(doc.approved_domains)

	def test_email_domain_matching_is_exact(self):
		settings = frappe._dict(approved_domains=[frappe._dict(email_domain="miraaya.com", enabled=1)])
		self.assertTrue(eligibility_module.is_approved_email(" Ali@Miraaya.COM ", settings))
		self.assertFalse(eligibility_module.is_approved_email("ali@mail.miraaya.com", settings))
		self.assertFalse(eligibility_module.is_approved_email("ali@miraaya.com.evil.io", settings))
		self.assertFalse(eligibility_module.is_approved_email("ali@notmiraaya.com", settings))
		self.assertFalse(eligibility_module.is_approved_email("a@b@miraaya.com", settings))
		self.assertFalse(eligibility_module.is_approved_email("", settings))
		disabled = frappe._dict(approved_domains=[frappe._dict(email_domain="miraaya.com", enabled=0)])
		self.assertFalse(eligibility_module.is_approved_email("ali@miraaya.com", disabled))
		both = frappe._dict(
			approved_domains=[
				frappe._dict(email_domain="miraaya.com", enabled=1),
				frappe._dict(email_domain="mail.miraaya.com", enabled=1),
			]
		)
		self.assertTrue(eligibility_module.is_approved_email("ali@mail.miraaya.com", both))

	def _set_email(self, customer, email, verified=None):
		frappe.db.set_value("Customer", customer, "email_id", email)
		if eligibility_module.has_verification_fields():
			frappe.db.set_value(
				"Customer",
				customer,
				{
					eligibility_module.VERIFIED_FLAG_FIELD: 1 if verified else 0,
					eligibility_module.VERIFIED_EMAIL_FIELD: verified or None,
				},
			)

	def test_no_enabled_domain_blocks_everyone(self):
		self._configure()
		with patch.object(eligibility_module, "get_approved_domains", return_value=[]):
			result = self._check(ELIGIBLE_CUSTOMER)
			self.assertFalse(result.eligible)
			self.assertEqual(result.reason, REASON_NO_APPROVED_DOMAINS)
			self.assertFalse(result.ff_member)
			self.assertIn("no approved email domains", result.message)
			self.assertIsNone(get_program_for_customer(ELIGIBLE_CUSTOMER, DATE))
			self.assertEqual(self._check(OTHER_DOMAIN_CUSTOMER).reason, REASON_NO_APPROVED_DOMAINS)

	def test_member_needs_verified_email_on_approved_domain(self):
		if not eligibility_module.has_verification_fields():
			self.skipTest("POSNext email verification fields are not installed")
		self._configure(approved_domains=[{"email_domain": "miraaya.com"}])

		self._set_email(ELIGIBLE_CUSTOMER, "Staff@Miraaya.com", verified="staff@miraaya.com")
		result = self._check(ELIGIBLE_CUSTOMER)
		self.assertTrue(result.eligible)
		self.assertEqual(result.reason, REASON_EMAIL_VERIFIED)
		self.assertEqual(result.email_domain, "miraaya.com")
		self.assertTrue(result.ff_member)

		for email in ("staff@gmail.com", "", "x@sub.miraaya.com"):
			self._set_email(ELIGIBLE_CUSTOMER, email, verified=email or None)
			result = self._check(ELIGIBLE_CUSTOMER)
			self.assertEqual(result.reason, REASON_EMAIL_DOMAIN_NOT_APPROVED, email)
			self.assertIn("approved company domain", result.message)
			self.assertFalse(result.ff_member)

		self._set_email(UNVERIFIED_CUSTOMER, "flag@miraaya.com", verified=None)
		result = self._check(UNVERIFIED_CUSTOMER)
		self.assertEqual(result.reason, REASON_EMAIL_NOT_VERIFIED)
		self.assertIn("verify the email", result.message)
		self.assertTrue(result.ff_member)

		self._set_email(UNVERIFIED_CUSTOMER, "other@miraaya.com", verified="flag@miraaya.com")
		self.assertEqual(self._check(UNVERIFIED_CUSTOMER).reason, REASON_EMAIL_NOT_VERIFIED)

	def test_email_gate_in_api_payload(self):
		self._configure(approved_domains=[{"email_domain": "miraaya.com"}])
		self._set_email(ELIGIBLE_CUSTOMER, "staff@gmail.com")
		payload = check_friends_and_family_eligibility(ELIGIBLE_CUSTOMER, DATE)
		self.assertFalse(payload["eligible"])
		self.assertEqual(payload["reason"], REASON_EMAIL_DOMAIN_NOT_APPROVED)
		self.assertFalse(payload["ff_member"])
		self.assertTrue(payload["message"])
		self.assertEqual(payload["discount_percentage"], 0)
