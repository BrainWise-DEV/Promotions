# Copyright (c) 2026, POS Next and contributors

import json
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.test_promotions import ITEM_A, _cart_payload, _ctx, _line, _make_rule


class TestBundledSameItemFree(FrappeTestCase):
	def test_same_item_free_is_separate_row_on_top(self):
		"""Same-item Product rule: gift is returned as a free row, the paid
		line keeps its full price (no carve / rate drift)."""
		rule = _make_rule(
			"_PNXT_TEST_BundledFree",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=1,
			min_qty=4,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
		)
		payload = _cart_payload(_ctx(), [_line(_ctx(), ITEM_A, qty=4)])
		resp = apply_offers(
			invoice_data=json.dumps(payload),
			selected_offers=json.dumps([rule]),
		)
		free_rows = resp.get("free_items") or []
		self.assertEqual(len(free_rows), 1)
		self.assertEqual(free_rows[0]["item_code"], ITEM_A)
		self.assertEqual(flt(free_rows[0]["qty"]), 1)
		item = resp["items"][0]
		self.assertEqual(flt(item.get("qty")), 4)
		self.assertEqual(flt(item.get("free_qty") or 0), 0)
		self.assertEqual(flt(item.get("discount_amount") or 0), 0)
		self.assertNotEqual(item.get("discount_source"), "free_item")

	def test_same_item_free_odd_qty_keeps_full_rate(self):
		"""Regression: qty 3 used to become rate 66.667 / total 199.99."""
		rule = _make_rule(
			"_PNXT_TEST_BundledFreeOdd",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=1,
			min_qty=2,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
		)
		line = _line(_ctx(), ITEM_A, qty=3)
		payload = _cart_payload(_ctx(), [line])
		resp = apply_offers(
			invoice_data=json.dumps(payload),
			selected_offers=json.dumps([rule]),
		)
		item = resp["items"][0]
		self.assertAlmostEqual(flt(item.get("rate")), flt(line.get("price_list_rate") or line.get("rate")), places=6)
		self.assertEqual(flt(item.get("discount_amount") or 0), 0)
		self.assertEqual(len(resp.get("free_items") or []), 1)

	def test_same_item_free_not_applied_below_min_qty(self):
		rule = _make_rule(
			"_PNXT_TEST_BundledFreeLow",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=1,
			min_qty=4,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
		)
		payload = _cart_payload(_ctx(), [_line(_ctx(), ITEM_A, qty=3)])
		resp = apply_offers(
			invoice_data=json.dumps(payload),
			selected_offers=json.dumps([rule]),
		)
		item = resp["items"][0]
		self.assertEqual(flt(item.get("free_qty")), 0)
		self.assertEqual(resp.get("free_items"), [])


if __name__ == "__main__":
	unittest.main()
