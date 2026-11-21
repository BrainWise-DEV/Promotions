# Copyright (c) 2026, BrainWise and contributors
"""D-R3-01: an F&F customer buys a line under an Item Level Discount rule.

The cart blocks F&F on the line (Item Level always blocks, Task 8s) and sends the
line at the rule's rate. pos_next saves paid rows with a blank ``pricing_rules``
(its ERPNext re-price guard), so the invoice's F&F pass sees no tag: it must find
the Item Level rule itself, as it does for an untagged Buy X Get Y line, or it
re-prices the line to F&F and Checkout loops on "Invoice Total Changed".

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute posnext_promotions.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_item_level_fnf_invoice'}"
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.promotions import friends_and_family_gifts as gifts
from posnext_promotions.promotions.friends_and_family_apply import DISCOUNT_SOURCE_FRIENDS_AND_FAMILY as FNF
from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows, give_verified_email
from posnext_promotions.tests.test_offline_coupon_overshoot import _shared_ctx
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	ITEM_PRICES,
	_apply_offers_and_stamp,
	_cart_payload,
	_line,
	_make_rule,
	_pos_invoice_pipeline,
	_resolve_customer_group,
	_resolve_territory,
)

PREFIX = "_PNXT_H1_"
PLAIN_CUSTOMER = f"{PREFIX}PLAIN_CUSTOMER"
PCT = 15


def _cleanup():
	for name in frappe.get_all("Pricing Rule", filters={"title": ["like", f"{PREFIX}%"]}, pluck="name"):
		frappe.delete_doc("Pricing Rule", name, force=True, ignore_permissions=True)


class TestItemLevelFnFInvoice(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		submit_invoice, update_invoice = _pos_invoice_pipeline()
		if update_invoice is None:
			raise __import__("unittest").SkipTest("pos_next invoice APIs are not installed")
		cls.submit_invoice, cls.update_invoice = staticmethod(submit_invoice), staticmethod(update_invoice)
		cls.ctx = _shared_ctx()
		give_verified_email(cls.ctx.customer)
		if not frappe.db.exists("Customer", PLAIN_CUSTOMER):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": PLAIN_CUSTOMER,
					"customer_group": _resolve_customer_group(),
					"territory": _resolve_territory(),
					"customer_type": "Individual",
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		_cleanup()
		if frappe.db.exists("Customer", PLAIN_CUSTOMER) and not frappe.db.exists(
			"Sales Invoice", {"customer": PLAIN_CUSTOMER}
		):
			frappe.delete_doc("Customer", PLAIN_CUSTOMER, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("h1_item_level")
		_cleanup()
		self._settings()

	def _settings(self, **values):
		settings = frappe.get_single("Friends and Family Settings")
		settings.update(
			{
				"enabled": 1,
				"approved_domains": approved_domain_rows(),
				"discount_percentage": 30,
				"discount_scope": "Entire Cart",
				"start_date": None,
				"end_date": None,
				"allow_free_gift_with_ff": 0,
				"applicable_brands": [],
				"applicable_item_groups": [],
				"applicable_items": [],
				"excluded_brands": [],
				"excluded_items": [],
				"excluded_item_groups": [],
				"max_discount_per_order": 0,
				"max_discount_per_customer_month": 0,
				**values,
			}
		)
		settings.save()

	def tearDown(self):
		frappe.db.rollback(save_point="h1_item_level")

	def _rule(self, fresh: bool):
		fields = dict(
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=PCT,
		)
		if not frappe.get_meta("Pricing Rule").has_field("promotion_type"):
			self.skipTest("Pricing Rule.promotion_type custom field missing")
		if fresh:
			return _make_rule(f"{PREFIX}IL_FRESH", promotion_type=gifts.PROMOTION_TYPE_ITEM_LEVEL, **fields)
		# A Price rule the user switched to Item Level afterwards.
		rule = _make_rule(f"{PREFIX}IL_SWITCHED", **fields)
		doc = frappe.get_doc("Pricing Rule", rule)
		doc.promotion_type = gifts.PROMOTION_TYPE_ITEM_LEVEL
		doc.save(ignore_permissions=True)
		return rule

	def _cart(self, customer, rule):
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A)])
		payload["customer"] = customer
		_apply_offers_and_stamp(payload, [rule])
		return payload

	def _assert_rule_price(self, row, msg):
		expected = ITEM_PRICES[ITEM_A] * (100 - PCT) / 100
		self.assertAlmostEqual(flt(row.get("rate")), expected, places=2, msg=msg)
		self.assertNotEqual(row.get("posnext_discount_source"), FNF, msg)

	def _sell(self, customer, rule, msg):
		payload = self._cart(customer, rule)
		line = payload["items"][0]
		# The cart: the rule's price and tag, no F&F.
		self._assert_rule_price(line, f"{msg}: cart")
		self.assertIn(rule, line.get("pricing_rules") or "", f"{msg}: cart tag")

		draft = self.update_invoice(json.dumps(payload))
		paid = [r for r in draft["items"] if not r.get("is_free_item")]
		self.assertEqual(len(paid), 1, msg)
		# The server's checked draft agrees with the cart (no "Invoice Total Changed").
		self._assert_rule_price(paid[0], f"{msg}: draft")
		self.assertAlmostEqual(flt(draft["grand_total"]), flt(line["rate"]), places=2, msg=f"{msg}: total")

		draft["payments"] = [{"mode_of_payment": self.ctx.mode_of_payment, "amount": flt(draft["grand_total"])}]
		self.submit_invoice(
			invoice=json.dumps(draft, default=str),
			data=json.dumps({"change_amount": 0, "write_off_amount": 0}),
		)
		sale = frappe.get_doc("Sales Invoice", draft["name"])
		self.assertEqual(sale.docstatus, 1, msg)
		self._assert_rule_price(sale.items[0].as_dict(), f"{msg}: sold")
		# The server-confirmed tag is kept on the sold row (Ruling 137).
		self.assertEqual(sale.items[0].pricing_rules, rule, f"{msg}: sold tag")

	def test_fresh_item_level_rule_blocks_fnf_on_the_invoice(self):
		rule = self._rule(fresh=True)
		for allow in (0, 1):
			self._settings(allow_free_gift_with_ff=allow)
			self._sell(self.ctx.customer, rule, f"F&F customer, fresh rule, allow={allow}")
		# Control: the same line still sells to a plain customer.
		self._sell(PLAIN_CUSTOMER, rule, "plain customer, fresh rule")

	def test_price_rule_switched_to_item_level_blocks_fnf_on_the_invoice(self):
		rule = self._rule(fresh=False)
		self._sell(self.ctx.customer, rule, "F&F customer, switched rule")
		self._sell(PLAIN_CUSTOMER, rule, "plain customer, switched rule")

	def test_other_lines_still_get_fnf(self):
		rule = self._rule(fresh=True)
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)])
		_apply_offers_and_stamp(payload, [rule])
		draft = self.update_invoice(json.dumps(payload))
		rows = {r["item_code"]: r for r in draft["items"] if not r.get("is_free_item")}
		self._assert_rule_price(rows[ITEM_A], "Item Level line")
		self.assertEqual(rows[ITEM_B].get("posnext_discount_source"), FNF)
		self.assertAlmostEqual(flt(rows[ITEM_B]["rate"]), ITEM_PRICES[ITEM_B] * 0.7, places=2)

	def test_item_level_cap_gives_fnf_back_above_max_qty(self):
		# ERPNext gives the rule's discount only up to max_qty: within it the line is the
		# rule's; above it the line has no promotion and F&F applies, on cart and invoice.
		rule = self._rule(fresh=True)
		frappe.db.set_value("Pricing Rule", rule, "max_qty", 2)
		frappe.clear_document_cache("Pricing Rule", rule)
		for qty, fnf in ((2, False), (3, True)):
			msg = f"qty {qty}"
			payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=qty)])
			_apply_offers_and_stamp(payload, [rule])
			draft = self.update_invoice(json.dumps(payload))
			row = next(r for r in draft["items"] if not r.get("is_free_item"))
			self.assertAlmostEqual(flt(row["rate"]), flt(payload["items"][0]["rate"]), places=2, msg=f"{msg}: cart vs draft")
			if fnf:
				self.assertEqual(row.get("posnext_discount_source"), FNF, msg)
				self.assertAlmostEqual(flt(row["rate"]), ITEM_PRICES[ITEM_A] * 0.7, places=2, msg=msg)
			else:
				self._assert_rule_price(row, msg)
