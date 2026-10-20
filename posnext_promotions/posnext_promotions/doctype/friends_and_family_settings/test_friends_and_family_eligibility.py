# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows, give_verified_email
from posnext_promotions.api.friends_and_family import (
	check_friends_and_family_eligibility,
	get_friends_and_family_program,
)
from posnext_promotions.promotions import friends_and_family_eligibility as eligibility_module
from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
	REASON_EMAIL_DOMAIN_NOT_APPROVED,
	REASON_EMAIL_NOT_VERIFIED,
	REASON_NO_APPROVED_DOMAINS,
	REASON_CUSTOMER_DISABLED,
	REASON_CUSTOMER_FLAG,
	REASON_CUSTOMER_GROUP,
	REASON_CUSTOMER_NOT_FOUND,
	REASON_NO_CUSTOMER,
	REASON_NOT_ELIGIBLE,
	REASON_PROGRAM_INACTIVE,
	ensure_customer_flag_field,
	get_customer_eligibility,
	get_program_for_customer,
)

DATE = "2026-10-05"
# A leaf group configured directly, and a parent group configured for its children.
FF_GROUP = "_Test FF Customer Group"
FF_PARENT_GROUP = "_Test FF Customer Parent Group"
FF_CHILD_GROUP = "_Test FF Customer Child Group"
OTHER_GROUP = "_Test FF Other Customer Group"

GROUP_CUSTOMER = "_Test FF Group Customer"
CHILD_GROUP_CUSTOMER = "_Test FF Child Group Customer"
FLAG_CUSTOMER = "_Test FF Flag Customer"
PLAIN_CUSTOMER = "_Test FF Plain Customer"
DISABLED_CUSTOMER = "_Test FF Disabled Customer"
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
		for name, parent, is_group in (
			(FF_GROUP, root, 0),
			(FF_PARENT_GROUP, root, 1),
			(FF_CHILD_GROUP, FF_PARENT_GROUP, 0),
			(OTHER_GROUP, root, 0),
		):
			if not frappe.db.exists("Customer Group", name):
				frappe.get_doc(
					{
						"doctype": "Customer Group",
						"customer_group_name": name,
						"parent_customer_group": parent,
						"is_group": is_group,
					}
				).insert()
		for name, group, flag, disabled in (
			(GROUP_CUSTOMER, FF_GROUP, 0, 0),
			(CHILD_GROUP_CUSTOMER, FF_CHILD_GROUP, 0, 0),
			(FLAG_CUSTOMER, OTHER_GROUP, 1, 0),
			(PLAIN_CUSTOMER, OTHER_GROUP, 0, 0),
			(DISABLED_CUSTOMER, FF_GROUP, 1, 1),
		):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc(
					{"doctype": "Customer", "customer_name": name, "customer_group": group}
				).insert()
			frappe.db.set_value("Customer", name, {FLAG_FIELD: flag, "disabled": disabled})
			give_verified_email(name)
		_ensure_user(MANAGER_USER, ["Sales Manager", "Sales User"])
		_ensure_user(CASHIER_USER, ["Sales User"])

	def tearDown(self):
		frappe.set_user("Administrator")
		# Email tests change customer emails; restore the verified fixture email.
		for name in (GROUP_CUSTOMER, CHILD_GROUP_CUSTOMER, FLAG_CUSTOMER, PLAIN_CUSTOMER, DISABLED_CUSTOMER):
			give_verified_email(name)

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
				"eligible_customer_groups": [
					{"customer_group": FF_GROUP},
					{"customer_group": FF_PARENT_GROUP},
				],
			}
		)
		doc.update(values)
		doc.save()
		return doc

	def _check(self, customer):
		return get_customer_eligibility(customer, DATE)

	# ---- settings validation ---------------------------------------------

	def test_group_basis_requires_groups_when_enabled(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(eligibility_basis="Customer Group", eligible_customer_groups=[])

	def test_group_or_flag_basis_allows_no_groups(self):
		self._configure(eligible_customer_groups=[])
		self.assertEqual(self._check(FLAG_CUSTOMER).reason, REASON_CUSTOMER_FLAG)

	def test_flag_basis_clears_groups(self):
		doc = self._configure(eligibility_basis="Customer Flag")
		self.assertEqual(doc.eligible_customer_groups, [])

	def test_duplicate_group_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(
				eligible_customer_groups=[{"customer_group": FF_GROUP}, {"customer_group": FF_GROUP}]
			)

	# ---- eligibility -----------------------------------------------------

	def test_group_member_is_eligible(self):
		self._configure()
		result = self._check(GROUP_CUSTOMER)
		self.assertTrue(result.eligible)
		self.assertEqual(result.reason, REASON_CUSTOMER_GROUP)
		self.assertEqual(result.matched_customer_group, FF_GROUP)
		self.assertEqual(get_program_for_customer(GROUP_CUSTOMER, DATE).discount_percentage, 30)

	def test_child_group_member_is_eligible(self):
		self._configure()
		result = self._check(CHILD_GROUP_CUSTOMER)
		self.assertTrue(result.eligible)
		self.assertEqual(result.matched_customer_group, FF_PARENT_GROUP)

	def test_flagged_customer_is_eligible(self):
		self._configure()
		result = self._check(FLAG_CUSTOMER)
		self.assertTrue(result.eligible)
		self.assertEqual(result.reason, REASON_CUSTOMER_FLAG)

	def test_unmatched_customer_is_not_eligible(self):
		self._configure()
		result = self._check(PLAIN_CUSTOMER)
		self.assertFalse(result.eligible)
		self.assertEqual(result.reason, REASON_NOT_ELIGIBLE)
		self.assertIsNone(get_program_for_customer(PLAIN_CUSTOMER, DATE))

	def test_group_only_basis_ignores_flag(self):
		self._configure(eligibility_basis="Customer Group")
		self.assertFalse(self._check(FLAG_CUSTOMER).eligible)
		self.assertTrue(self._check(GROUP_CUSTOMER).eligible)

	def test_flag_only_basis_ignores_group(self):
		self._configure(eligibility_basis="Customer Flag")
		self.assertFalse(self._check(GROUP_CUSTOMER).eligible)
		self.assertTrue(self._check(FLAG_CUSTOMER).eligible)

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
		self.assertEqual(self._check(GROUP_CUSTOMER).reason, REASON_PROGRAM_INACTIVE)
		self._configure(end_date="2026-10-01")
		self.assertEqual(self._check(GROUP_CUSTOMER).reason, REASON_PROGRAM_INACTIVE)

	# ---- POS API ---------------------------------------------------------

	def test_api_payload_for_eligible_customer(self):
		self._configure(discount_percentage=25)
		payload = check_friends_and_family_eligibility(GROUP_CUSTOMER, DATE)
		self.assertTrue(payload["eligible"])
		self.assertEqual(payload["reason"], REASON_CUSTOMER_GROUP)
		self.assertEqual(payload["discount_percentage"], 25)
		self.assertTrue(payload["program_active"])
		self.assertEqual(payload["program"]["discount_scope"], "Entire Cart")
		self.assertTrue(payload["message"])

	def test_api_payload_for_ineligible_customer(self):
		self._configure()
		payload = check_friends_and_family_eligibility(PLAIN_CUSTOMER, DATE)
		self.assertFalse(payload["eligible"])
		self.assertEqual(payload["discount_percentage"], 0)
		self.assertIsNone(payload["program"])
		self.assertTrue(payload["program_active"])

		self._configure(enabled=0)
		payload = check_friends_and_family_eligibility(GROUP_CUSTOMER, DATE)
		self.assertFalse(payload["eligible"])
		self.assertFalse(payload["program_active"])

	def test_program_payload_carries_eligibility_rules(self):
		self._configure()
		rules = get_friends_and_family_program(DATE)["eligibility"]
		self.assertEqual(rules["basis"], "Customer Group or Flag")
		self.assertIn(FF_GROUP, rules["customer_groups"])
		self.assertIn(FF_PARENT_GROUP, rules["customer_groups"])
		self.assertIn(FF_CHILD_GROUP, rules["customer_groups"])
		self.assertEqual(rules["flag_field"], FLAG_FIELD)

	# ---- flag guard ------------------------------------------------------

	def test_cashier_cannot_set_flag(self):
		frappe.set_user(CASHIER_USER)
		customer = frappe.get_doc("Customer", PLAIN_CUSTOMER)
		customer.set(FLAG_FIELD, 1)
		with self.assertRaises(frappe.PermissionError):
			customer.save(ignore_permissions=True)

	def test_manager_can_set_flag(self):
		frappe.set_user(MANAGER_USER)
		customer = frappe.get_doc("Customer", PLAIN_CUSTOMER)
		customer.set(FLAG_FIELD, 1)
		customer.save(ignore_permissions=True)
		frappe.set_user("Administrator")
		frappe.db.set_value("Customer", PLAIN_CUSTOMER, FLAG_FIELD, 0)

	# ---- approved email domains (mandatory) --------------------------------

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
		# A disabled program may be saved without domains.
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
		# A sub-domain counts only when listed as its own row.
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
			result = self._check(GROUP_CUSTOMER)
			self.assertFalse(result.eligible)
			self.assertEqual(result.reason, REASON_NO_APPROVED_DOMAINS)
			self.assertTrue(result.ff_member)
			self.assertIn("no approved email domains", result.message)
			self.assertIsNone(get_program_for_customer(GROUP_CUSTOMER, DATE))
			self.assertEqual(self._check(PLAIN_CUSTOMER).reason, REASON_NOT_ELIGIBLE)

	def test_member_needs_verified_email_on_approved_domain(self):
		if not eligibility_module.has_verification_fields():
			self.skipTest("POSNext email verification fields are not installed")
		self._configure(approved_domains=[{"email_domain": "miraaya.com"}])

		self._set_email(GROUP_CUSTOMER, "Staff@Miraaya.com", verified="staff@miraaya.com")
		result = self._check(GROUP_CUSTOMER)
		self.assertTrue(result.eligible)
		self.assertEqual(result.reason, REASON_CUSTOMER_GROUP)
		self.assertEqual(result.email_domain, "miraaya.com")
		self.assertTrue(result.ff_member)

		# Unapproved / missing / sub-domain email → domain reason (even if verified).
		for email in ("staff@gmail.com", "", "x@sub.miraaya.com"):
			self._set_email(GROUP_CUSTOMER, email, verified=email or None)
			result = self._check(GROUP_CUSTOMER)
			self.assertEqual(result.reason, REASON_EMAIL_DOMAIN_NOT_APPROVED, email)
			self.assertIn("approved company domain", result.message)

		# Approved but never verified → verify reason.
		self._set_email(FLAG_CUSTOMER, "flag@miraaya.com", verified=None)
		result = self._check(FLAG_CUSTOMER)
		self.assertEqual(result.reason, REASON_EMAIL_NOT_VERIFIED)
		self.assertIn("verify the email", result.message)

		# Email changed after verification → not verified.
		self._set_email(FLAG_CUSTOMER, "other@miraaya.com", verified="flag@miraaya.com")
		self.assertEqual(self._check(FLAG_CUSTOMER).reason, REASON_EMAIL_NOT_VERIFIED)

		# Always required: there is no switch to turn verification off.
		self.assertFalse(hasattr(eligibility_module, "email_verification_required"))

	def test_non_members_are_never_asked_for_email(self):
		self._configure(approved_domains=[{"email_domain": "miraaya.com"}])
		self._set_email(PLAIN_CUSTOMER, "x@gmail.com")
		result = self._check(PLAIN_CUSTOMER)
		self.assertEqual(result.reason, REASON_NOT_ELIGIBLE)
		self.assertFalse(result.ff_member)
		self.assertFalse(eligibility_module.is_customer_ff_member(PLAIN_CUSTOMER))
		self._set_email(GROUP_CUSTOMER, "x@gmail.com")
		self.assertTrue(eligibility_module.is_customer_ff_member(GROUP_CUSTOMER))

	def test_email_gate_in_api_payload(self):
		self._configure(approved_domains=[{"email_domain": "miraaya.com"}])
		self._set_email(GROUP_CUSTOMER, "staff@gmail.com")
		payload = check_friends_and_family_eligibility(GROUP_CUSTOMER, DATE)
		self.assertFalse(payload["eligible"])
		self.assertEqual(payload["reason"], REASON_EMAIL_DOMAIN_NOT_APPROVED)
		self.assertTrue(payload["ff_member"])
		self.assertTrue(payload["message"])
		self.assertEqual(payload["discount_percentage"], 0)
