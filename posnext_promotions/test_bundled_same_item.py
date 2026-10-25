# Copyright (c) 2026, POS Next and contributors
# For license information, please see license.txt
"""Same-item "buy N get M free" bundled onto the paid line never carves the gift out of min_qty."""

import unittest
from unittest.mock import patch

import frappe

from posnext_promotions.api import apply_offers


def _rule(min_qty=2, max_qty=0):
	return frappe._dict(
		name="PR-B2G1", price_or_product_discount="Product", same_item=1, min_qty=min_qty, max_qty=max_qty
	)


def _price(scanned_qty):
	"""Run the bundled path for a cart of `scanned_qty` units at 15 with ERPNext granting 1 free."""
	line = frappe._dict(item_code="CHIPSY", qty=scanned_qty, price_list_rate=15, rate=15, pricing_rules="PR-B2G1")
	free_items_map = {("CHIPSY", "PR-B2G1"): frappe._dict(item_code="CHIPSY", qty=1)}
	with (
		patch.object(apply_offers.frappe, "get_cached_doc", return_value=_rule()),
		patch.object(apply_offers, "rule_promotion_type", return_value=None),
	):
		apply_offers._apply_bundled_same_item_free_discounts([line], free_items_map, {"PR-B2G1": _rule()})
	return line


class TestBundledSameItemFree(unittest.TestCase):
	def test_min_qty_scanned_pays_full_price(self):
		line = _price(2)
		self.assertFalse(line.get("discount_amount"))
		self.assertFalse(line.get("free_qty"))

	def test_min_plus_free_scanned_gets_one_free(self):
		line = _price(3)
		self.assertEqual(line.discount_amount, 15)
		self.assertEqual(line.free_qty, 1)
		self.assertEqual(line.rate, 10)
