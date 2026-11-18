# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Task 8p-promo (Ruling 114a) — the cart never offers a one-time rule it cannot honour.

``apply_offers`` skips a one-time-per-customer rule (item or transaction) that the
customer already redeemed (a usage row, or a stamp on another submitted sale), and every
one-time rule on a Sales Order / Quotation cart. The walk-in customer keeps its meaning:
never offered one-time rules.
"""

from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.api.apply_offers import _redeemed_one_time_rules, apply_offers
from posnext_promotions.test_promotions import ITEM_A, ITEM_B, _cart_payload, _ctx, _line, _make_rule

PREFIX = "_PNXT_OTC_"
NAMED = "_PNXT_OTC_Customer"


class TestOneTimeCart(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ctx = _ctx()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("ot_cart")
		if not frappe.db.exists("Customer", NAMED):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": NAMED,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				}
			).insert(ignore_permissions=True)
		for name in frappe.get_all("Pricing Rule", filters={"title": ["like", f"{PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("Pricing Rule", name, force=True, ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback(save_point="ot_cart")

	def _item_rule(self):
		return _make_rule(
			f"{PREFIX}Item",
			apply_on="Item Code",
			items=[{"item_code": ITEM_B}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=10,
			one_time_per_customer=1,
		)

	def _txn_rule(self):
		return _make_rule(
			f"{PREFIX}Txn",
			apply_on="Transaction",
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=10,
			apply_discount_on="Grand Total",
			one_time_per_customer=1,
		)

	def _apply(self, rule, customer=NAMED, doctype=None, select=True):
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_B, qty=1), _line(self.ctx, ITEM_A, qty=1)])
		payload["customer"] = customer
		if doctype:
			payload["doctype"] = doctype
		return apply_offers(
			invoice_data=json.dumps(payload), selected_offers=json.dumps([rule] if select else [])
		)

	def _redeem(self, rule):
		frappe.get_doc(
			{"doctype": "One Time Customer Offer Usage", "customer": NAMED, "pricing_rule": rule}
		).insert(ignore_permissions=True)

	def _offered(self, resp, rule) -> bool:
		return rule in (resp.get("applied_pricing_rules") or [])

	def test_unredeemed_rules_are_offered(self):
		item, txn = self._item_rule(), self._txn_rule()
		self.assertTrue(self._offered(self._apply(item), item))
		resp = self._apply(txn)
		self.assertTrue(self._offered(resp, txn))
		self.assertTrue(flt(resp.get("additional_discount_percentage")) or flt(resp.get("discount_amount")))

	def test_redeemed_item_rule_not_offered(self):
		rule = self._item_rule()
		self._redeem(rule)
		for select in (True, False):
			resp = self._apply(rule, select=select)
			self.assertFalse(self._offered(resp, rule), f"select={select}")
			line = next(i for i in resp["items"] if i["item_code"] == ITEM_B)
			self.assertEqual(flt(line.get("discount_percentage")), 0)

	def test_redeemed_transaction_rule_not_offered(self):
		rule = self._txn_rule()
		self._redeem(rule)
		for select in (True, False):
			resp = self._apply(rule, select=select)
			self.assertFalse(self._offered(resp, rule), f"select={select}")
			self.assertEqual(flt(resp.get("additional_discount_percentage")), 0)
			self.assertEqual(flt(resp.get("discount_amount")), 0)

	def test_rule_stamped_on_another_submitted_invoice_not_offered(self):
		item, txn = self._item_rule(), self._txn_rule()
		stamp = json.dumps([item, txn])
		# A submitted, non-return sale of this customer carrying the stamp (no usage row).
		frappe.db.sql(
			"""insert into `tabSales Invoice` (name, customer, docstatus, is_return, pos_applied_one_time_rules,
			creation, modified, owner, modified_by) values (%s, %s, 1, 0, %s, now(), now(), 'Administrator', 'Administrator')""",
			(f"{PREFIX}SINV", NAMED, stamp),
		)
		for rule in (item, txn):
			self.assertFalse(self._offered(self._apply(rule), rule))

	def test_cancelled_or_return_stamp_does_not_block(self):
		item = self._item_rule()
		for i, (docstatus, is_return) in enumerate(((2, 0), (1, 1), (0, 0))):
			frappe.db.sql(
				"""insert into `tabSales Invoice` (name, customer, docstatus, is_return, pos_applied_one_time_rules,
				creation, modified, owner, modified_by) values (%s, %s, %s, %s, %s, now(), now(), 'Administrator', 'Administrator')""",
				(f"{PREFIX}SINV{i}", NAMED, docstatus, is_return, json.dumps([item])),
			)
		self.assertTrue(self._offered(self._apply(item), item))

	def test_sales_order_cart_gets_no_one_time_rules(self):
		item, txn = self._item_rule(), self._txn_rule()
		for doctype in ("Sales Order", "Quotation"):
			for rule in (item, txn):
				for select in (True, False):
					resp = self._apply(rule, doctype=doctype, select=select)
					self.assertFalse(self._offered(resp, rule), f"{doctype} select={select}")

	def test_walk_in_customer_unchanged(self):
		item, txn = self._item_rule(), self._txn_rule()
		# The profile's default customer never gets a one-time rule.
		for rule in (item, txn):
			self.assertFalse(self._offered(self._apply(rule, customer=self.ctx.customer), rule))
		# ... and a non one-time rule still applies to it.
		plain = _make_rule(
			f"{PREFIX}Plain",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=10,
		)
		self.assertTrue(self._offered(self._apply(plain, customer=self.ctx.customer), plain))

	def test_redeeming_prle_10_does_not_hide_prle_1(self):
		# Stamps are JSON text: a rule named PRLE-10 must not match PRLE-1.
		rule = self._item_rule()
		longer = f"{rule}0"
		frappe.db.sql(
			"""insert into `tabSales Invoice` (name, customer, docstatus, is_return, pos_applied_one_time_rules,
			creation, modified, owner, modified_by) values (%s, %s, 1, 0, %s, now(), now(), 'Administrator', 'Administrator')""",
			(f"{PREFIX}SINV_LONG", NAMED, json.dumps([longer])),
		)
		self.assertTrue(self._offered(self._apply(rule), rule))
		self.assertEqual(_redeemed_one_time_rules(NAMED), {longer})

	def test_other_customers_stamp_does_not_block(self):
		rule = self._item_rule()
		other = frappe.db.get_value("Customer", {"name": ["not in", [NAMED, self.ctx.customer]]}, "name")
		self.assertTrue(other, "needs a second customer")
		frappe.db.sql(
			"""insert into `tabSales Invoice` (name, customer, docstatus, is_return, pos_applied_one_time_rules,
			creation, modified, owner, modified_by) values (%s, %s, 1, 0, %s, now(), now(), 'Administrator', 'Administrator')""",
			(f"{PREFIX}SINV_OTHER", other, json.dumps([rule])),
		)
		frappe.get_doc(
			{"doctype": "One Time Customer Offer Usage", "customer": other, "pricing_rule": rule}
		).insert(ignore_permissions=True)
		self.assertTrue(self._offered(self._apply(rule), rule))
		self.assertFalse(self._offered(self._apply(rule, customer=other), rule))

	def test_any_non_invoice_doctype_gets_no_one_time_rules(self):
		rule = self._item_rule()
		self.assertFalse(self._offered(self._apply(rule, doctype="Delivery Note"), rule))
