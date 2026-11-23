# Copyright (c) 2026, BrainWise and contributors
"""D-R3-04 (b): a one-time offer redeemed by the invoice being validated is not "already used".

pos_next redeems a one-time offer before it submits the invoice, writing the usage
row for that invoice. Submit then runs the scoped-coupon check, which re-derives the
promotion an "Add on top" coupon stacks on (``coupon_share._rederived_promotions``).
It used to read that same invoice's usage row as an earlier redemption, drop the
offer, find no promotion under the coupon and refuse the sale ("discount 28.0
exceeds its limit of 10.0"). Usage rows and stamps of the invoice being validated
are now ignored; the same customer's other invoices still count.

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute posnext_promotions.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_one_time_own_invoice'}"
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from posnext_promotions.overrides import coupon_share
from posnext_promotions.promotions import ff_compatibility
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_C,
	ITEM_PRICES,
	_make_rule,
	_resolve_customer_group,
	_resolve_territory,
)
from posnext_promotions.tests.test_offline_coupon_overshoot import _shared_ctx

PREFIX = "_PNXT_H1_"
CUSTOMER = f"{PREFIX}OT_CUSTOMER"
OWN = f"{PREFIX}OWN_INVOICE"
EARLIER = f"{PREFIX}EARLIER_INVOICE"
USAGE = "One Time Customer Offer Usage"
PCT = 40


class TestOneTimeOwnInvoice(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.table_exists(USAGE) or not frappe.get_meta("Pricing Rule").has_field(
			"one_time_per_customer"
		):
			raise __import__("unittest").SkipTest("pos_next one-time offers are not installed")
		cls.ctx = _shared_ctx()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("h1_one_time")
		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": CUSTOMER,
					"customer_group": _resolve_customer_group(),
					"territory": _resolve_territory(),
					"customer_type": "Individual",
				}
			).insert(ignore_permissions=True)
		self.rule = self._one_time(f"{PREFIX}OT_40_C", ITEM_C)
		# The same customer's offer on another item, redeemed by an earlier invoice.
		self.other_rule = self._one_time(f"{PREFIX}OT_40_A", ITEM_A)

	@staticmethod
	def _one_time(title, item_code):
		return _make_rule(
			title,
			apply_on="Item Code",
			items=[{"item_code": item_code}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=PCT,
			one_time_per_customer=1,
		)

	def tearDown(self):
		frappe.db.rollback(save_point="h1_one_time")

	def _redeemed_by(self, invoice, rule=None):
		frappe.get_doc(
			{
				"doctype": USAGE,
				"customer": CUSTOMER,
				"pricing_rule": rule or self.rule,
				"sales_invoice": invoice,
				"redemption_date": frappe.utils.now(),
			}
		).insert(ignore_permissions=True, ignore_links=True)

	def _invoice(self):
		"""The invoice being submitted: its stacked "Add on top" coupon row."""
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": CUSTOMER,
				"company": self.ctx.company,
				"pos_profile": self.ctx.pos_profile,
				"posting_date": nowdate(),
				"selling_price_list": self.ctx.price_list,
				"currency": self.ctx.currency,
				"is_pos": 1,
				"items": [
					{
						"item_code": code,
						"qty": 1,
						"uom": "Nos",
						"conversion_factor": 1,
						"price_list_rate": ITEM_PRICES[code],
						"rate": ITEM_PRICES[code] * 0.6 * 0.9,
						"posnext_coupon_code": f"{PREFIX}AOT",
					}
					for code in (ITEM_C, ITEM_A)
				],
			}
		)
		doc.name = OWN
		return doc

	def _promotions(self):
		"""Promotion per unit under the coupon on each line: {item_code: amount}."""
		doc = self._invoice()
		found = coupon_share._rederived_promotions(doc, list(doc.items))
		return {row.item_code: flt(found.get(id(row))) for row in doc.items}

	@staticmethod
	def _rule_row(rule):
		return frappe.get_all("Pricing Rule", filters={"name": rule}, fields=ff_compatibility.rule_fields())[0]

	def test_own_invoice_usage_row_is_not_an_earlier_redemption(self):
		self._redeemed_by(OWN)
		# The coupon stacks on the one-time promotion this invoice redeems: 40% of 20.
		self.assertAlmostEqual(self._promotions()[ITEM_C], ITEM_PRICES[ITEM_C] * PCT / 100, places=2)
		self.assertTrue(ff_compatibility.rule_applies_to_doc(self._rule_row(self.rule), self._invoice()))

	def test_earlier_invoice_usage_row_still_blocks_the_offer(self):
		# One cart: this invoice redeems the ITEM_C offer, an earlier one redeemed ITEM_A's.
		self._redeemed_by(OWN)
		self._redeemed_by(EARLIER, self.other_rule)
		promotions = self._promotions()
		self.assertAlmostEqual(promotions[ITEM_C], ITEM_PRICES[ITEM_C] * PCT / 100, places=2)
		self.assertEqual(promotions[ITEM_A], 0.0, "an earlier redemption must still block the offer")
		invoice = self._invoice()
		self.assertTrue(ff_compatibility.rule_applies_to_doc(self._rule_row(self.rule), invoice))
		self.assertFalse(ff_compatibility.rule_applies_to_doc(self._rule_row(self.other_rule), invoice))
