# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Regression tests for F&F discount restoration, applied_rules tracking, and multi-slab lookup."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.api import apply_offers as ao
from posnext_promotions.api import offers as offers_api
from posnext_promotions.overrides.pricing_rule import enforce_cross_cart_pricing_config


class TestFnfResetAppliedRulesAndSlabs(FrappeTestCase):
	def test_recursive_product_adds_rule_to_applied_rules_per_line(self):
		rule_name = "PRLE-REC-PERLINE"
		full_rule = frappe._dict(
			name=rule_name,
			price_or_product_discount="Product",
			apply_on="Item Code",
			items=[frappe._dict(item_code="ITEM-1")],
			same_item=1,
			is_recursive=1,
			free_qty=1,
			recurse_for=2,
			apply_recursion_over=0,
			min_qty=2,
			max_qty=0,
			free_item="ITEM-1",
			free_item_uom="Nos",
			free_item_rate=0,
			promotional_scheme=None,
		)
		prepared = [frappe._dict(item_code="ITEM-1", qty=4, is_free_item=0, pricing_rules=[])]
		free_items_map = {}
		applied_rules = set()
		rule_map = {rule_name: frappe._dict(name=rule_name, promotional_scheme=None)}

		with (
			patch.object(frappe, "get_cached_doc", return_value=full_rule),
			patch.object(
				ao,
				"_make_free_item_doc",
				side_effect=lambda code, qty, r, *_: frappe._dict(item_code=code, qty=qty, pricing_rules=r),
			),
		):
			ao._recompute_recursive_product_free_items(prepared, free_items_map, rule_map, applied_rules)

		self.assertIn(rule_name, applied_rules)
		self.assertIn(("ITEM-1", rule_name), free_items_map)

	def test_recursive_product_adds_rule_to_applied_rules_aggregate(self):
		rule_name = "PRLE-REC-AGG"
		full_rule = frappe._dict(
			name=rule_name,
			price_or_product_discount="Product",
			apply_on="Brand",
			brands=[frappe._dict(brand="Cosrx")],
			same_item=0,
			is_recursive=1,
			free_qty=1,
			recurse_for=2,
			apply_recursion_over=0,
			min_qty=2,
			max_qty=0,
			free_item="GIFT-1",
			free_item_uom="Nos",
			free_item_rate=0,
			promotional_scheme=None,
		)
		prepared = [
			frappe._dict(item_code="ITEM-1", brand="Cosrx", qty=1, is_free_item=0, pricing_rules=[]),
			frappe._dict(item_code="ITEM-2", brand="Cosrx", qty=1, is_free_item=0, pricing_rules=[]),
		]
		free_items_map = {}
		applied_rules = set()
		rule_map = {rule_name: frappe._dict(name=rule_name, promotional_scheme=None)}

		with (
			patch.object(frappe, "get_cached_doc", return_value=full_rule),
			patch.object(
				ao,
				"_make_free_item_doc",
				side_effect=lambda code, qty, r, *_: frappe._dict(item_code=code, qty=qty, pricing_rules=r),
			),
		):
			ao._recompute_recursive_product_free_items(prepared, free_items_map, rule_map, applied_rules)

		self.assertIn(rule_name, applied_rules)
		self.assertIn(("GIFT-1", rule_name), free_items_map)

	def test_gwp_adds_rule_to_applied_rules_when_applied(self):
		rule_name = "PRLE-GWP-BRAND"
		full_rule = frappe._dict(
			name=rule_name,
			price_or_product_discount="Product",
			promotion_type="GWP",
			apply_on="Brand",
			brands=[frappe._dict(brand="Cosrx")],
			same_item=1,
			is_recursive=0,
			free_qty=1,
			min_qty=2,
			max_qty=0,
			free_item=None,
			free_item_uom="Nos",
			free_item_rate=0,
			promotional_scheme=None,
			gwp_paid_qty_basis="Max Price",
		)
		prepared = [
			frappe._dict(
				item_code="COSRX-1",
				brand="Cosrx",
				qty=2,
				price_list_rate=27000,
				rate=27000,
				is_free_item=0,
				pricing_rules=[],
			)
		]
		free_items_map = {}
		applied_rules = set()
		rule_map = {rule_name: frappe._dict(name=rule_name, promotion_type="GWP", promotional_scheme=None)}

		with (
			patch.object(frappe, "get_cached_doc", return_value=full_rule),
			patch.object(
				ao,
				"_make_free_item_doc",
				side_effect=lambda code, qty, r, *_, **__: frappe._dict(item_code=code, qty=qty, pricing_rules=r),
			),
		):
			ao._apply_gwp_line_discounts(prepared, free_items_map, rule_map, applied_rules)

		self.assertIn(rule_name, applied_rules)
		self.assertIn(("COSRX-1", rule_name), free_items_map)

	def test_brand_gwp_promotional_scheme_sets_mixed_conditions(self):
		doc = frappe._dict(
			doctype="Promotional Scheme",
			apply_on="Brand",
			promotion_type="GWP",
			mixed_conditions=0,
			price_discount_slabs=[],
			product_discount_slabs=[frappe._dict(min_qty=2, free_qty=1)],
		)
		enforce_cross_cart_pricing_config(doc)
		self.assertEqual(doc.mixed_conditions, 1)

	def test_apply_offers_clears_stale_promo_source_without_misclassifying_as_manual(self):
		profile = frappe._dict(
			name="_TEST_PROFILE",
			company="_Test Company",
			warehouse="_Test Warehouse",
			selling_price_list="Standard Selling",
			ignore_pricing_rule=0,
		)
		invoice_data = {
			"doctype": "Sales Invoice",
			"pos_profile": "_TEST_PROFILE",
			"company": "_Test Company",
			"currency": "IQD",
			"customer": "_Test Customer",
			"items": [
				{
					"item_code": "ITEM-PROMO",
					"qty": 1,
					"price_list_rate": 27000.0,
					"rate": 21600.0,
					"discount_percentage": 20.0,
					"discount_amount": 5400.0,
					"pricing_rules": [],
					"discount_source": "item_level_promotion",
					"is_already_discounted": 1,
					"is_accumulative_discount": 1,
				},
				{
					"item_code": "ITEM-MANUAL",
					"qty": 1,
					"price_list_rate": 27000.0,
					"rate": 24300.0,
					"discount_percentage": 10.0,
					"discount_amount": 2700.0,
					"pricing_rules": [],
					"discount_source": "manual_discount",
					"is_already_discounted": 1,
				},
			],
		}

		with (
			patch.object(frappe, "get_cached_doc", return_value=profile),
			patch.object(
				frappe,
				"get_cached_value",
				side_effect=lambda doctype, name, fieldname, as_dict=False: (
					frappe._dict(customer_group="All Customer Groups", territory="All Territories")
					if as_dict
					else "IQD"
				),
			),
			patch.object(frappe, "get_all", return_value=[]),
			patch.object(ao, "erpnext_apply_pricing_rule", return_value=[]),
			patch.object(ao, "apply_friends_and_family_to_items", return_value={"eligible": False, "items": []}),
		):
			resp = ao.apply_offers(invoice_data, selected_offers=[])

		promo_line = resp["items"][0]
		manual_line = resp["items"][1]

		self.assertEqual(promo_line["discount_percentage"], 0)
		self.assertEqual(promo_line["discount_amount"], 0)
		self.assertEqual(promo_line["discount_source"], "")
		self.assertEqual(promo_line["is_already_discounted"], 0)
		self.assertEqual(promo_line["is_accumulative_discount"], 0)
		self.assertEqual(promo_line["rate"], 27000.0)

		self.assertEqual(manual_line["discount_source"], "manual_discount")
		self.assertEqual(manual_line["discount_percentage"], 10.0)

	def test_slab_fetcher_matches_each_rule_to_its_own_slab_id(self):
		fake_slabs = [
			{
				"name": "SLAB-1",
				"parent": "SCHEME-MULTI",
				"min_qty": 2,
				"max_qty": 3,
				"min_amount": 0,
				"max_amount": 0,
				"rate_or_discount": "Discount Percentage",
				"rate": 0,
				"discount_amount": 0,
				"discount_percentage": 10,
				"apply_multiple_pricing_rules": 0,
			},
			{
				"name": "SLAB-2",
				"parent": "SCHEME-MULTI",
				"min_qty": 4,
				"max_qty": 0,
				"min_amount": 0,
				"max_amount": 0,
				"rate_or_discount": "Discount Percentage",
				"rate": 0,
				"discount_amount": 0,
				"discount_percentage": 20,
				"apply_multiple_pricing_rules": 0,
			},
		]
		fake_rules = [
			{
				"name": "PRLE-S1",
				"title": "Slab 1",
				"apply_on": "Item Code",
				"selling": 1,
				"promotional_scheme": "SCHEME-MULTI",
				"promotional_scheme_id": "SLAB-1",
				"coupon_code_based": 0,
				"one_time_per_customer": 0,
				"price_or_product_discount": "Price",
				"priority": 1,
				"valid_from": None,
				"valid_upto": None,
				"promotion_type": None,
			},
			{
				"name": "PRLE-S2",
				"title": "Slab 2",
				"apply_on": "Item Code",
				"selling": 1,
				"promotional_scheme": "SCHEME-MULTI",
				"promotional_scheme_id": "SLAB-2",
				"coupon_code_based": 0,
				"one_time_per_customer": 0,
				"price_or_product_discount": "Price",
				"priority": 2,
				"valid_from": None,
				"valid_upto": None,
				"promotion_type": None,
			},
		]

		with (
			patch.object(frappe.db, "has_column", return_value=False),
			patch.object(frappe.db, "sql", side_effect=[fake_rules, fake_slabs, []]),
			patch.object(
				offers_api.EligibilityFetcher,
				"fetch_all",
				return_value={"SCHEME-MULTI": offers_api.OfferEligibility(["ITEM-1"], [], [])},
			),
		):
			offers = offers_api._get_promotional_scheme_offers("Test Co", "2026-11-24")

		by_name = {o.name: o for o in offers}
		self.assertEqual(by_name["PRLE-S1"].min_qty, 2)
		self.assertEqual(by_name["PRLE-S1"].discount_percentage, 10)
		self.assertEqual(by_name["PRLE-S2"].min_qty, 4)
		self.assertEqual(by_name["PRLE-S2"].discount_percentage, 20)
