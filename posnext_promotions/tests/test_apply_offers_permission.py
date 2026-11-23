# Copyright (c) 2026, BrainWise and contributors
"""D-R3-06: a refused read in ERPNext's pricing context is not an "Apply Offers Error".

ERPNext's ``apply_pricing_rule`` first checks that the user may read the transaction
the pricing context names (``validate_pricing_context``). A POS cashier who may not
read Sales Orders, pricing a cart whose target is a Sales Order, is refused there.
That refusal is the answer: it must reach the caller as a PermissionError, and must
not be logged as an internal 'Apply Offers Error' and re-thrown as a generic one.

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute posnext_promotions.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_apply_offers_permission'}"
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.test_promotions import ITEM_A, _cart_payload, _line
from posnext_promotions.tests.test_offline_coupon_overshoot import _shared_ctx

USER = "_pnxt_h1_cashier@example.com"
CASHIER_ROLE = "POSNext Cashier"
LOG_TITLE = "Apply Offers Error"


def _cleanup():
	for name in frappe.get_all("Error Log", filters={"owner": USER}, pluck="name"):
		frappe.delete_doc("Error Log", name, force=True, ignore_permissions=True)
	if frappe.db.exists("User", USER):
		frappe.delete_doc("User", USER, force=True, ignore_permissions=True)
	# Another app on the dev site adds this row for every new user.
	if frappe.db.table_exists("User Verification Status"):
		frappe.db.delete("User Verification Status", {"user": USER})


class TestApplyOffersPermission(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Role", CASHIER_ROLE):
			raise __import__("unittest").SkipTest(f"{CASHIER_ROLE} role missing")
		cls.ctx = _shared_ctx()
		_cleanup()
		user = frappe.get_doc(
			{"doctype": "User", "email": USER, "first_name": "_PNXT_H1_", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		user.add_roles(CASHIER_ROLE)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		_cleanup()
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _logs(self):
		return frappe.db.count("Error Log", {"owner": USER, "method": LOG_TITLE})

	def _price(self, doctype):
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A)])
		payload["doctype"] = doctype
		return apply_offers(json.dumps(payload))

	def test_unreadable_target_doctype_is_a_permission_error_not_an_offers_error(self):
		if frappe.has_permission("Sales Order", user=USER):
			self.skipTest(f"{CASHIER_ROLE} may read Sales Orders on this site")
		self.assertTrue(frappe.has_permission("Sales Invoice", user=USER))
		frappe.set_user(USER)
		# Control: the cashier prices a Sales Invoice cart.
		self.assertTrue(self._price("Sales Invoice").get("items"))
		before = self._logs()
		with self.assertRaises(frappe.PermissionError):
			self._price("Sales Order")
		self.assertEqual(self._logs(), before, "a refused read was logged as an Apply Offers Error")
