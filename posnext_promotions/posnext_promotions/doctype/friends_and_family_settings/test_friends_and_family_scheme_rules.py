# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Miraaya F&F rules end to end through ``apply_offers`` (Malak 9 Nov / meeting 16 Nov).

* Buy X Get Y and Routine (Item Level Discount) lines get no F&F; other lines do.
  Regression: B1G1 used to switch to F&F as qty grew (qty 4 report).
* Allow Free Gift with F&F (default off): a free gift whose trigger line got F&F
  is dropped when off and kept when on.
"""

from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.promotions import friends_and_family_gifts as gifts
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY as FNF,
	REASON_SCHEME_NOT_ELIGIBLE,
)
from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows, give_verified_email
from posnext_promotions.test_promotions import ITEM_A, ITEM_B, ITEM_C, _cart_payload, _ctx, _line, _make_rule


class TestFriendsAndFamilySchemeRules(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ctx = _ctx()
		give_verified_email(cls.ctx.customer)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("ff_scheme_rules")
		for name in frappe.get_all("Pricing Rule", filters={"title": ["like", "_PNXT_FF_%"]}, pluck="name"):
			frappe.delete_doc("Pricing Rule", name, force=True, ignore_permissions=True)
		self._settings()

	def tearDown(self):
		frappe.db.rollback(save_point="ff_scheme_rules")

	def _settings(self, **values):
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

	def _apply(self, lines, rules):
		payload = _cart_payload(self.ctx, [_line(self.ctx, code, qty=qty) for code, qty in lines])
		return apply_offers(invoice_data=json.dumps(payload), selected_offers=json.dumps(list(rules)))

	@staticmethod
	def _ff_line(resp, item_code):
		return next(
			line for line in resp["friends_and_family"]["items"] if line.get("item_code") == item_code
		)

	@staticmethod
	def _item(resp, item_code):
		return next(i for i in resp["items"] if i.get("item_code") == item_code and not i.get("is_free_item"))

	@staticmethod
	def _free_qty(resp, item_code):
		"""Same-item Product gifts are a free row on top of paid qty, not free_qty on the paid line."""
		return sum(flt(f.get("qty")) for f in (resp.get("free_items") or []) if f.get("item_code") == item_code)

	def _bxgy(self, title, **extra):
		fields = dict(
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
		fields.update(extra)
		return _make_rule(title, **fields)

	def _assert_bxgy_keeps_line(self, resp, qty, min_qty=2):
		line = self._item(resp, ITEM_A)
		ff = self._ff_line(resp, ITEM_A)
		if qty < min_qty:
			# Promotion not reached yet: the line is a plain line and gets F&F.
			self.assertEqual(line.get("discount_source"), FNF, f"qty {qty}")
			return
		self.assertNotEqual(line.get("discount_source"), FNF, f"qty {qty}: BXGY line switched to F&F")
		self.assertFalse(ff.get("applies"), f"qty {qty}")
		self.assertEqual(ff.get("reason"), REASON_SCHEME_NOT_ELIGIBLE, f"qty {qty}")

	def test_recursive_b1g1_never_switches_to_fnf_qty_1_to_6(self):
		# Buy 1 Get 1, recursive: every 2 paid units earn 1 free row (paid // recurse_for).
		rule = self._bxgy("_PNXT_FF_BXGY_REC", min_qty=2, is_recursive=1, recurse_for=2, apply_recursion_over=0)
		for qty in range(1, 7):
			resp = self._apply([(ITEM_A, qty), (ITEM_B, 1)], [rule])
			self._assert_bxgy_keeps_line(resp, qty, min_qty=2)
			self.assertEqual(self._free_qty(resp, ITEM_A), qty // 2, f"qty {qty}: BXGY free units")
			# Other cart lines still get F&F.
			self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF, f"qty {qty}")

	def test_capped_b1g1_never_switches_to_fnf_qty_1_to_6(self):
		rule = self._bxgy("_PNXT_FF_BXGY_CAP", max_qty=3)
		for qty in range(1, 7):
			resp = self._apply([(ITEM_A, qty), (ITEM_B, 1)], [rule])
			# Client report: qty 4 (above max_qty) used to flip the line to F&F.
			self._assert_bxgy_keeps_line(resp, qty)
			if 2 <= qty <= 3:
				self.assertEqual(self._free_qty(resp, ITEM_A), 1, f"qty {qty}")
			self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF, f"qty {qty}")

	def _routine(self, title, pct):
		rule = _make_rule(
			title,
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=pct,
		)
		if frappe.get_meta("Pricing Rule").has_field("promotion_type"):
			frappe.db.set_value("Pricing Rule", rule, "promotion_type", gifts.PROMOTION_TYPE_ITEM_LEVEL)
		else:
			self.skipTest("Pricing Rule.promotion_type custom field missing")
		frappe.clear_document_cache("Pricing Rule", rule)
		return rule

	def test_routine_item_level_line_keeps_its_discount(self):
		rule = self._routine("_PNXT_FF_ROUTINE", 10)
		resp = self._apply([(ITEM_A, 1), (ITEM_B, 1)], [rule])
		line = self._item(resp, ITEM_A)
		self.assertNotEqual(line.get("discount_source"), FNF)
		self.assertAlmostEqual(flt(line.get("discount_percentage")), 10, places=2)
		self.assertEqual(self._ff_line(resp, ITEM_A).get("reason"), REASON_SCHEME_NOT_ELIGIBLE)
		self.assertIn(rule, resp["applied_pricing_rules"])
		self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF)

	# ------------------------------------------------------------------
	# Routine never gets F&F, whatever "Allow Free Gift with F&F" says and
	# whatever its % is vs F&F (30% here). Buy X Get Y, Price "buy one"
	# included, follows the checkbox (Task 8s): off → no F&F, on → F&F allowed.
	# ------------------------------------------------------------------

	def _buy_one(self, title, promotion_type=None):
		"""Price Discount Buy X Get Y like dev-miraaya "buy one" (PRLE-0230):
		min qty 2, 100% off the cheapest unit, discounted-qty limit 1."""
		rule = _make_rule(
			title,
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=100,
			min_qty=2,
			apply_discount_on_price="Min",
			min_or_max_discount_qty_limit=1,
		)
		if promotion_type is not None and frappe.get_meta("Pricing Rule").has_field("promotion_type"):
			frappe.db.set_value("Pricing Rule", rule, "promotion_type", promotion_type)
			frappe.clear_document_cache("Pricing Rule", rule)
		return rule

	@staticmethod
	def _line_saving(line):
		qty = flt(line.get("qty"))
		gross = flt(line.get("price_list_rate")) * qty
		net = flt(line.get("amount")) if line.get("amount") not in (None, "") else flt(line.get("rate")) * qty
		return gross - net

	def test_buy_one_price_bxgy_classified_as_bxgy(self):
		rule = self._buy_one("_PNXT_FF_BUY_ONE_KIND")
		self.assertTrue(gifts.is_price_bxgy_rule(rule))
		self.assertEqual(gifts.scheme_blocking_kind(rule), gifts.SCHEME_KIND_BXGY)

	def test_buy_one_price_bxgy_follows_setting_qty_2_and_4(self):
		rule = self._buy_one("_PNXT_FF_BUY_ONE")
		for qty in (2, 4):
			# Off: whatever the % (qty 2 → promo 50% > F&F 30%; qty 4 → promo 25% < F&F 30%),
			# the line keeps "buy one" and gets no F&F.
			resp = self._apply([(ITEM_A, qty), (ITEM_B, 1)], [rule])
			msg = f"allow=0 qty={qty}"
			line = self._item(resp, ITEM_A)
			ff = self._ff_line(resp, ITEM_A)
			self.assertNotEqual(line.get("discount_source"), FNF, msg)
			self.assertFalse(ff.get("applies"), msg)
			self.assertEqual(ff.get("reason"), REASON_SCHEME_NOT_ELIGIBLE, msg)
			# One unit (50) free — the promotion itself is untouched.
			self.assertAlmostEqual(self._line_saving(line), 50.0, places=2, msg=msg)
			self.assertIn(rule, resp["applied_pricing_rules"], msg)
			# Other lines still get F&F.
			self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF, msg)

		# On: the line is not blocked and competes with F&F like any Price rule.
		self._settings(allow_free_gift_with_ff=1)
		self.assertIsNone(gifts.scheme_blocking_kind(rule))
		resp = self._apply([(ITEM_A, 2), (ITEM_B, 1)], [rule])
		line = self._item(resp, ITEM_A)
		# qty 2: one free unit (50%) beats F&F 30%.
		self.assertNotEqual(line.get("discount_source"), FNF, "allow=1 qty=2")
		self.assertNotEqual(self._ff_line(resp, ITEM_A).get("reason"), REASON_SCHEME_NOT_ELIGIBLE, "allow=1 qty=2")
		self.assertAlmostEqual(self._line_saving(line), 50.0, places=2, msg="allow=1 qty=2")
		self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF, "allow=1 qty=2")
		# qty 4: F&F 30% beats one free unit (25%).
		resp = self._apply([(ITEM_A, 4), (ITEM_B, 1)], [rule])
		line = self._item(resp, ITEM_A)
		self.assertEqual(line.get("discount_source"), FNF, "allow=1 qty=4")
		# F&F replaces "buy one", it does not stack on it: 50 at 30% off is 35.
		self.assertAlmostEqual(flt(line.get("discount_percentage")), 30, places=2, msg="allow=1 qty=4")
		self.assertAlmostEqual(flt(line.get("rate")), 35, places=2, msg="allow=1 qty=4")
		self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF, "allow=1 qty=4")

	def test_buy_one_labelled_item_level_or_auto_is_still_bxgy(self):
		for label in (gifts.PROMOTION_TYPE_ITEM_LEVEL, "Auto Discount", ""):
			rule = self._buy_one("_PNXT_FF_BUY_ONE_LBL", promotion_type=label)
			self.assertEqual(gifts.scheme_blocking_kind(rule), gifts.SCHEME_KIND_BXGY, label)
			resp = self._apply([(ITEM_A, 4), (ITEM_B, 1)], [rule])
			self.assertNotEqual(self._item(resp, ITEM_A).get("discount_source"), FNF, label)
			self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF, label)

	def test_product_bxgy_gets_fnf_and_keeps_free_rows_when_setting_on(self):
		self._settings(allow_free_gift_with_ff=1)
		rule = self._bxgy("_PNXT_FF_BXGY_ON", min_qty=2, is_recursive=1, recurse_for=2, apply_recursion_over=0)
		for qty in range(1, 7):
			resp = self._apply([(ITEM_A, qty), (ITEM_B, 1)], [rule])
			# Task 8s: the BXGY line gets F&F at every qty and keeps its free rows.
			self.assertEqual(self._item(resp, ITEM_A).get("discount_source"), FNF, f"qty {qty}")
			self.assertEqual(self._free_qty(resp, ITEM_A), qty // 2, f"qty {qty}: BXGY free units")
			self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF, f"qty {qty}")

	def test_routine_never_gets_fnf_setting_off_and_on(self):
		for allow in (0, 1):
			self._settings(allow_free_gift_with_ff=allow)
			# 10% < F&F 30%: best-discount would pick F&F, the rule must not.
			rule = self._routine("_PNXT_FF_ROUTINE_ALLOW", 10)
			resp = self._apply([(ITEM_A, 2), (ITEM_B, 1)], [rule])
			msg = f"allow={allow}"
			line = self._item(resp, ITEM_A)
			self.assertNotEqual(line.get("discount_source"), FNF, msg)
			self.assertAlmostEqual(flt(line.get("discount_percentage")), 10, places=2, msg=msg)
			self.assertEqual(self._ff_line(resp, ITEM_A).get("reason"), REASON_SCHEME_NOT_ELIGIBLE, msg)
			self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF, msg)

	def _free_gift_rule(self):
		return _make_rule(
			"_PNXT_FF_FREE_GIFT",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=0,
			free_item=ITEM_C,
			min_qty=1,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
		)

	def test_free_gift_dropped_when_setting_off(self):
		rule = self._free_gift_rule()
		resp = self._apply([(ITEM_A, 1)], [rule])
		self.assertEqual(self._item(resp, ITEM_A).get("discount_source"), FNF)
		self.assertFalse([f for f in resp.get("free_items") or [] if f.get("item_code") == ITEM_C])
		self.assertNotIn(rule, resp["applied_pricing_rules"])

	def test_free_gift_kept_when_setting_on(self):
		self._settings(allow_free_gift_with_ff=1)
		rule = self._free_gift_rule()
		resp = self._apply([(ITEM_A, 1)], [rule])
		self.assertEqual(self._item(resp, ITEM_A).get("discount_source"), FNF)
		self.assertTrue([f for f in resp.get("free_items") or [] if f.get("item_code") == ITEM_C])

	def test_free_gift_kept_when_trigger_has_no_fnf(self):
		# Trigger item excluded from F&F → the gift is not affected by the setting.
		self._settings(excluded_items=[{"item_code": ITEM_A}])
		rule = self._free_gift_rule()
		resp = self._apply([(ITEM_A, 1), (ITEM_B, 1)], [rule])
		self.assertNotEqual(self._item(resp, ITEM_A).get("discount_source"), FNF)
		self.assertEqual(self._item(resp, ITEM_B).get("discount_source"), FNF)
		self.assertTrue([f for f in resp.get("free_items") or [] if f.get("item_code") == ITEM_C])

	def test_default_is_off(self):
		meta = frappe.get_meta("Friends and Family Settings")
		field = meta.get_field("allow_free_gift_with_ff")
		self.assertEqual(field.fieldtype, "Check")
		self.assertEqual(str(field.default), "0")
