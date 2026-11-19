# Copyright (c) 2026, BrainWise and contributors
"""D-17: standalone Product-discount Pricing Rules reach the POS cart.

The cart only asks ``apply_offers`` for offers ``get_offers`` listed, and the
offline cart prices only from that list. A standalone Product rule (BXGY, free
item, cart-total gift, GWP) must be listed the way a Promotional Scheme's
product rule is: ``offer == "Give Product"`` with the free-item fields.

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute posnext_promotions.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_standalone_product_offers'}"
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, cint, flt, nowdate

from posnext_promotions.api.gwp import calculate_gwp_discount_percentage
from posnext_promotions.api.offers import apply_offers, get_offers
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	ITEM_C,
	_cart_payload,
	_line,
	_make_rule,
	_resolve_company,
)

PREFIX = "_PNXT_F1_"
PRODUCT = {"price_or_product_discount": "Product", "rate_or_discount": "Discount Percentage"}


def _shared_ctx():
	"""``_ctx()``, without re-saving the shared ``_PNXT_TEST_`` POS Profile when it exists.

	Other suites on the dev site hold that row locked for the length of a test, and
	``_ensure_pos_profile`` re-saves it on every call (a lock wait timeout here).
	"""
	from unittest.mock import patch

	import posnext_promotions.test_promotions as tp

	original = tp._ensure_pos_profile

	def reuse(company, warehouse, price_list, mode_of_payment):
		name = f"_PNXT_TEST_POS_PROFILE_{company}"
		if frappe.db.exists("POS Profile", name):
			return name
		return original(company, warehouse, price_list, mode_of_payment)

	with patch.object(tp, "_ensure_pos_profile", reuse):
		return tp._ctx()

# Keys a product offer carries for the cart; scheme and standalone must agree on them.
SHAPE_KEYS = (
	"offer",
	"apply_on",
	"auto",
	"coupon_based",
	"min_qty",
	"max_qty",
	"min_amt",
	"max_amt",
	"discount_type",
	"rate",
	"discount_amount",
	"discount_percentage",
	"eligible_items",
	"eligible_item_groups",
	"eligible_brands",
	"free_item",
	"free_qty",
	"same_item",
	"is_recursive",
	# recurse_for left out: ERPNext's Pricing Rule.validate_recursion stores 1 on a
	# standalone Product rule while a scheme slab keeps 0; the cart reads it only
	# when is_recursive is 1 (test_recursive_bxgy_is_listed checks that case).
	"apply_recursion_over",
	"promotion_type",
	"free_item_uom",
	"gwp_paid_qty_basis",
	"apply_discount_on_price",
	"min_or_max_discount_qty_limit",
)


class TestStandaloneProductOffers(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ctx = _shared_ctx()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def _rule(self, suffix, **fields):
		return _make_rule(f"{PREFIX}{suffix}", **{**PRODUCT, **fields})

	def _offers(self):
		return {offer["name"]: offer for offer in get_offers(self.ctx.pos_profile)}

	def _listed(self, rule):
		offer = self._offers().get(rule)
		self.assertIsNotNone(offer, f"{rule} missing from get_offers")
		self.assertEqual(offer["offer"], "Give Product")
		self.assertEqual(offer["source"], "Pricing Rule")
		self.assertEqual(cint(offer["auto"]), 1)
		return offer

	def test_bxgy_same_item_is_listed(self):
		rule = self._rule(
			"BXGY", apply_on="Item Code", items=[{"item_code": ITEM_A}], same_item=1, min_qty=2, free_qty=1
		)
		offer = self._listed(rule)
		self.assertEqual(cint(offer["same_item"]), 1)
		self.assertEqual(flt(offer["free_qty"]), 1)
		self.assertEqual(flt(offer["min_qty"]), 2)
		self.assertEqual(offer["eligible_items"], [ITEM_A])

	def test_recursive_bxgy_is_listed(self):
		rule = self._rule(
			"BXGYRec",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			same_item=1,
			min_qty=2,
			free_qty=1,
			is_recursive=1,
			recurse_for=2,
		)
		offer = self._listed(rule)
		self.assertEqual(cint(offer["is_recursive"]), 1)
		self.assertEqual(flt(offer["recurse_for"]), 2)

	def test_free_other_item_is_listed(self):
		rule = self._rule(
			"FREE", apply_on="Item Code", items=[{"item_code": ITEM_B}], free_item=ITEM_C, free_qty=1, min_qty=1
		)
		offer = self._listed(rule)
		self.assertEqual(offer["free_item"], ITEM_C)
		self.assertEqual(cint(offer["same_item"]), 0)
		self.assertEqual(flt(offer["free_qty"]), 1)

	def test_cart_total_gift_is_listed(self):
		rule = self._rule("CartGift", apply_on="Transaction", min_qty=0, min_amt=500, free_item=ITEM_C, free_qty=1)
		offer = self._listed(rule)
		self.assertEqual(offer["apply_on"], "Transaction")
		self.assertEqual(flt(offer["min_amt"]), 500)
		self.assertEqual(offer["free_item"], ITEM_C)

	def test_gwp_is_listed_with_its_discount(self):
		rule = self._rule(
			"GWP",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			same_item=1,
			min_qty=2,
			free_qty=1,
			promotion_type="GWP",
		)
		offer = self._listed(rule)
		self.assertEqual(offer["promotion_type"], "GWP")
		# Same derivation as a scheme GWP slab.
		self.assertAlmostEqual(
			flt(offer["discount_percentage"]), calculate_gwp_discount_percentage(1, 2), places=4
		)
		self.assertGreater(flt(offer["discount_percentage"]), 0)

	def test_price_rules_still_listed_as_item_price(self):
		rule = _make_rule(
			f"{PREFIX}Price",
			apply_on="Item Code",
			items=[{"item_code": ITEM_B}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=20,
		)
		offer = self._offers().get(rule)
		self.assertIsNotNone(offer)
		self.assertEqual(offer["offer"], "Item Price")
		self.assertEqual(flt(offer["discount_percentage"]), 20)
		self.assertFalse(offer["free_item"])
		self.assertEqual(flt(offer["free_qty"]), 0)

	def test_listed_offer_gives_the_free_row(self):
		"""What the online cart does: ask apply_offers for the listed offers its line meets."""
		rule = self._rule(
			"BXGYCart", apply_on="Item Code", items=[{"item_code": ITEM_A}], same_item=1, min_qty=2, free_qty=1
		)
		selected = [
			name for name, offer in self._offers().items() if offer["name"] == rule and ITEM_A in offer["eligible_items"]
		]
		self.assertEqual(selected, [rule])
		resp = apply_offers(
			invoice_data=json.dumps(_cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=2)])),
			selected_offers=json.dumps(selected),
		)
		free = [row for row in resp.get("free_items") or [] if cint(row.get("is_free_item"))]
		self.assertEqual(len(free), 1, resp.get("free_items"))
		self.assertEqual(free[0]["item_code"], ITEM_A)
		self.assertEqual(flt(free[0]["qty"]), 1)
		self.assertEqual(free[0]["pricing_rules"], rule)

	def test_same_shape_as_a_scheme_product_rule(self):
		scheme_name = f"{PREFIX}SchemeBXGY"
		if frappe.db.exists("Promotional Scheme", scheme_name):
			frappe.delete_doc("Promotional Scheme", scheme_name, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Promotional Scheme",
				"name": scheme_name,
				"company": _resolve_company(),
				"apply_on": "Item Code",
				"selling": 1,
				"buying": 0,
				"valid_from": nowdate(),
				"items": [{"item_code": ITEM_C}],
				"product_discount_slabs": [
					{"rule_description": "BXGY", "min_qty": 2, "free_qty": 1, "same_item": 1}
				],
			}
		).insert(ignore_permissions=True)
		scheme_rule = frappe.db.get_value("Pricing Rule", {"promotional_scheme": scheme_name}, "name")
		standalone = self._rule(
			"Shape", apply_on="Item Code", items=[{"item_code": ITEM_C}], same_item=1, min_qty=2, free_qty=1
		)

		offers = self._offers()
		scheme_offer, standalone_offer = offers.get(scheme_rule), offers.get(standalone)
		self.assertIsNotNone(scheme_offer, "scheme product rule missing from get_offers")
		self.assertIsNotNone(standalone_offer, "standalone product rule missing from get_offers")
		self.assertEqual(
			{key: standalone_offer[key] for key in SHAPE_KEYS}, {key: scheme_offer[key] for key in SHAPE_KEYS}
		)

	# ---- Ruling 135: a Product rule for another price list is not the profile's offer ----

	def _other_price_list(self):
		name = f"{PREFIX}Other PL"
		if not frappe.db.exists("Price List", name):
			frappe.get_doc(
				{"doctype": "Price List", "price_list_name": name, "selling": 1, "currency": self.ctx.currency}
			).insert(ignore_permissions=True)
		return name

	def test_product_rule_for_another_price_list_is_not_listed(self):
		rule = self._rule(
			"OtherPL",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			same_item=1,
			min_qty=2,
			free_qty=1,
			for_price_list=self._other_price_list(),
		)
		self.assertNotIn(rule, self._offers())

	def test_product_rule_for_the_profile_price_list_is_listed(self):
		rule = self._rule(
			"OwnPL",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			same_item=1,
			min_qty=2,
			free_qty=1,
			for_price_list=self.ctx.price_list,
			currency=frappe.db.get_value("Price List", self.ctx.price_list, "currency"),
		)
		self._listed(rule)

	def test_scheme_product_rule_for_another_price_list_is_not_listed(self):
		scheme_name = f"{PREFIX}SchemePL"
		if frappe.db.exists("Promotional Scheme", scheme_name):
			frappe.delete_doc("Promotional Scheme", scheme_name, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Promotional Scheme",
				"name": scheme_name,
				"company": _resolve_company(),
				"apply_on": "Item Code",
				"selling": 1,
				"buying": 0,
				"valid_from": nowdate(),
				"items": [{"item_code": ITEM_C}],
				"product_discount_slabs": [{"rule_description": "BXGY", "min_qty": 2, "free_qty": 1, "same_item": 1}],
			}
		).insert(ignore_permissions=True)
		scheme_rule = frappe.db.get_value("Pricing Rule", {"promotional_scheme": scheme_name}, "name")
		self.assertIn(scheme_rule, self._offers())  # no for_price_list: listed as before
		frappe.db.set_value("Pricing Rule", scheme_rule, "for_price_list", self._other_price_list())
		self.assertNotIn(scheme_rule, self._offers())

	# ---- M-1: the widened filter still drops disabled / expired / other-company rules ----

	def test_disabled_expired_and_other_company_product_rules_are_not_listed(self):
		base = {"apply_on": "Item Code", "items": [{"item_code": ITEM_A}], "same_item": 1, "min_qty": 2, "free_qty": 1}
		disabled = self._rule("Disabled", disable=1, **base)
		expired = self._rule(
			"Expired", valid_from=add_days(nowdate(), -10), valid_upto=add_days(nowdate(), -1), **base
		)
		offers = self._offers()
		self.assertNotIn(disabled, offers)
		self.assertNotIn(expired, offers)

		other = frappe.db.get_value("Company", {"name": ["!=", self.ctx.company]}, "name")
		if other:
			foreign = self._rule(
				"OtherCo",
				company=other,
				currency=frappe.get_cached_value("Company", other, "default_currency"),
				**base,
			)
			self.assertNotIn(foreign, self._offers())
