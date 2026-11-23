# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.promotions import ff_classifier, ff_constants
from posnext_promotions.promotions import friends_and_family_gifts as gifts

_ITEM_LEVEL = {"promotion_type": "Item Level Discount", "price_or_product_discount": "Price", "apply_on": "Item Code"}

KIND_TABLE = [
	({"price_or_product_discount": "Product", "same_item": 1}, "bxgy"),
	({"promotion_type": "GWP", "price_or_product_discount": "Product", "same_item": 0}, "gwp"),
	({"promotion_type": "Gift Pool"}, "free_item"),
	({"price_or_product_discount": "Product", "apply_on": "Transaction"}, "cart_total_gift"),
	({"price_or_product_discount": "Product", "apply_on": "Brand"}, "brand_gift"),
	({"price_or_product_discount": "Product", "apply_on": "Item Code", "same_item": 0}, "free_item"),
	(_ITEM_LEVEL, "item_level"),
	({"price_or_product_discount": "Price", "apply_on": "Item Code"}, "price"),
]

# Price "buy one": 100% off (or Rate 0) a limited qty of the Min/Max-priced units.
_BUY_ONE = {
	"price_or_product_discount": "Price",
	"rate_or_discount": "Discount Percentage",
	"discount_percentage": 100,
	"apply_discount_on_price": "Min",
	"min_or_max_discount_qty_limit": 1,
	"apply_on": "Item Code",
}

# Literal outputs of the pre-refactor classify_gift_rule / scheme_blocking_kind
# (captured before gifts.py was touched): (rule, classify_gift_rule, scheme_blocking_kind).
FROZEN = [
	({"price_or_product_discount": "Product", "same_item": 1}, "bxgy", "bxgy"),
	({"promotion_type": "GWP", "price_or_product_discount": "Product", "same_item": 0}, "gwp", None),
	({"promotion_type": "GWP", "price_or_product_discount": "Product", "same_item": 1}, "bxgy", "bxgy"),
	({"promotion_type": "Gift Pool"}, "free_item", None),
	({"price_or_product_discount": "Product", "apply_on": "Transaction"}, "cart_total_gift", None),
	({"price_or_product_discount": "Product", "apply_on": "Brand"}, "brand_gift", None),
	({"price_or_product_discount": "Product", "apply_on": "Item Code", "same_item": 0}, "free_item", None),
	({"price_or_product_discount": "Product", "apply_on": "Unknown", "same_item": 0}, "free_item", None),
	(_ITEM_LEVEL, None, "item_level"),
	({**_ITEM_LEVEL, "coupon_code_based": 1}, None, None),
	({**_ITEM_LEVEL, "apply_discount_on_price": "Accumulative"}, None, None),
	({**_ITEM_LEVEL, "apply_on": "Transaction"}, None, None),
	({"promotion_type": "Item Level Discount", "apply_on": "Item Code"}, None, "item_level"),
	({"price_or_product_discount": "Price", "apply_on": "Item Code"}, None, None),
	({"price_or_product_discount": "Product", "same_item": 1, "coupon_code_based": 1}, "bxgy", None),
	({"price_or_product_discount": "Product", "apply_on": "Transaction", "coupon_code_based": 1}, "cart_total_gift", None),
	({"promotion_type": "GWP", "price_or_product_discount": "Price"}, "gwp", None),
	({"apply_on": "Item Code"}, None, None),
	("NO-SUCH-PRICING-RULE-XYZ", None, None),
	({}, None, None),
]


class TestFFClassifier(FrappeTestCase):
	def test_kinds(self):
		for rule, kind in KIND_TABLE:
			self.assertEqual(ff_classifier.classify_promotion(rule), kind, rule)
			self.assertIn(kind, ff_constants.PROMOTION_KINDS)

	def test_missing_rule_is_unclassified(self):
		self.assertEqual(ff_classifier.classify_promotion("NO-SUCH-PRICING-RULE-XYZ"), "unclassified")
		self.assertEqual(ff_classifier.classify_promotion({}), "unclassified")
		self.assertEqual(ff_classifier.classify_promotion(None), "unclassified")

	def test_product_unknown_apply_on_stays_free_item(self):
		rule = {"price_or_product_discount": "Product", "apply_on": "Unknown", "same_item": 0}
		self.assertEqual(ff_classifier.classify_promotion(rule), "free_item")

	def test_is_gift_kind(self):
		for kind in ("free_item", "gwp", "cart_total_gift", "brand_gift"):
			self.assertTrue(ff_classifier.is_gift_kind(kind), kind)
		for kind in ("bxgy", "item_level", "price", "unclassified", ""):
			self.assertFalse(ff_classifier.is_gift_kind(kind), kind)

	def test_unclassified_rules(self):
		self.assertEqual(ff_classifier.unclassified_rules(["NO-SUCH-PRICING-RULE-XYZ"]), ["NO-SUCH-PRICING-RULE-XYZ"])
		self.assertEqual(ff_classifier.unclassified_rules([]), [])

	def test_wrappers_frozen(self):
		for rule, gift, scheme in FROZEN:
			self.assertEqual(gifts.classify_gift_rule(rule), gift, rule)
			self.assertEqual(gifts.scheme_blocking_kind(rule), scheme, rule)

	def test_price_buy_one_is_bxgy(self):
		for extra in ({}, {"promotion_type": "Item Level Discount"}, {"promotion_type": "GWP"}, {"promotion_type": "Auto Discount"}):
			rule = {**_BUY_ONE, **extra}
			self.assertTrue(ff_classifier.is_price_bxgy_rule(rule), extra)
			self.assertEqual(ff_classifier.classify_promotion(rule), "bxgy", extra)
		rate_zero = {**_BUY_ONE, "rate_or_discount": "Rate", "rate": 0, "apply_discount_on_price": "Max"}
		self.assertEqual(ff_classifier.classify_promotion(rate_zero), "bxgy")
		for extra in (
			{"discount_percentage": 50},
			{"min_or_max_discount_qty_limit": 0},
			{"apply_discount_on_price": ""},
			{"rate_or_discount": "Rate", "rate": 10},
			{"price_or_product_discount": "Product", "same_item": 0},
			{"apply_discount_on_price": "Accumulative"},
			{"rate_or_discount": "Discount Amount", "discount_amount": 50},
			{"apply_on": "Transaction"},
		):
			rule = {**_BUY_ONE, **extra}
			self.assertFalse(ff_classifier.is_price_bxgy_rule(rule), extra)
			self.assertNotEqual(ff_classifier.classify_promotion(rule), "bxgy", extra)
		# A coupon "buy one" is still Buy X Get Y, but coupons keep the best-discount comparison.
		coupon = {**_BUY_ONE, "coupon_code_based": 1}
		self.assertEqual(ff_classifier.classify_promotion(coupon), "bxgy")
		self.assertIsNone(gifts.scheme_blocking_kind(coupon))

	def test_partial_row_loads_missing_fields(self):
		"""A row with only same_item / apply_on / price_or_product_discount is completed from the rule."""
		if not frappe.get_meta("Pricing Rule").has_field("promotion_type"):
			self.skipTest("Pricing Rule.promotion_type custom field missing")
		from posnext_promotions.test_promotions import ITEM_B, _ctx, _make_rule

		_ctx()
		frappe.db.savepoint("ff_classifier_partial")
		try:
			name = _make_rule(
				"_PNXT_FFCL_GWP",
				apply_on="Item Code",
				items=[{"item_code": ITEM_B}],
				price_or_product_discount="Product",
				rate_or_discount="Discount Percentage",
				same_item=0,
				free_item=ITEM_B,
				free_qty=1,
				free_item_uom="Nos",
			)
			frappe.db.set_value("Pricing Rule", name, "promotion_type", "GWP")
			frappe.clear_document_cache("Pricing Rule", name)
			row = frappe._dict(name=name, same_item=0, apply_on="Item Code", price_or_product_discount="Product")
			self.assertEqual(ff_classifier.classify_promotion(row), "gwp")
		finally:
			frappe.db.rollback(save_point="ff_classifier_partial")
