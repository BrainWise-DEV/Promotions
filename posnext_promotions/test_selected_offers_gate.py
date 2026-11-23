# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""A selected offer counts only if it wins ERPNext's own rule selection (G5, ledger 531).

The cart passes its applied offers as ``selected_offers``. ``apply_offers`` used to
load every selected name ERPNext did not return, unchecked, so the cart could show a
gift (or a discount) the server then refuses. A name ERPNext did not return now
counts only if ERPNext's ``get_pricing_rules`` picks it for a paid line on this cart,
with a GWP / Gift Pool judged on the cart's summed quantity, as pos_next's free-row
check judges it (Ruling 148). A mixed-SKU GWP stays granted (D-R2-08).
"""

from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_paid_freeze import (
	PaidInvoiceFixture,
)
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	_cart_payload,
	_line,
	_make_rule,
	_resolve_customer_group,
	_resolve_territory,
)

RULE_PREFIX = "_PNXT_G5_SEL_"
OTHER_CUSTOMER = "_PNXT_G5_SEL_OTHER"


class TestSelectedOffersGate(PaidInvoiceFixture, FrappeTestCase):
	def setUp(self):
		super().setUp()
		self._settings(enabled=0)
		if not frappe.db.exists("Customer", OTHER_CUSTOMER):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": OTHER_CUSTOMER,
					"customer_group": _resolve_customer_group(),
					"territory": _resolve_territory(),
				}
			).insert(ignore_permissions=True)

	def _rule(self, suffix, **fields):
		return _make_rule(f"{RULE_PREFIX}{suffix}", **fields)

	def _apply(self, lines, rules):
		return apply_offers(
			invoice_data=json.dumps(_cart_payload(self.ctx, lines)),
			selected_offers=json.dumps(list(rules)),
		)

	def _gwp(self, suffix, items, min_qty, **fields):
		return self._rule(
			suffix,
			apply_on="Item Code",
			items=[{"item_code": code} for code in items],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=1,
			min_qty=min_qty,
			free_qty=1,
			promotion_type="GWP",
			**fields,
		)

	def test_selected_offer_erpnext_does_not_pick_is_not_applied(self):
		rule = self._gwp("OtherCustomer", [ITEM_A], 1, applicable_for="Customer", customer=OTHER_CUSTOMER)
		resp = self._apply([_line(self.ctx, ITEM_A, qty=2)], [rule])
		self.assertEqual(resp.get("free_items") or [], [])
		self.assertNotIn(rule, resp.get("applied_pricing_rules") or [])
		# Nor a discount that is not valid yet.
		future = self._rule(
			"Future",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=20,
			valid_from=add_days(nowdate(), 5),
		)
		resp = self._apply([_line(self.ctx, ITEM_A, qty=1)], [future])
		self.assertEqual(flt(resp["items"][0].get("discount_percentage")), 0, resp["items"][0])
		self.assertNotIn(future, resp.get("applied_pricing_rules") or [])

	def _mixed(self, suffix, min_qty, **fields):
		"""UIT-PR-GWP's shape (D-R2-08): buy ITEM_A / ITEM_B, ``min_qty`` in all, get ITEM_A free."""
		return self._rule(
			suffix,
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}, {"item_code": ITEM_B}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=0,
			free_item=ITEM_A,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
			min_qty=min_qty,
			promotion_type="GWP",
			gwp_paid_qty_basis="Max Price",
			**fields,
		)

	def test_selected_mixed_sku_gift_follows_the_summed_quantity(self):
		lines = [_line(self.ctx, ITEM_A, qty=1), _line(self.ctx, ITEM_B, qty=1)]
		rule = self._mixed("MixedSku", 2)
		resp = self._apply(lines, [rule])
		free = resp.get("free_items") or []
		self.assertEqual([(row["item_code"], row["pricing_rules"]) for row in free], [(ITEM_A, rule)])
		# For another customer, the same cart gets no gift.
		other = self._mixed("MixedSkuOther", 2, applicable_for="Customer", customer=OTHER_CUSTOMER)
		resp = self._apply(lines, [other])
		self.assertEqual(resp.get("free_items") or [], [])

	# ---- matches pos_next G1 (Ruling 154, review m-1) -------------------------

	def test_redeemed_one_time_rule_is_left_out_of_the_lookup(self):
		# (b) A one-time gift this customer redeemed outranks the selected gift: ERPNext would
		# pick it, so the selected one is judged without it, as pos_next does.
		selected = self._gwp("Selected", [ITEM_A], 1, priority="1")
		redeemed = self._gwp("Redeemed", [ITEM_A], 1, priority="5", one_time_per_customer=1)
		frappe.get_doc(
			{"doctype": "One Time Customer Offer Usage", "customer": OTHER_CUSTOMER, "pricing_rule": redeemed}
		).insert(ignore_permissions=True)
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=1)])
		payload["customer"] = OTHER_CUSTOMER
		resp = apply_offers(invoice_data=json.dumps(payload), selected_offers=json.dumps([selected]))
		self.assertEqual([row["pricing_rules"] for row in resp.get("free_items") or []], [selected], resp)

	def test_price_list_is_the_pos_profiles(self):
		# (c) A rule for the profile's price list is judged on that list, whatever the client sends.
		other = "_PNXT_G5_SEL_PL"
		if not frappe.db.exists("Price List", other):
			frappe.get_doc({"doctype": "Price List", "price_list_name": other, "selling": 1}).insert(
				ignore_permissions=True
			)
		profile_list = frappe.db.get_value("POS Profile", self.ctx.pos_profile, "selling_price_list")
		currency = frappe.db.get_value("Price List", profile_list, "currency")
		frappe.db.set_value("Price List", other, "currency", currency)
		lines = [_line(self.ctx, ITEM_A, qty=1)]
		payload = _cart_payload(self.ctx, lines)
		payload["price_list"] = other
		rule = self._gwp("ProfileList", [ITEM_A], 1, for_price_list=profile_list, currency=currency)
		resp = apply_offers(invoice_data=json.dumps(payload), selected_offers=json.dumps([rule]))
		self.assertEqual([row["pricing_rules"] for row in resp.get("free_items") or []], [rule], resp)
