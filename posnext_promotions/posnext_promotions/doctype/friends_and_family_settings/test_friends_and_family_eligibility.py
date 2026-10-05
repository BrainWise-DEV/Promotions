# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.api.friends_and_family import (
	check_friends_and_family_eligibility,
	get_friends_and_family_program,
)
from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
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
		_ensure_user(MANAGER_USER, ["Sales Manager", "Sales User"])
		_ensure_user(CASHIER_USER, ["Sales User"])

	def tearDown(self):
		frappe.set_user("Administrator")

	def _configure(self, **values):
		doc = frappe.get_single("Friends and Family Settings")
		doc.update(
			{
				"enabled": 1,
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
