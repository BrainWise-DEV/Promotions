# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Task 6g — a gift is always its own free-item row, never a discount on a paid line.

* Gift with Purchase over several items (Max / Min Price basis, aggregated or per line)
  adds the gift on top of the paid units as an ``is_free_item`` row (the rule's free item
  rate, 0 when unset; the GWP rule in ``pricing_rules``); the paid lines keep their price
  and qty, and only paid lines count toward the minimum (Ruling 84). Same for a GWP rule
  on the whole transaction.
* No promotion strategy turns a paid line into a zero-rate line, and stacked price
  discounts stop at 100 %.
* Friends & Family below 100 %; ``FF_CONTRACT`` is 5.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt

import posnext_promotions
from posnext_promotions.api import apply_offers as ao
from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_paid_freeze import (
	PaidInvoiceFixture,
)
from posnext_promotions.promotions import engine
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	ITEM_C,
	ITEM_PRICES,
	_cart_payload,
	_line,
	_make_rule,
)

RULE_PREFIX = "_PNXT_FFP_GWP6G_"


class TestGwpFreeRow(PaidInvoiceFixture, FrappeTestCase):
	def setUp(self):
		super().setUp()
		# Paid lines must show the promotion alone: F&F would discount them too.
		self._settings(enabled=0)

	# ---- helpers ------------------------------------------------------------

	def _rule(self, suffix, **fields):
		return _make_rule(f"{RULE_PREFIX}{suffix}", **fields)

	def _mixed_gwp(self, basis="Max Price", **fields):
		values = {
			"apply_on": "Item Code",
			"mixed_conditions": 1,
			"items": [{"item_code": ITEM_A}, {"item_code": ITEM_B}],
			"price_or_product_discount": "Product",
			"rate_or_discount": "Discount Percentage",
			"same_item": 1,
			"min_qty": 4,
			"max_qty": 4,
			"free_qty": 2,
			"gwp_paid_qty_basis": basis,
			"promotion_type": "GWP",
		}
		values.update(fields)
		return self._rule(f"Mixed{basis.split()[0]}", **values)

	def _apply(self, lines, rules):
		return apply_offers(
			invoice_data=json.dumps(_cart_payload(self.ctx, lines)),
			selected_offers=json.dumps(list(rules)),
		)

	def _two_by_two(self):
		return [_line(self.ctx, ITEM_A, qty=2), _line(self.ctx, ITEM_B, qty=2)]

	def assert_full_price(self, line):
		self.assertEqual(flt(line.get("discount_amount")), 0, line.get("item_code"))
		self.assertEqual(flt(line.get("discount_percentage")), 0, line.get("item_code"))
		self.assertEqual(flt(line.get("rate")), flt(line.get("price_list_rate")), line.get("item_code"))
		self.assertEqual(flt(line.get("gwp_free_qty")), 0, line.get("item_code"))
		self.assertEqual(flt(line.get("free_qty")), 0, line.get("item_code"))

	def assert_gift_row(self, row, item_code, qty, rule, rate=0):
		self.assertEqual(row["item_code"], item_code)
		self.assertEqual(flt(row["qty"]), qty)
		self.assertEqual(cint(row["is_free_item"]), 1)
		self.assertEqual(flt(row["rate"]), rate)
		self.assertEqual(flt(row["price_list_rate"]), rate)
		self.assertEqual(row["pricing_rules"], rule)
		self.assertEqual(row.get("discount_source"), "gwp")
		# Not a same-SKU split: the gift comes on top of the paid units.
		self.assertFalse(cint(row.get("gwp_same_item_row")))

	# ---- mixed items: aggregated ------------------------------------------------

	def test_mixed_gwp_gift_is_free_row(self):
		for basis, gift_code in (("Max Price", ITEM_A), ("Min Price", ITEM_B)):
			with self.subTest(basis=basis):
				rule = self._mixed_gwp(basis)
				try:
					resp = self._apply(self._two_by_two(), [rule])
				finally:
					frappe.db.set_value("Pricing Rule", rule, "disable", 1)
				free = resp.get("free_items") or []
				self.assertEqual(len(free), 1, free)
				self.assert_gift_row(free[0], gift_code, 2, rule)
				self.assertIn(rule, resp.get("applied_pricing_rules") or [])

	def test_paid_line_keeps_price(self):
		rule = self._mixed_gwp()
		resp = self._apply(self._two_by_two(), [rule])
		for line in resp["items"]:
			self.assert_full_price(line)
			# The paid lines stay linked to the gift (gift policy finds its trigger).
			self.assertIn(rule, line.get("pricing_rules") or "")
		# On top: the scanned units are all paid (Ruling 84).
		self.assertEqual(
			[(line["item_code"], flt(line["qty"])) for line in resp["items"]], [(ITEM_A, 2), (ITEM_B, 2)]
		)

	def test_gift_row_honours_free_item_rate(self):
		rule = self._mixed_gwp(free_item_rate=5)
		resp = self._apply(self._two_by_two(), [rule])
		self.assert_gift_row(resp["free_items"][0], ITEM_A, 2, rule, rate=5)

	def test_transaction_gwp_gift_on_top(self):
		rule = self._rule(
			"Txn",
			apply_on="Transaction",
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=1,
			min_qty=2,
			free_qty=1,
			promotion_type="GWP",
		)
		lines = [frappe._dict(_line(self.ctx, ITEM_A, qty=2)), frappe._dict(_line(self.ctx, ITEM_B, qty=3))]
		free_items_map = {}
		rule_map = {rule: frappe._dict(name=rule, promotion_type="GWP", promotional_scheme=None)}
		ao._apply_gwp_line_discounts(lines, free_items_map, rule_map, set())
		for line in lines:
			self.assert_full_price(line)
		self.assertEqual([flt(line.qty) for line in lines], [2, 3])
		self.assertEqual(set(free_items_map), {(ITEM_A, rule), (ITEM_B, rule)})
		for code in (ITEM_A, ITEM_B):
			self.assert_gift_row(free_items_map[(code, rule)], code, 1, rule)

	def test_gift_rows_of_one_item_are_merged(self):
		rule = self._mixed_gwp(min_qty=5, max_qty=0, free_qty=3)
		lines = [
			_line(self.ctx, ITEM_A, qty=2),
			_line(self.ctx, ITEM_A, qty=1),
			_line(self.ctx, ITEM_B, qty=2),
		]
		resp = self._apply(lines, [rule])
		free = resp.get("free_items") or []
		self.assertEqual(len(free), 1, free)
		self.assert_gift_row(free[0], ITEM_A, 3, rule)

	def test_echoed_gift_row_does_not_count_or_double(self):
		rule = self._mixed_gwp()
		echo = dict(
			_line(self.ctx, ITEM_A, qty=2), rate=0, price_list_rate=0, is_free_item=1, pricing_rules=rule
		)
		resp = self._apply([*self._two_by_two(), echo], [rule])
		free = resp.get("free_items") or []
		self.assertEqual(len(free), 1, free)
		self.assert_gift_row(free[0], ITEM_A, 2, rule)

	# ---- mixed items: per line -----------------------------------------------

	def test_per_line_gwp_gift_is_free_row(self):
		rule = self._rule(
			"PerLine",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}, {"item_code": ITEM_B}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=1,
			min_qty=1,
			free_qty=1,
			promotion_type="GWP",
		)
		lines = [frappe._dict(_line(self.ctx, ITEM_A, qty=1)), frappe._dict(_line(self.ctx, ITEM_B, qty=3))]
		free_items_map = {}
		rule_map = {rule: frappe._dict(name=rule, promotion_type="GWP", promotional_scheme=None)}
		# Single-code and multi-code rules both aggregate today; the per-line branch is
		# reached by rules that do not (e.g. Transaction scope). Force it.
		with patch.object(ao, "should_aggregate_gwp_quantities", return_value=False):
			ao._apply_gwp_line_discounts(lines, free_items_map, rule_map, set())
		for line in lines:
			self.assert_full_price(line)
			self.assertIn(rule, line.get("pricing_rules") or "")
		self.assertEqual(set(free_items_map), {(ITEM_A, rule), (ITEM_B, rule)})
		for code in (ITEM_A, ITEM_B):
			self.assert_gift_row(free_items_map[(code, rule)], code, 1, rule)

	# ---- every strategy ------------------------------------------------------

	def test_no_promotion_returns_zero_rate_paid_line(self):
		"""Each strategy at its normal settings: no paid line reaches 100 % or a zero rate."""
		price = {"price_or_product_discount": "Price", "apply_on": "Item Code"}
		product = {"price_or_product_discount": "Product", "rate_or_discount": "Discount Percentage"}
		fixtures = [
			(
				"Pct",
				{
					**price,
					"items": [{"item_code": ITEM_A}],
					"rate_or_discount": "Discount Percentage",
					"discount_percentage": 15,
				},
				[(ITEM_A, 1)],
			),
			(
				"Amt",
				{
					**price,
					"items": [{"item_code": ITEM_A}],
					"rate_or_discount": "Discount Amount",
					"discount_amount": 10,
				},
				[(ITEM_A, 1)],
			),
			(
				"Rate",
				{**price, "items": [{"item_code": ITEM_A}], "rate_or_discount": "Rate", "rate": 30},
				[(ITEM_A, 1)],
			),
			(
				"ItemLevel",
				{
					**price,
					"items": [{"item_code": ITEM_A}],
					"rate_or_discount": "Discount Percentage",
					"discount_percentage": 10,
					"promotion_type": "Item Level Discount",
				},
				[(ITEM_A, 1)],
			),
			(
				"FreeSame",
				{
					**product,
					"apply_on": "Item Code",
					"items": [{"item_code": ITEM_A}],
					"same_item": 1,
					"min_qty": 2,
					"free_qty": 1,
				},
				[(ITEM_A, 2)],
			),
			(
				"FreeOther",
				{
					**product,
					"apply_on": "Item Code",
					"items": [{"item_code": ITEM_B}],
					"free_item": ITEM_C,
					"free_qty": 1,
				},
				[(ITEM_B, 1)],
			),
			(
				"GwpSame",
				{
					**product,
					"apply_on": "Item Code",
					"items": [{"item_code": ITEM_A}],
					"same_item": 1,
					"min_qty": 2,
					"free_qty": 1,
					"promotion_type": "GWP",
				},
				[(ITEM_A, 2)],
			),
			(
				"GwpMixedMax",
				{
					**product,
					"apply_on": "Item Code",
					"mixed_conditions": 1,
					"items": [{"item_code": ITEM_A}, {"item_code": ITEM_B}],
					"same_item": 1,
					"min_qty": 4,
					"free_qty": 2,
					"gwp_paid_qty_basis": "Max Price",
					"promotion_type": "GWP",
				},
				[(ITEM_A, 2), (ITEM_B, 2)],
			),
			(
				"GwpMixedMin",
				{
					**product,
					"apply_on": "Item Code",
					"mixed_conditions": 1,
					"items": [{"item_code": ITEM_A}, {"item_code": ITEM_B}],
					"same_item": 1,
					"min_qty": 4,
					"free_qty": 2,
					"gwp_paid_qty_basis": "Min Price",
					"promotion_type": "GWP",
				},
				[(ITEM_A, 2), (ITEM_B, 2)],
			),
		]
		for suffix, fields, cart in fixtures:
			with self.subTest(strategy=suffix):
				rule = self._rule(suffix, **fields)
				try:
					resp = self._apply([_line(self.ctx, code, qty=qty) for code, qty in cart], [rule])
				finally:
					# One rule at a time: two enabled rules on one item conflict.
					frappe.db.set_value("Pricing Rule", rule, "disable", 1)
				for line in resp["items"]:
					if cint(line.get("is_free_item")):
						continue
					qty = flt(line.get("qty"))
					net = flt(line.get("price_list_rate")) * qty - flt(line.get("discount_amount"))
					self.assertLess(flt(line.get("discount_percentage")), 100, (suffix, line))
					self.assertGreater(net, 0, (suffix, line))
					self.assertGreater(flt(line.get("rate")), 0, (suffix, line))

	# ---- stacking stops at 100 % ---------------------------------------------

	def test_stacked_price_discounts_stop_at_100(self):
		rules = [
			self._rule(
				f"Stack{n}",
				apply_on="Item Code",
				items=[{"item_code": ITEM_A}],
				price_or_product_discount="Price",
				rate_or_discount="Discount Percentage",
				discount_percentage=60,
				apply_multiple_pricing_rules=1,
				priority=str(n),
			)
			for n in (1, 2)
		]
		resp = self._apply([_line(self.ctx, ITEM_A, qty=2)], rules)
		line = resp["items"][0]
		self.assertLessEqual(flt(line.get("discount_percentage")), 100)
		self.assertLessEqual(flt(line.get("discount_amount")), ITEM_PRICES[ITEM_A] * 2)
		self.assertGreaterEqual(flt(line.get("rate")), 0)

	def test_amount_discount_never_exceeds_the_line(self):
		rule = self._rule(
			"AmtOver",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Amount",
			discount_amount=80,
		)
		resp = self._apply([_line(self.ctx, ITEM_A, qty=2)], [rule])
		line = resp["items"][0]
		self.assertLessEqual(flt(line.get("discount_amount")), ITEM_PRICES[ITEM_A] * 2)
		self.assertLessEqual(flt(line.get("discount_percentage")), 100)

	def test_accumulated_parts_stop_at_100(self):
		line = frappe._dict(item_code=ITEM_A, qty=1, price_list_rate=50, rate=50, discount_percentage=30)
		engine.begin_line_discounts([line])
		engine.add_line_part(line, "a", 50)
		engine.add_line_part(line, "b", 40)
		engine.finalize_line_discounts([line])
		self.assertEqual(flt(line.discount_percentage), 100)
		self.assertEqual(flt(line.discount_amount), 50)
		self.assertEqual(flt(line.rate), 0)

	# ---- Friends & Family / contract -----------------------------------------

	def test_ff_percentage_100_rejected(self):
		for pct in (100, 120):
			with self.subTest(pct=pct), self.assertRaises(frappe.ValidationError):
				self._settings(discount_percentage=pct)
		self.assertEqual(flt(self._settings(discount_percentage=99.5).discount_percentage), 99.5)

	def test_ff_at_100_already_saved_can_still_be_saved(self):
		# A site already at 100 can save other Settings changes; only a change to 100 is refused.
		self._settings(discount_percentage=50)
		frappe.db.set_single_value("Friends and Family Settings", "discount_percentage", 100)
		doc = frappe.get_single("Friends and Family Settings")
		doc.start_date = None
		doc.save()
		self.assertEqual(flt(doc.discount_percentage), 100)

	def test_contract_is_5(self):
		self.assertEqual(posnext_promotions.FF_CONTRACT, 5)
