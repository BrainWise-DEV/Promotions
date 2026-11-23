# Copyright (c) 2026, BrainWise and contributors
"""The server's coupon-only share of a coupon line's discount (G5, D-R2-15).

Needs Sales Invoice Item ``posnext_coupon_share``: run on a migrated site (pos-dev).
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.overrides.coupon_share import FIELD_SHARE, set_coupon_shares, stores_share
from posnext_promotions.promotions import ff_snapshot
from posnext_promotions.promotions.friends_and_family_caps import FIELD_FNF_REPLACED

CODE = "_PNXT_G5_SHARE"
APPLY_OFFERS = "posnext_promotions.api.apply_offers.apply_offers"
CUSTOMER = "_PNXT_G5_CUSTOMER"


def _coupon(conflict="Add on top"):
	return frappe._dict(coupon_code=CODE, promotion_conflict=conflict, discount_type="Percentage")


class TestCouponShare(FrappeTestCase):
	def setUp(self):
		if not frappe.get_meta("Sales Invoice Item").has_field(FIELD_SHARE):
			self.skipTest("posnext_coupon_share is not migrated on this site")

	def _doc(self, **row):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": CUSTOMER,
				"company": "_PNXT_G5_CO",
				"posting_date": frappe.utils.nowdate(),
				"items": [
					{
						"item_code": "_PNXT_G5_ITEM",
						"qty": 2,
						"price_list_rate": 50,
						"rate": 36,
						"discount_amount": 14,
						"posnext_coupon_code": CODE,
						**row,
					}
				],
			}
		)
		self.assertTrue(stores_share(doc))
		return doc

	def _snapshot(self, doc, **data):
		row = doc.items[0]
		values = {
			"discount_source": "item_level_promotion",
			"discount_percentage": 20,
			"discount_amount": 20,  # the cart's line total: 10 a unit
			"rate": 50,
			"pricing_rules": "",
			"posnext_coupon_code": None,
			"coupon_code": None,
			"is_already_discounted": 1,
			**data,
		}
		return ff_snapshot.sign(row, values, doc=frappe._dict(customer=CUSTOMER, company=doc.company))

	def _shares(self, doc, conflict="Add on top"):
		return set_coupon_shares(doc, [doc.items[0]], {CODE: _coupon(conflict)})

	def test_signed_snapshot_gives_the_promotion(self):
		doc = self._doc()
		doc.items[0].set(FIELD_FNF_REPLACED, self._snapshot(doc))
		with patch(APPLY_OFFERS, side_effect=AssertionError("no re-derivation needed")):
			shares = self._shares(doc)
		self.assertEqual(shares[id(doc.items[0])], (4.0, 10.0))
		self.assertAlmostEqual(flt(doc.items[0].get(FIELD_SHARE)), 4)

	def test_forged_snapshot_is_dropped_and_rederived(self):
		doc = self._doc()
		forged = json.loads(self._snapshot(doc))
		forged["data"]["discount_amount"] = 28  # all of it "promotion": a zero coupon share
		doc.items[0].set(FIELD_FNF_REPLACED, json.dumps(forged))
		evaluated = {"items": [{"discount_percentage": 20, "discount_amount": 20, "pricing_rules": "R"}]}
		with patch(APPLY_OFFERS, return_value=evaluated) as apply_offers:
			self._shares(doc)
		apply_offers.assert_called_once()
		sent = apply_offers.call_args.args[0]["items"][0]
		self.assertEqual((sent["rate"], sent["discount_amount"], sent["pricing_rules"]), (50, 0, ""))
		self.assertFalse(doc.items[0].get(FIELD_FNF_REPLACED))
		self.assertAlmostEqual(flt(doc.items[0].get(FIELD_SHARE)), 4)

	def test_same_coupon_snapshot_is_no_promotion(self):
		doc = self._doc()
		doc.items[0].set(
			FIELD_FNF_REPLACED,
			self._snapshot(doc, discount_source="coupon", posnext_coupon_code=CODE, coupon_code=CODE),
		)
		with patch(APPLY_OFFERS, side_effect=AssertionError("no re-derivation needed")):
			self._shares(doc)
		self.assertAlmostEqual(flt(doc.items[0].get(FIELD_SHARE)), 14)

	def test_other_coupon_snapshot_is_no_promotion(self):
		# G5 review m-4: coupon X never stacks on another coupon Y a snapshot holds.
		doc = self._doc()
		doc.items[0].set(
			FIELD_FNF_REPLACED,
			self._snapshot(doc, discount_source="coupon", posnext_coupon_code="_PNXT_G5_Y", coupon_code="_PNXT_G5_Y"),
		)
		with patch(APPLY_OFFERS, side_effect=AssertionError("no re-derivation needed")):
			self._shares(doc)
		self.assertAlmostEqual(flt(doc.items[0].get(FIELD_SHARE)), 14)

	def test_plain_coupon_line_is_its_whole_discount(self):
		doc = self._doc()
		doc.items[0].set(FIELD_FNF_REPLACED, self._snapshot(doc))
		with patch(APPLY_OFFERS, side_effect=AssertionError("a plain coupon is not re-derived")):
			self._shares(doc, conflict="Better of the two")
		self.assertAlmostEqual(flt(doc.items[0].get(FIELD_SHARE)), 14)

	def test_failed_evaluation_counts_the_whole_discount(self):
		doc = self._doc()
		with patch(APPLY_OFFERS, side_effect=frappe.ValidationError("boom")), patch("frappe.log_error"):
			self._shares(doc)
		self.assertAlmostEqual(flt(doc.items[0].get(FIELD_SHARE)), 14)

	def test_client_share_never_survives(self):
		doc = self._doc(posnext_coupon_code=None, **{FIELD_SHARE: 9})
		set_coupon_shares(doc, [], {})
		self.assertEqual(flt(doc.items[0].get(FIELD_SHARE)), 0)
