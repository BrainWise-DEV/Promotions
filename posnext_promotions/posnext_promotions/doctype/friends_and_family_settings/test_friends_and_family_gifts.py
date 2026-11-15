# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Allow Free Gift with F&F — classification and suppression (Miraaya clarifications)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.promotions import friends_and_family_gifts as gifts
from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows


def _rule(**values):
	return frappe._dict(values)


class TestFriendsAndFamilyGiftClassification(FrappeTestCase):
	def test_classify_gift_kinds(self):
		self.assertEqual(
			gifts.classify_gift_rule(_rule(promotion_type="GWP", price_or_product_discount="Product")),
			gifts.GIFT_KIND_GWP,
		)
		# B2G1 labelled as GWP is still BXGY (same_item), not configurable GWP.
		self.assertEqual(
			gifts.classify_gift_rule(
				_rule(
					promotion_type="GWP",
					price_or_product_discount="Product",
					same_item=1,
					is_recursive=1,
					min_qty=2,
					free_qty=1,
				)
			),
			gifts.GIFT_KIND_BXGY,
		)
		self.assertEqual(
			gifts.classify_gift_rule(
				_rule(promotion_type="Gift Pool", price_or_product_discount="Product", apply_on="Item Group")
			),
			gifts.GIFT_KIND_FREE_ITEM,
		)
		self.assertEqual(
			gifts.classify_gift_rule(
				_rule(
					price_or_product_discount="Product",
					same_item=1,
					apply_on="Item Code",
					min_qty=2,
					free_qty=1,
				)
			),
			gifts.GIFT_KIND_BXGY,
		)
		self.assertEqual(
			gifts.classify_gift_rule(
				_rule(
					price_or_product_discount="Product",
					same_item=0,
					apply_on="Transaction",
					free_item="GIFT",
				)
			),
			gifts.GIFT_KIND_CART_TOTAL,
		)
		self.assertEqual(
			gifts.classify_gift_rule(
				_rule(
					price_or_product_discount="Product",
					same_item=0,
					apply_on="Brand",
					free_item="GIFT",
				)
			),
			gifts.GIFT_KIND_BRAND,
		)
		self.assertEqual(
			gifts.classify_gift_rule(
				_rule(
					price_or_product_discount="Product",
					same_item=0,
					apply_on="Item Code",
					free_item="GIFT",
				)
			),
			gifts.GIFT_KIND_FREE_ITEM,
		)
		self.assertIsNone(
			gifts.classify_gift_rule(_rule(price_or_product_discount="Price", discount_percentage=10))
		)

	def test_bxgy_always_blocked_when_fnf_applied(self):
		blocked_off = gifts.blocked_gift_kinds(fnf_applied=True, allow_free_gift=False)
		blocked_on = gifts.blocked_gift_kinds(fnf_applied=True, allow_free_gift=True)
		self.assertIn(gifts.GIFT_KIND_BXGY, blocked_off)
		self.assertIn(gifts.GIFT_KIND_BXGY, blocked_on)
		self.assertTrue(gifts.CONFIGURABLE_GIFT_KINDS.issubset(blocked_off))
		self.assertFalse(gifts.CONFIGURABLE_GIFT_KINDS & blocked_on)
		self.assertEqual(gifts.blocked_gift_kinds(fnf_applied=False, allow_free_gift=False), frozenset())

	def test_suppress_removes_configurable_gifts_when_disallowed(self):
		rule_map = {
			"PR-FREE": _rule(price_or_product_discount="Product", same_item=0, apply_on="Item Code"),
			"PR-BXGY": _rule(price_or_product_discount="Product", same_item=1, apply_on="Item Code"),
			"PR-GWP": _rule(promotion_type="GWP", price_or_product_discount="Product"),
		}
		free_items_map = {
			("GIFT", "PR-FREE"): frappe._dict(item_code="GIFT", qty=1, pricing_rules="PR-FREE"),
			("SKU", "PR-BXGY"): frappe._dict(item_code="SKU", qty=1, pricing_rules="PR-BXGY"),
			("GWP", "PR-GWP"): frappe._dict(item_code="GWP", qty=1, pricing_rules="PR-GWP"),
		}
		applied = {"PR-FREE", "PR-BXGY", "PR-GWP"}
		suppressed = gifts.suppress_incompatible_gifts(
			fnf_applied=True,
			allow_free_gift=False,
			prepared_items=[],
			free_items_map=free_items_map,
			rule_map=rule_map,
			applied_rules=applied,
		)
		self.assertEqual(free_items_map, {})
		self.assertEqual(suppressed, {"PR-FREE", "PR-BXGY", "PR-GWP"})
		self.assertEqual(applied, set())

	def test_allow_keeps_configurable_gifts_but_still_blocks_bxgy(self):
		rule_map = {
			"PR-FREE": _rule(price_or_product_discount="Product", same_item=0, apply_on="Item Code"),
			"PR-BXGY": _rule(price_or_product_discount="Product", same_item=1, apply_on="Item Code"),
		}
		free_items_map = {
			("GIFT", "PR-FREE"): frappe._dict(item_code="GIFT", qty=1, pricing_rules="PR-FREE"),
			("SKU", "PR-BXGY"): frappe._dict(item_code="SKU", qty=1, pricing_rules="PR-BXGY"),
		}
		gifts.suppress_incompatible_gifts(
			fnf_applied=True,
			allow_free_gift=True,
			prepared_items=[],
			free_items_map=free_items_map,
			rule_map=rule_map,
			applied_rules=set(),
		)
		self.assertIn(("GIFT", "PR-FREE"), free_items_map)
		self.assertNotIn(("SKU", "PR-BXGY"), free_items_map)

	def test_suppress_clears_bundled_bxgy_stamp_on_paid_line(self):
		rule_map = {
			"PR-BXGY": _rule(price_or_product_discount="Product", same_item=1, apply_on="Item Code"),
		}
		line = frappe._dict(
			{
				"item_code": "SKU",
				"qty": 3,
				"price_list_rate": 100,
				"rate": 66.67,
				"discount_amount": 100,
				"discount_percentage": 0,
				"free_qty": 1,
				"gwp_free_qty": 0,
				"discount_source": "free_item",
				"pricing_rules": "PR-BXGY",
				"is_free_item": 0,
			}
		)
		gifts.suppress_incompatible_gifts(
			fnf_applied=True,
			allow_free_gift=True,
			prepared_items=[line],
			free_items_map={},
			rule_map=rule_map,
			applied_rules={"PR-BXGY"},
		)
		self.assertEqual(flt(line.free_qty), 0)
		self.assertEqual(flt(line.discount_amount), 0)
		self.assertEqual(flt(line.rate), 100)
		self.assertFalse(line.pricing_rules)


class TestFriendsAndFamilyGiftSettings(FrappeTestCase):
	def _configure(self, **values):
		doc = frappe.get_single("Friends and Family Settings")
		doc.update(
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
			}
		)
		doc.update(values)
		doc.save()
		return doc

	def test_settings_field_and_program_payload(self):
		self._configure(allow_free_gift_with_ff=1)
		from posnext_promotions.promotions.friends_and_family import get_active_program

		program = get_active_program()
		self.assertTrue(program.allow_free_gift_with_ff)
		self.assertTrue(program.to_client_payload()["allow_free_gift_with_ff"])
		self.assertTrue(gifts.allow_free_gift_with_ff())

		self._configure(allow_free_gift_with_ff=0)
		program = get_active_program()
		self.assertFalse(program.allow_free_gift_with_ff)
		self.assertFalse(gifts.allow_free_gift_with_ff())
