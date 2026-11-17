# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Allow Free Gift with F&F — classification and suppression (Miraaya clarifications)."""

from __future__ import annotations

from unittest.mock import patch

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

	def test_scheme_blocking_kind(self):
		self.assertEqual(
			gifts.scheme_blocking_kind(_rule(price_or_product_discount="Product", same_item=1, apply_on="Item Code")),
			gifts.SCHEME_KIND_BXGY,
		)
		self.assertEqual(
			gifts.scheme_blocking_kind(
				_rule(promotion_type="Item Level Discount", price_or_product_discount="Price", same_item=0, apply_on="Item Code")
			),
			gifts.SCHEME_KIND_ITEM_LEVEL,
		)
		# Accumulative carries Item Level Discount but is its own mechanic.
		self.assertIsNone(
			gifts.scheme_blocking_kind(
				_rule(
					promotion_type="Item Level Discount",
					price_or_product_discount="Price",
					same_item=0,
					apply_on="Item Group",
					apply_discount_on_price="Accumulative",
				)
			)
		)
		# Auto discount and coupons keep the best-discount comparison.
		self.assertIsNone(
			gifts.scheme_blocking_kind(
				_rule(promotion_type="Auto Discount", price_or_product_discount="Price", same_item=0, apply_on="Item Code")
			)
		)
		self.assertIsNone(
			gifts.scheme_blocking_kind(
				_rule(
					promotion_type="Item Level Discount",
					price_or_product_discount="Price",
					same_item=0,
					apply_on="Item Code",
					coupon_code_based=1,
				)
			)
		)
		# Price "buy one" (100% off 1 unit via Min + qty limit) is BXGY, even if
		# the scheme is labelled Item Level Discount.
		self.assertTrue(
			gifts.is_price_bxgy_rule(
				_rule(
					promotion_type="Item Level Discount",
					price_or_product_discount="Price",
					apply_on="Item Code",
					apply_discount_on_price="Min",
					min_or_max_discount_qty_limit=1,
					rate_or_discount="Discount Percentage",
					discount_percentage=100,
				)
			)
		)
		self.assertEqual(
			gifts.scheme_blocking_kind(
				_rule(
					promotion_type="Item Level Discount",
					price_or_product_discount="Price",
					apply_on="Item Code",
					apply_discount_on_price="Min",
					min_or_max_discount_qty_limit=1,
					rate_or_discount="Discount Percentage",
					discount_percentage=100,
				)
			),
			gifts.SCHEME_KIND_BXGY,
		)
		# A flat / Min percentage that is not 100% off limited units stays Routine.
		self.assertFalse(
			gifts.is_price_bxgy_rule(
				_rule(
					promotion_type="Item Level Discount",
					price_or_product_discount="Price",
					apply_on="Item Code",
					apply_discount_on_price="Min",
					min_or_max_discount_qty_limit=1,
					rate_or_discount="Discount Percentage",
					discount_percentage=50,
				)
			)
		)

	def _rule_map(self):
		return {
			"PR-FREE": _rule(name="PR-FREE", price_or_product_discount="Product", same_item=0, apply_on="Item Code"),
			"PR-BXGY": _rule(name="PR-BXGY", price_or_product_discount="Product", same_item=1, apply_on="Item Code"),
			"PR-GWP": _rule(name="PR-GWP", promotion_type="GWP", price_or_product_discount="Product", same_item=0, apply_on="Item Code"),
			"PR-CART": _rule(name="PR-CART", price_or_product_discount="Product", same_item=0, apply_on="Transaction"),
		}

	def _free_map(self):
		return {
			("GIFT", "PR-FREE"): frappe._dict(item_code="GIFT", qty=1, pricing_rules="PR-FREE"),
			("SKU", "PR-BXGY"): frappe._dict(item_code="SKU", qty=1, pricing_rules="PR-BXGY"),
			("GWP", "PR-GWP"): frappe._dict(item_code="GWP", qty=1, pricing_rules="PR-GWP"),
			("CART", "PR-CART"): frappe._dict(item_code="CART", qty=1, pricing_rules="PR-CART"),
		}

	def test_off_drops_gifts_triggered_by_fnf_lines_but_never_bxgy(self):
		lines = [frappe._dict(item_code="TRIGGER", qty=1, pricing_rules="PR-FREE,PR-GWP", discount_source="friends_and_family")]
		free_map = self._free_map()
		applied = {"PR-FREE", "PR-BXGY", "PR-GWP", "PR-CART"}
		with patch.object(gifts, "_rule_scope_matches", return_value=False):
			suppressed = gifts.suppress_incompatible_gifts(
				fnf_indices={0},
				allow_free_gift=False,
				prepared_items=lines,
				free_items_map=free_map,
				rule_map=self._rule_map(),
				applied_rules=applied,
			)
		self.assertEqual(set(free_map), {("SKU", "PR-BXGY")})
		self.assertEqual(suppressed, {"PR-FREE", "PR-GWP", "PR-CART"})
		self.assertEqual(applied, {"PR-BXGY"})

	def test_off_keeps_item_gifts_whose_trigger_has_no_fnf(self):
		lines = [frappe._dict(item_code="TRIGGER", qty=1, pricing_rules="PR-FREE"), frappe._dict(item_code="OTHER", qty=1)]
		free_map = self._free_map()
		with patch.object(gifts, "_rule_scope_matches", return_value=False):
			gifts.suppress_incompatible_gifts(
				fnf_indices={1},
				allow_free_gift=False,
				prepared_items=lines,
				free_items_map=free_map,
				rule_map=self._rule_map(),
			)
		# Item gifts stay (trigger had no F&F); cart-total gift goes (F&F on the cart).
		self.assertIn(("GIFT", "PR-FREE"), free_map)
		self.assertIn(("SKU", "PR-BXGY"), free_map)
		self.assertNotIn(("CART", "PR-CART"), free_map)

	def test_on_or_no_fnf_keeps_everything(self):
		for fnf_indices, allow in (({0}, True), (set(), False)):
			free_map = self._free_map()
			lines = [frappe._dict(item_code="TRIGGER", qty=1, pricing_rules="PR-FREE", discount_source="friends_and_family")]
			gifts.suppress_incompatible_gifts(
				fnf_indices=fnf_indices,
				allow_free_gift=allow,
				prepared_items=lines,
				free_items_map=free_map,
				rule_map=self._rule_map(),
			)
			self.assertEqual(len(free_map), 4)

	def test_off_clears_gwp_stamp_on_fnf_line(self):
		line = frappe._dict(
			{
				"item_code": "SKU",
				"qty": 3,
				"price_list_rate": 100,
				"rate": 66.67,
				"discount_amount": 100,
				"discount_percentage": 0,
				"free_qty": 1,
				"gwp_free_qty": 1,
				"discount_source": "gwp",
				"pricing_rules": "PR-GWP",
				"is_free_item": 0,
			}
		)
		gifts.suppress_incompatible_gifts(
			fnf_indices={0},
			allow_free_gift=False,
			prepared_items=[line],
			free_items_map={},
			rule_map=self._rule_map(),
			applied_rules={"PR-GWP"},
		)
		self.assertEqual(flt(line.free_qty), 0)
		self.assertEqual(flt(line.discount_amount), 0)
		self.assertEqual(flt(line.rate), 100)
		self.assertFalse(line.pricing_rules)

	def test_mark_bxgy_lines_by_scope_and_min_qty(self):
		rule_map = {"PR-BXGY": _rule(name="PR-BXGY", price_or_product_discount="Product", same_item=1, apply_on="Item Code", min_qty=2)}
		lines = [frappe._dict(item_code="SKU", qty=q) for q in range(1, 7)]
		with patch.object(gifts, "_rule_scope_matches", return_value=True):
			flagged = gifts.mark_fnf_ineligible_lines(lines, rule_map)
		self.assertEqual(flagged, {1, 2, 3, 4, 5})
		self.assertEqual(lines[0].get(gifts.FIELD_FNF_BLOCKED_BY), "")
		self.assertEqual(lines[3].get(gifts.FIELD_FNF_BLOCKED_BY), "PR-BXGY")
		self.assertEqual(gifts.line_blocked_by_scheme(lines[3]), ["PR-BXGY"])

	def test_effective_percent_price_buy_one_and_product_free_row(self):
		# Price "buy one": 100% off 1 unit — qty 2 = 50%, qty 4 = 25%.
		line = frappe._dict(item_code="SKU", qty=2, price_list_rate=100, discount_percentage=50, discount_amount=50)
		_promo, _gross, pct = gifts.bxgy_promo_breakdown(line)
		self.assertAlmostEqual(pct, 50.0, places=4)
		line.qty = 4
		line.discount_percentage = 25
		line.discount_amount = 25  # per-unit fallback still yields 25%
		self.assertAlmostEqual(gifts.bxgy_effective_discount_percent(line), 25.0, places=4)
		# Product same-item: 2 paid + 1 free row → 33.333..., 4 paid + 1 free → 20%.
		paid = frappe._dict(item_code="SKU", qty=2, price_list_rate=100, discount_percentage=0, discount_amount=0)
		free = [frappe._dict(item_code="SKU", qty=1, price_list_rate=0)]
		self.assertAlmostEqual(gifts.bxgy_effective_discount_percent(paid, free), 100.0 / 3.0, places=4)
		paid.qty = 4
		self.assertAlmostEqual(gifts.bxgy_effective_discount_percent(paid, free), 20.0, places=4)

	def test_strip_weaker_bxgy_respects_checkbox_and_threshold(self):
		rule_map = {
			"PR-BXGY": _rule(
				name="PR-BXGY",
				price_or_product_discount="Price",
				apply_on="Item Code",
				apply_discount_on_price="Min",
				min_or_max_discount_qty_limit=1,
				rate_or_discount="Discount Percentage",
				discount_percentage=100,
			)
		}
		# qty 4 at 25% — weaker than 30%. Off: leave the line alone.
		weak = frappe._dict(
			item_code="SKU",
			qty=4,
			price_list_rate=100,
			discount_percentage=25,
			discount_amount=25,
			pricing_rules="PR-BXGY",
			fnf_blocked_by_scheme="PR-BXGY",
		)
		free_map = {}
		applied = {"PR-BXGY"}
		snaps = gifts.strip_weaker_bxgy_promos(
			prepared_items=[weak],
			free_items_map=free_map,
			rule_map=rule_map,
			applied_rules=applied,
			fnf_percentage=30,
			allow_free_gift=False,
		)
		self.assertEqual(snaps, [])
		self.assertEqual(weak.get("pricing_rules"), "PR-BXGY")
		self.assertEqual(applied, {"PR-BXGY"})

		# On + weaker promo: strip. On + 50% (qty 2): keep.
		strong = frappe._dict(
			item_code="SKU",
			qty=2,
			price_list_rate=100,
			discount_percentage=50,
			discount_amount=50,
			pricing_rules="PR-BXGY",
			fnf_blocked_by_scheme="PR-BXGY",
		)
		snaps = gifts.strip_weaker_bxgy_promos(
			prepared_items=[weak, strong],
			free_items_map=free_map,
			rule_map=rule_map,
			applied_rules=applied,
			fnf_percentage=30,
			allow_free_gift=True,
		)
		self.assertEqual([s["idx"] for s in snaps], [0])
		self.assertFalse(weak.get("pricing_rules"))
		self.assertEqual(weak.get(gifts.FIELD_FNF_BLOCKED_BY), "")
		self.assertEqual(flt(weak.get("discount_percentage")), 0)
		self.assertEqual(strong.get("pricing_rules"), "PR-BXGY")
		self.assertEqual(applied, {"PR-BXGY"})  # still used by the strong line

		# Restore when F&F did not stamp the stripped line.
		ff_result = {"items": [{"idx": 0, "applies": False, "discount_amount": 0}]}
		self.assertTrue(
			gifts.restore_bxgy_when_fnf_does_not_beat(snaps, [weak, strong], free_map, applied, ff_result)
		)
		self.assertEqual(weak.get("pricing_rules"), "PR-BXGY")
		self.assertAlmostEqual(flt(weak.get("discount_percentage")), 25.0, places=4)
		self.assertIn("PR-BXGY", applied)


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
