# Copyright (c) 2026, BrainWise and contributors
"""D-R4-03: an "Add on top" coupon on a Min/Max ("buy one") line can be sold.

The Min/Max post-processor materialises the line ERPNext's way: ``rate`` and
``amount`` set, ``discount_amount`` PER UNIT. Every other evaluated cart line
carries a line-total ``discount_amount``. The coupon-share check read the Min/Max
line's per-unit 8.33 as a line total and divided it by qty again (1.39), so a 10%
coupon on 6 x 50 (buy one, 250.02) appeared to take 22.84% and every Checkout was
refused. The promotion under the coupon is now the server's per-unit figure.

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute posnext_promotions.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_coupon_share_min_max'}"
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from posnext_promotions.overrides import coupon_share, scoped_coupon
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_PRICES,
	_make_rule,
	_resolve_customer_group,
	_resolve_territory,
)
from posnext_promotions.tests.test_offline_coupon_overshoot import _shared_ctx

PREFIX = "_PNXT_H1_"
CUSTOMER = f"{PREFIX}MM_CUSTOMER"
CODE = f"{PREFIX}AOT10"
QTY = 6
PRICE = ITEM_PRICES[ITEM_A]  # 50
# Buy one: 100% off one unit of 6 -> 250.02 (41.67 a unit), 8.33 off a unit.
PROMO_RATE = 41.67
PROMO_PER_UNIT = 8.33


class TestCouponShareMinMax(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.get_meta("POS Coupon").has_field("promotion_conflict"):
			raise __import__("unittest").SkipTest("POS Coupon.promotion_conflict missing")
		cls.ctx = _shared_ctx()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("h1_min_max")
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
		self.rule = _make_rule(
			f"{PREFIX}MM_BUY1",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=100,
			min_qty=2,
			apply_discount_on_price="Min",
			min_or_max_discount_qty_limit=1,
		)
		old = frappe.db.get_value("POS Coupon", {"coupon_code": CODE}, "name")
		if old:
			frappe.delete_doc("POS Coupon", old, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "POS Coupon",
				"coupon_name": CODE,
				"coupon_code": CODE,
				"coupon_type": "Promotional",
				"company": self.ctx.company,
				"discount_type": "Percentage",
				"discount_percentage": 10,
				"apply_scope": "All Eligible Items",
				"promotion_conflict": "Add on top",
				"maximum_use": 0,
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback(save_point="h1_min_max")

	def _invoice(self, coupon_off):
		"""6 x 50 under buy one, the coupon taking ``coupon_off`` of what buy one leaves."""
		rate = flt(PROMO_RATE * (1 - coupon_off), 2)
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
				"coupon_code": CODE,
				"items": [
					{
						"item_code": ITEM_A,
						"qty": QTY,
						"uom": "Nos",
						"conversion_factor": 1,
						"price_list_rate": PRICE,
						"rate": rate,
						"discount_percentage": flt((1 - rate / PRICE) * 100, 6),
						"discount_amount": flt(PRICE - rate, 2),
						"posnext_coupon_code": CODE,
					}
				],
			}
		)
		doc.name = f"{PREFIX}MM_INVOICE"
		doc.items[0].idx = 1
		return doc

	def test_promotion_under_the_coupon_is_the_servers_per_unit_figure(self):
		doc = self._invoice(0.10)
		found = coupon_share._rederived_promotions(doc, list(doc.items))
		self.assertAlmostEqual(found[id(doc.items[0])], PROMO_PER_UNIT, places=2)

	def test_add_on_top_coupon_on_buy_one_line_is_accepted_and_over_it_refused(self):
		# 10% of 41.67 = 4.17 a unit on top of buy one: the coupon's own 10%.
		scoped_coupon.validate_scoped_coupon_lines(self._invoice(0.10))
		# 20% of what buy one leaves is over the coupon's 10%: still refused.
		with self.assertRaises(frappe.ValidationError) as caught:
			scoped_coupon.validate_scoped_coupon_lines(self._invoice(0.20))
		self.assertIn("exceeds POS Coupon", str(caught.exception))
