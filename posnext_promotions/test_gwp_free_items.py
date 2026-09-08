# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import json
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.api.gwp import (
	PROMOTION_TYPE_GWP,
	group_gwp_free_items,
	get_scheme_gwp_free_items,
)
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	ITEM_C,
	_cart_payload,
	_ctx,
	_line,
	_make_rule,
	_resolve_company,
)


class TestGWPFreeItemHelpers(unittest.TestCase):
	def test_group_preserves_order_and_uniques(self):
		self.assertEqual(
			group_gwp_free_items(
				[
					{"item_code": "A"},
					{"item_code": "B"},
					{"item_code": "C"},
					{"item_code": "A"},
				]
			),
			["A", "B", "C"],
		)


class TestGWPFreeItemsScheme(FrappeTestCase):
	SCHEME_NAME = "_PNXT_TEST_GWP_FREE_ITEMS"

	def setUp(self):
		super().setUp()
		self.ctx = _ctx()
		self._delete_scheme()

	def tearDown(self):
		self._delete_scheme()
		super().tearDown()

	def _delete_scheme(self):
		if frappe.db.exists("Promotional Scheme", self.SCHEME_NAME):
			frappe.delete_doc(
				"Promotional Scheme",
				self.SCHEME_NAME,
				force=True,
				ignore_permissions=True,
			)
			frappe.db.commit()

	def _skip_if_uninstalled(self):
		if not frappe.db.exists("DocType", "POS GWP Free Item"):
			self.skipTest("POS GWP Free Item is not installed")
		if not frappe.db.has_column("Promotional Scheme", "promotion_type"):
			self.skipTest("promotion_type custom field is not installed")

	def _make_scheme(self, free_items, min_qty=1, free_qty=1):
		self._skip_if_uninstalled()
		scheme = frappe.get_doc(
			{
				"doctype": "Promotional Scheme",
				"name": self.SCHEME_NAME,
				"company": _resolve_company(),
				"apply_on": "Item Code",
				"selling": 1,
				"promotion_type": PROMOTION_TYPE_GWP,
				"min_qty": min_qty,
				"items": [{"item_code": ITEM_A}],
				"gwp_free_items": [{"item_code": code} for code in free_items],
				"product_discount_slabs": [
					{
						"rule_description": "GWP Free Items",
						"free_qty": free_qty,
					}
				],
			}
		)
		scheme.insert(ignore_permissions=True)
		frappe.db.commit()
		return scheme

	def _rule_name(self):
		return frappe.db.get_value(
			"Pricing Rule", {"promotional_scheme": self.SCHEME_NAME, "disable": 0}, "name"
		)

	def test_scheme_level_min_qty_projects_to_rule(self):
		scheme = self._make_scheme([ITEM_B], min_qty=2, free_qty=1)
		rule_name = self._rule_name()
		self.assertTrue(rule_name)
		self.assertAlmostEqual(
			flt(frappe.db.get_value("Pricing Rule", rule_name, "min_qty")),
			2,
			places=2,
		)
		slab = scheme.product_discount_slabs[0]
		self.assertAlmostEqual(flt(slab.min_qty), 2, places=2)

	def test_get_scheme_gwp_free_items_loads_ordered_codes(self):
		self._make_scheme([ITEM_B, ITEM_C])
		self.assertEqual(get_scheme_gwp_free_items(self.SCHEME_NAME), [ITEM_B, ITEM_C])

	def test_buy_qualifying_qty_gets_first_free_item(self):
		self._make_scheme([ITEM_B, ITEM_C], min_qty=1, free_qty=1)
		rule = self._rule_name()
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=2)])
		resp = apply_offers(
			invoice_data=json.dumps(payload),
			selected_offers=json.dumps([rule]),
		)
		free_items = resp.get("free_items") or []
		self.assertEqual(len(free_items), 1)
		self.assertEqual(free_items[0].get("item_code"), ITEM_B)
		self.assertEqual(flt(free_items[0].get("qty")), 1)

	def test_oos_first_free_item_falls_back_to_next(self):
		from unittest.mock import patch

		self._make_scheme([ITEM_B, ITEM_C], min_qty=1, free_qty=1)
		rule = self._rule_name()
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=2)])

		def stock(item):
			if item.get("item_code") == ITEM_B:
				return 0
			return 100

		with (
			patch("posnext_promotions.api.apply_offers._should_block", return_value=True),
			patch("posnext_promotions.api.apply_offers._item_is_stock_item", return_value=True),
			patch("posnext_promotions.api.apply_offers._get_available_stock", side_effect=stock),
		):
			resp = apply_offers(
				invoice_data=json.dumps(payload),
				selected_offers=json.dumps([rule]),
			)

		free_items = resp.get("free_items") or []
		self.assertEqual(len(free_items), 1)
		self.assertEqual(free_items[0].get("item_code"), ITEM_C)

	def test_all_free_items_oos_grants_nothing(self):
		from unittest.mock import patch

		self._make_scheme([ITEM_B, ITEM_C], min_qty=1, free_qty=1)
		rule = self._rule_name()
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=2)])

		with (
			patch("posnext_promotions.api.apply_offers._should_block", return_value=True),
			patch("posnext_promotions.api.apply_offers._item_is_stock_item", return_value=True),
			patch("posnext_promotions.api.apply_offers._get_available_stock", return_value=0),
		):
			resp = apply_offers(
				invoice_data=json.dumps(payload),
				selected_offers=json.dumps([rule]),
			)

		self.assertEqual(resp.get("free_items") or [], [])

	def test_multi_free_qty_spreads_in_order(self):
		self._make_scheme([ITEM_B, ITEM_C], min_qty=1, free_qty=2)
		rule = self._rule_name()
		# Same-SKU GWP eligibility needs min_qty + free_qty scans (1 + 2 = 3).
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=3)])
		resp = apply_offers(
			invoice_data=json.dumps(payload),
			selected_offers=json.dumps([rule]),
		)
		free_items = resp.get("free_items") or []
		by_code = {row.get("item_code"): flt(row.get("qty")) for row in free_items}
		self.assertEqual(by_code, {ITEM_B: 1, ITEM_C: 1})

	def test_empty_gwp_free_items_keeps_same_sku_behavior(self):
		"""Backward compat: standalone GWP rule without a free-item pool."""
		rule = _make_rule(
			"_PNXT_TEST_GWP_NoPoolCompat",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=1,
			min_qty=2,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
			promotion_type="GWP",
		)
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=3)])
		resp = apply_offers(
			invoice_data=json.dumps(payload),
			selected_offers=json.dumps([rule]),
		)
		free_items = resp.get("free_items") or []
		self.assertEqual(len(free_items), 1)
		self.assertEqual(free_items[0].get("item_code"), ITEM_A)
		self.assertEqual(flt(free_items[0].get("qty")), 1)


if __name__ == "__main__":
	unittest.main()
