# Copyright (c) 2026, BrainWise and contributors
"""D-18: an offline sale whose line coupon went over its limit before the sync.

POS Settings ``offline_coupon_overshoot``:
- "Allow and log" (default): the sync succeeds, the use is counted past the
  limit and the skipped check is written to Error Log.
- "Reject": the sync keeps refusing.

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute posnext_promotions.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_offline_coupon_overshoot'}"
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.test_promotions import (
	ITEM_C,
	ITEM_PRICES,
	_cart_payload,
	_line,
	_pos_invoice_pipeline,
)

CODE = "_PNXT_F1_MAX1"
OVERSHOOT_FIELD = "offline_coupon_overshoot"
OVERSHOOT_LOG_TITLE = "POS Coupon check skipped for an offline sale"


def _shared_ctx():
	"""``_ctx()``, without re-saving the shared ``_PNXT_TEST_`` POS Profile when it exists.

	Other suites on the dev site hold that row locked for the length of a test, and
	``_ensure_pos_profile`` re-saves it on every call (a lock wait timeout here).
	"""
	from unittest.mock import patch

	import posnext_promotions.test_promotions as tp

	original = tp._ensure_pos_profile

	def reuse(company, warehouse, price_list, mode_of_payment):
		name = f"_PNXT_TEST_POS_PROFILE_{company}"
		if frappe.db.exists("POS Profile", name):
			return name
		return original(company, warehouse, price_list, mode_of_payment)

	with patch.object(tp, "_ensure_pos_profile", reuse):
		return tp._ctx()


def _delete_tagged_error_logs():
	for name in frappe.get_all(
		"Error Log",
		# Underscores escaped: unescaped, LIKE's "_" matches any character.
		or_filters={"error": ["like", r"%\_PNXT\_F1\_%"], "method": ["like", r"%\_PNXT\_F1\_%"]},
		pluck="name",
	):
		frappe.delete_doc("Error Log", name, force=True, ignore_permissions=True)


class TestOfflineLineCouponOvershoot(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# pos_next's submit-time Error Logs can land after an earlier run's tearDownClass.
		_delete_tagged_error_logs()
		cls.ctx = _shared_ctx()
		existing = frappe.db.get_value("POS Coupon", {"coupon_code": CODE}, "name")
		if existing:
			frappe.delete_doc("POS Coupon", existing, force=True, ignore_permissions=True)
		cls.coupon = (
			frappe.get_doc(
				{
					"doctype": "POS Coupon",
					"coupon_name": CODE,
					"coupon_code": CODE,
					"coupon_type": "Promotional",
					"company": cls.ctx.company,
					"discount_type": "Percentage",
					"discount_percentage": 10,
					"apply_scope": "All Eligible Items",
					"maximum_use": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		cls.pos_settings = frappe.db.get_value("POS Settings", {"pos_profile": cls.ctx.pos_profile}, "name")
		if not cls.pos_settings:
			cls.pos_settings = (
				frappe.get_doc({"doctype": "POS Settings", "pos_profile": cls.ctx.pos_profile})
				.insert(ignore_permissions=True)
				.name
			)
		cls.original_policy = frappe.db.get_value("POS Settings", cls.pos_settings, OVERSHOOT_FIELD)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		frappe.db.set_value("POS Settings", cls.pos_settings, OVERSHOOT_FIELD, cls.original_policy)
		frappe.delete_doc("POS Coupon", cls.coupon, force=True, ignore_permissions=True)
		_delete_tagged_error_logs()
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		# The coupon's last use was taken by another till while this sale sat offline.
		frappe.db.set_value("POS Coupon", self.coupon, {"used": 1, "maximum_use": 1, "disabled": 0})

	def tearDown(self):
		frappe.db.rollback()

	def _set_policy(self, value):
		frappe.db.set_value("POS Settings", self.pos_settings, OVERSHOOT_FIELD, value)

	def _offline_payload(self, header_coupon=False):
		"""What the cart queued offline: the line discounted by the coupon and tagged with it.

		POSSale.vue's offline checkout builds its own payload with no header
		``coupon_code`` (the line tags come from formatItemsForSubmission), so by
		default the header is left out, as the UI run's queued sale had it.
		"""
		price = ITEM_PRICES[ITEM_C]
		line = _line(self.ctx, ITEM_C)
		line.update(
			{
				"rate": price * 0.9,
				"discount_percentage": 10,
				"discount_amount": price * 0.1,
				"posnext_coupon_code": CODE,
				"posnext_coupon_discount": price * 0.1,
			}
		)
		payload = _cart_payload(self.ctx, [line])
		if header_coupon:
			payload["coupon_code"] = CODE
		payload.update(
			{
				"customer": self.ctx.customer,
				"offline_id": f"_PNXT_F1_{frappe.generate_hash(length=10)}",
				"payments": [{"mode_of_payment": self.ctx.mode_of_payment, "amount": price * 0.9}],
			}
		)
		return payload

	def _sync(self, payload):
		submit_invoice, _update = _pos_invoice_pipeline()  # optional app: never imported directly
		if submit_invoice is None:
			self.skipTest("pos_next invoice APIs are not installed")
		result = submit_invoice(invoice=json.dumps(payload), data=json.dumps({}))
		return frappe.get_doc("Sales Invoice", result["name"])

	def test_allow_and_log_syncs_counts_and_logs(self):
		self._set_policy("Allow and log")
		sale = self._sync(self._offline_payload())

		self.assertEqual(sale.docstatus, 1)
		self.assertEqual((sale.coupon_code or "").upper(), CODE)
		self.assertEqual((sale.items[0].posnext_coupon_code or "").upper(), CODE)
		self.assertAlmostEqual(sale.items[0].discount_percentage, 10, places=2)
		# The restored header must not apply the coupon a second time.
		self.assertEqual(flt(sale.discount_amount), 0)
		self.assertAlmostEqual(flt(sale.net_total), ITEM_PRICES[ITEM_C] * 0.9, places=2)
		self.assertEqual(frappe.db.get_value("POS Coupon", self.coupon, "used"), 2)
		logs = frappe.get_all(
			"Error Log", filters={"method": OVERSHOOT_LOG_TITLE, "reference_name": sale.name}, pluck="error"
		)
		self.assertTrue(any("\nmaximum_use: " in (log or "") for log in logs), logs)

	def test_reject_keeps_refusing(self):
		self._set_policy("Reject")
		with self.assertRaises(frappe.ValidationError) as caught:
			self._sync(self._offline_payload())
		# Refused by the coupon policy, not by a lost header.
		self.assertIn("usage limit", str(caught.exception))
		self.assertEqual(frappe.db.get_value("POS Coupon", self.coupon, "used"), 1)

	def test_within_limit_offline_line_coupon_syncs(self):
		frappe.db.set_value("POS Coupon", self.coupon, "used", 0)
		self._set_policy("Allow and log")
		sale = self._sync(self._offline_payload())
		self.assertEqual(sale.docstatus, 1)
		self.assertEqual((sale.coupon_code or "").upper(), CODE)
		self.assertEqual(frappe.db.get_value("POS Coupon", self.coupon, "used"), 1)

	def test_header_present_still_syncs(self):
		self._set_policy("Allow and log")
		sale = self._sync(self._offline_payload(header_coupon=True))
		self.assertEqual(sale.docstatus, 1)
		self.assertEqual(frappe.db.get_value("POS Coupon", self.coupon, "used"), 2)

	def test_online_line_tag_without_header_still_refused(self):
		frappe.db.set_value("POS Coupon", self.coupon, "used", 0)
		payload = self._offline_payload()
		payload.pop("offline_id")
		with self.assertRaises(frappe.ValidationError) as caught:
			self._sync(payload)
		self.assertIn("is not the coupon applied", str(caught.exception))

	def test_offline_lines_with_two_codes_still_refused(self):
		frappe.db.set_value("POS Coupon", self.coupon, "used", 0)
		payload = self._offline_payload()
		second = dict(payload["items"][0], posnext_coupon_code=f"{CODE}_OTHER")
		payload["items"].append(second)
		with self.assertRaises(frappe.ValidationError):
			self._sync(payload)
