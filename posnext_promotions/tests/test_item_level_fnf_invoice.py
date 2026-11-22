# Copyright (c) 2026, BrainWise and contributors
"""D-R3-01: an F&F customer buys a line under an Item Level Discount rule.

The cart blocks F&F on the line (Item Level always blocks, Task 8s) and sends the
line at the rule's rate. pos_next saves paid rows with a blank ``pricing_rules``
(its ERPNext re-price guard), so the invoice's F&F pass sees no tag: it must find
the Item Level rule itself, as it does for an untagged Buy X Get Y line, or it
re-prices the line to F&F and Checkout loops on "Invoice Total Changed".

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute posnext_promotions.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_item_level_fnf_invoice'}"
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.promotions import ff_compatibility, ff_config
from posnext_promotions.promotions import friends_and_family_gifts as gifts
from posnext_promotions.promotions.friends_and_family_apply import DISCOUNT_SOURCE_FRIENDS_AND_FAMILY as FNF
from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows, give_verified_email
from posnext_promotions.tests.test_offline_coupon_overshoot import _shared_ctx
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	ITEM_PRICES,
	_apply_offers_and_stamp,
	_cart_payload,
	_line,
	_make_rule,
	_pos_invoice_pipeline,
	_ensure_group_tree,
	_resolve_customer_group,
	_resolve_territory,
)

PREFIX = "_PNXT_H1_"
PLAIN_CUSTOMER = f"{PREFIX}PLAIN_CUSTOMER"
PCT = 15


def _cleanup():
	for name in frappe.get_all("Pricing Rule", filters={"title": ["like", f"{PREFIX}%"]}, pluck="name"):
		frappe.delete_doc("Pricing Rule", name, force=True, ignore_permissions=True)


class TestItemLevelFnFInvoice(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		submit_invoice, update_invoice = _pos_invoice_pipeline()
		if update_invoice is None:
			raise __import__("unittest").SkipTest("pos_next invoice APIs are not installed")
		cls.submit_invoice, cls.update_invoice = staticmethod(submit_invoice), staticmethod(update_invoice)
		cls.ctx = _shared_ctx()
		give_verified_email(cls.ctx.customer)
		if not frappe.db.exists("Customer", PLAIN_CUSTOMER):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": PLAIN_CUSTOMER,
					"customer_group": _resolve_customer_group(),
					"territory": _resolve_territory(),
					"customer_type": "Individual",
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		_cleanup()
		if frappe.db.exists("Customer", PLAIN_CUSTOMER) and not frappe.db.exists(
			"Sales Invoice", {"customer": PLAIN_CUSTOMER}
		):
			frappe.delete_doc("Customer", PLAIN_CUSTOMER, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("h1_item_level")
		_cleanup()
		self._settings()

	def _settings(self, **values):
		settings = frappe.get_single("Friends and Family Settings")
		settings.update(
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
				**values,
			}
		)
		settings.save()

	def tearDown(self):
		frappe.db.rollback(save_point="h1_item_level")
		# The Settings save above was rolled back: drop the cached copy too, so other
		# processes on a shared dev site never read it (review m6).
		frappe.clear_document_cache("Friends and Family Settings", "Friends and Family Settings")
		ff_config.clear_cache()

	def _rule(self, fresh: bool):
		fields = dict(
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=PCT,
		)
		if not frappe.get_meta("Pricing Rule").has_field("promotion_type"):
			self.skipTest("Pricing Rule.promotion_type custom field missing")
		if fresh:
			return _make_rule(f"{PREFIX}IL_FRESH", promotion_type=gifts.PROMOTION_TYPE_ITEM_LEVEL, **fields)
		# A Price rule the user switched to Item Level afterwards.
		rule = _make_rule(f"{PREFIX}IL_SWITCHED", **fields)
		doc = frappe.get_doc("Pricing Rule", rule)
		doc.promotion_type = gifts.PROMOTION_TYPE_ITEM_LEVEL
		doc.save(ignore_permissions=True)
		return rule

	def _cart(self, customer, rule):
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A)])
		payload["customer"] = customer
		self.resp = _apply_offers_and_stamp(payload, [rule])
		return payload

	def _cart_line(self, item_code):
		return next(i for i in self.resp["items"] if i["item_code"] == item_code and not i.get("is_free_item"))

	def _submit(self, draft):
		draft["payments"] = [{"mode_of_payment": self.ctx.mode_of_payment, "amount": flt(draft["grand_total"])}]
		self.submit_invoice(
			invoice=json.dumps(draft, default=str),
			data=json.dumps({"change_amount": 0, "write_off_amount": 0}),
		)
		sale = frappe.get_doc("Sales Invoice", draft["name"])
		self.assertEqual(sale.docstatus, 1)
		return sale

	def _agree(self, lines, rules, blocked, msg=""):
		"""Cart vs checked draft vs sold invoice, line by line: same rates, so no
		"Invoice Total Changed". ``blocked``: item codes the cart must block for F&F."""
		payload = _cart_payload(self.ctx, [_line(self.ctx, code, qty=qty) for code, qty in lines])
		self.resp = _apply_offers_and_stamp(payload, rules)
		cart = {line["item_code"]: flt(line["rate"]) for line in payload["items"] if not line.get("is_free_item")}
		for code, _qty in lines:
			verdict = self._cart_line(code).get("fnf_blocked_by_scheme") or ""
			if code in blocked:
				self.assertTrue(verdict, f"{msg}: cart {code} not blocked")
				self.assertNotEqual(self._cart_line(code).get("discount_source"), FNF, f"{msg}: cart {code}")
			else:
				self.assertFalse(verdict, f"{msg}: cart {code} blocked by {verdict}")
		draft = self.update_invoice(json.dumps(payload))
		drafted = {r["item_code"]: flt(r["rate"]) for r in draft["items"] if not r.get("is_free_item")}
		for code, rate in cart.items():
			self.assertAlmostEqual(drafted[code], rate, places=2, msg=f"{msg}: {code} cart vs draft")
		sale = self._submit(draft)
		for row in sale.items:
			if not row.is_free_item:
				self.assertAlmostEqual(flt(row.rate), cart[row.item_code], places=2, msg=f"{msg}: {row.item_code} sold")
		return cart

	def _assert_rule_price(self, row, msg):
		expected = ITEM_PRICES[ITEM_A] * (100 - PCT) / 100
		self.assertAlmostEqual(flt(row.get("rate")), expected, places=2, msg=msg)
		self.assertNotEqual(row.get("posnext_discount_source"), FNF, msg)

	def _sell(self, customer, rule, msg):
		payload = self._cart(customer, rule)
		line = payload["items"][0]
		# The cart: the rule's price and tag, no F&F.
		self._assert_rule_price(line, f"{msg}: cart")
		self.assertIn(rule, line.get("pricing_rules") or "", f"{msg}: cart tag")
		self.assertIn(rule, self._cart_line(ITEM_A).get("fnf_blocked_by_scheme") or "", f"{msg}: cart verdict")

		draft = self.update_invoice(json.dumps(payload))
		paid = [r for r in draft["items"] if not r.get("is_free_item")]
		self.assertEqual(len(paid), 1, msg)
		# The server's checked draft agrees with the cart (no "Invoice Total Changed").
		self._assert_rule_price(paid[0], f"{msg}: draft")
		self.assertAlmostEqual(flt(draft["grand_total"]), flt(line["rate"]), places=2, msg=f"{msg}: total")

		sale = self._submit(draft)
		self._assert_rule_price(sale.items[0].as_dict(), f"{msg}: sold")
		# The server-confirmed tag is kept on the sold row (Ruling 137).
		self.assertEqual(sale.items[0].pricing_rules, rule, f"{msg}: sold tag")

	def test_fresh_item_level_rule_blocks_fnf_on_the_invoice(self):
		rule = self._rule(fresh=True)
		for allow in (0, 1):
			self._settings(allow_free_gift_with_ff=allow)
			self._sell(self.ctx.customer, rule, f"F&F customer, fresh rule, allow={allow}")
		# Control: the same line still sells to a plain customer.
		self._sell(PLAIN_CUSTOMER, rule, "plain customer, fresh rule")

	def test_price_rule_switched_to_item_level_blocks_fnf_on_the_invoice(self):
		rule = self._rule(fresh=False)
		self._sell(self.ctx.customer, rule, "F&F customer, switched rule")
		self._sell(PLAIN_CUSTOMER, rule, "plain customer, switched rule")

	def test_other_lines_still_get_fnf(self):
		rule = self._rule(fresh=True)
		payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)])
		_apply_offers_and_stamp(payload, [rule])
		draft = self.update_invoice(json.dumps(payload))
		rows = {r["item_code"]: r for r in draft["items"] if not r.get("is_free_item")}
		self._assert_rule_price(rows[ITEM_A], "Item Level line")
		self.assertEqual(rows[ITEM_B].get("posnext_discount_source"), FNF)
		self.assertAlmostEqual(flt(rows[ITEM_B]["rate"]), ITEM_PRICES[ITEM_B] * 0.7, places=2)

	def test_item_level_cap_gives_fnf_back_above_max_qty(self):
		# ERPNext gives the rule's discount only up to max_qty: within it the line is the
		# rule's; above it the line has no promotion and F&F applies, on cart and invoice.
		rule = self._rule(fresh=True)
		frappe.db.set_value("Pricing Rule", rule, "max_qty", 2)
		frappe.clear_document_cache("Pricing Rule", rule)
		for qty, fnf in ((2, False), (3, True)):
			msg = f"qty {qty}"
			payload = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A, qty=qty)])
			_apply_offers_and_stamp(payload, [rule])
			draft = self.update_invoice(json.dumps(payload))
			row = next(r for r in draft["items"] if not r.get("is_free_item"))
			self.assertAlmostEqual(flt(row["rate"]), flt(payload["items"][0]["rate"]), places=2, msg=f"{msg}: cart vs draft")
			if fnf:
				self.assertEqual(row.get("posnext_discount_source"), FNF, msg)
				self.assertAlmostEqual(flt(row["rate"]), ITEM_PRICES[ITEM_A] * 0.7, places=2, msg=msg)
			else:
				self._assert_rule_price(row, msg)
			sold = self._submit(draft).items[0]
			self.assertAlmostEqual(flt(sold.rate), flt(row["rate"]), places=2, msg=f"{msg}: sold")
			self.assertEqual(sold.pricing_rules, "" if fnf else rule, f"{msg}: sold tag")

	# ---- Review round 1 (Ruling 159) ---------------------------------------------

	def _item_level(self, title, **fields):
		if not frappe.get_meta("Pricing Rule").has_field("promotion_type"):
			self.skipTest("Pricing Rule.promotion_type custom field missing")
		base = dict(
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=PCT,
			promotion_type=gifts.PROMOTION_TYPE_ITEM_LEVEL,
		)
		base.update(fields)
		return _make_rule(f"{PREFIX}{title}", **base)

	def test_mixed_conditions_rule_agrees_on_cart_and_invoice(self):
		# Probe A: "any 2 of A / B, 15% off". Each line alone is below min_qty 2.
		rule = self._item_level(
			"IL_MIXED", items=[{"item_code": ITEM_A}, {"item_code": ITEM_B}], mixed_conditions=1, min_qty=2
		)
		cart = self._agree([(ITEM_A, 1), (ITEM_B, 1)], [rule], blocked={ITEM_A, ITEM_B}, msg="mixed")
		self.assertAlmostEqual(cart[ITEM_A], ITEM_PRICES[ITEM_A] * 0.85, places=2)
		self.assertAlmostEqual(cart[ITEM_B], ITEM_PRICES[ITEM_B] * 0.85, places=2)

	def test_apply_rule_on_other_rule_agrees_on_cart_and_invoice(self):
		# Probe G: "buy A, 15% off B". B is outside the rule's own items.
		rule = self._item_level("IL_OTHER", apply_rule_on_other="Item Code", other_item_code=ITEM_B)
		self._agree([(ITEM_A, 1), (ITEM_B, 1)], [rule], blocked={ITEM_A, ITEM_B}, msg="apply_rule_on_other")

	def _cumulative(self, title, history_margin):
		"""A cumulative Item Level rule whose min_qty is ERPNext's history of ITEM_A in its
		window plus ``history_margin``: a qty-1 line meets it at margin 1, not at 2."""
		from erpnext.accounts.doctype.pricing_rule.utils import get_qty_amount_data_for_cumulative

		rule = self._item_level(
			title,
			is_cumulative=1,
			valid_from=frappe.utils.add_days(frappe.utils.nowdate(), -30),
			valid_upto=frappe.utils.add_days(frappe.utils.nowdate(), 30),
		)
		history = get_qty_amount_data_for_cumulative(
			frappe.get_doc("Pricing Rule", rule), frappe._dict(doctype="Sales Invoice"), [ITEM_A]
		)
		frappe.db.set_value("Pricing Rule", rule, "min_qty", flt(history[0] if history else 0) + history_margin)
		frappe.clear_document_cache("Pricing Rule", rule)
		return rule

	def test_cumulative_rule_met_by_history_blocks_on_cart_and_invoice(self):
		# ERPNext adds the posted history to the line: met, the line is the rule's.
		rule = self._cumulative("IL_CUMULATIVE_MET", 1)
		cart = self._agree([(ITEM_A, 1), (ITEM_B, 1)], [rule], blocked={ITEM_A}, msg="cumulative met")
		self.assertAlmostEqual(cart[ITEM_A], ITEM_PRICES[ITEM_A] * 0.85, places=2)

	def test_cumulative_rule_short_of_history_keeps_fnf(self):
		# I-1: a history that does not reach min_qty gives no promotion: the line keeps F&F.
		rule = self._cumulative("IL_CUMULATIVE_SHORT", 2)
		cart = self._agree([(ITEM_A, 1)], [rule], blocked=set(), msg="cumulative short")
		self.assertAlmostEqual(cart[ITEM_A], ITEM_PRICES[ITEM_A] * 0.7, places=2)

	def test_conditions_are_judged_on_erpnext_pricing_doc(self):
		# C-R1-1: the cart's ERPNext saw an unsaved Sales Invoice with the customer's group
		# and territory, is_pos 1 and no totals; both judges must evaluate the same names.
		group = frappe.db.get_value("Customer", self.ctx.customer, "customer_group")
		cases = (
			# (condition, ERPNext applies it on the cart)
			("customer_group == 'All Customer Groups'", group == "All Customer Groups"),
			("is_pos == 1", True),
			("grand_total > 10", False),
		)
		for n, (condition, applies) in enumerate(cases):
			rule = self._item_level(f"IL_COND_{n}", condition=condition)
			blocked = {ITEM_A} if applies else set()
			cart = self._agree([(ITEM_A, 1)], [rule], blocked=blocked, msg=condition)
			expected = 0.85 if applies else 0.7
			self.assertAlmostEqual(cart[ITEM_A], ITEM_PRICES[ITEM_A] * expected, places=2, msg=condition)
			frappe.db.set_value("Pricing Rule", rule, "disable", 1)
			frappe.clear_document_cache("Pricing Rule", rule)

	def test_rule_whose_condition_is_false_does_not_block(self):
		# Probe F: ERPNext never applies a rule whose condition is false: the line is F&F's.
		rule = self._item_level("IL_NEVER", condition="0")
		cart = self._agree([(ITEM_A, 1)], [rule], blocked=set(), msg="condition false")
		self.assertAlmostEqual(cart[ITEM_A], ITEM_PRICES[ITEM_A] * 0.7, places=2)

	# ---- The judge directly --------------------------------------------------------

	def _judge(self, lines):
		doc = frappe._dict(
			customer=self.ctx.customer,
			company=self.ctx.company,
			pos_profile=self.ctx.pos_profile,
			posting_date=frappe.utils.nowdate(),
			items=lines,
		)
		ff_compatibility.block_ineligible_lines(lines, doc=doc)
		return [gifts._split_rule_names(line.get(gifts.FIELD_FNF_BLOCKED_BY)) for line in lines]

	def _judge_line(self, qty=1, **fields):
		line = frappe._dict(_line(self.ctx, ITEM_A, qty=qty), **fields)
		line.setdefault("item_group", frappe.db.get_value("Item", ITEM_A, "item_group"))
		return line

	def test_mixed_sums_count_free_rows(self):
		# m-r1-1: ERPNext sums every in-scope row, free rows included.
		rule = self._item_level("IL_MIXED_FREE", mixed_conditions=1, min_qty=2)
		paid = self._judge_line(1)
		free = self._judge_line(1, is_free_item=1, rate=0)
		self.assertEqual(self._judge([paid, free])[0], [rule])

	def test_tagged_item_level_line_above_max_qty_is_not_blocked(self):
		# m1: a tag above the cap is judged as the untagged line is.
		rule = self._item_level("IL_TAG_CAP", max_qty=2)
		self.assertEqual(self._judge([self._judge_line(2, pricing_rules=rule)]), [[rule]])
		self.assertEqual(self._judge([self._judge_line(3, pricing_rules=rule)]), [[]])

	def test_amount_thresholds_use_the_price_list_rate(self):
		# m4: ERPNext measures min_amt on price_list_rate x qty, never on the rate charged.
		rule = self._item_level("IL_MIN_AMT", min_amt=40)
		self.assertEqual(self._judge([self._judge_line(price_list_rate=0, rate=50)]), [[]])
		self.assertEqual(self._judge([self._judge_line(price_list_rate=50, rate=10)]), [[rule]])

	def test_group_scope_is_expanded_once_per_rule(self):
		# I1: 30 Item Group rules x 20 lines used to expand the group tree 600 times.
		parent, child = _ensure_group_tree()
		rules = [
			self._item_level(f"IL_GROUP_{n}", apply_on="Item Group", items=[], item_groups=[{"item_group": parent}])
			for n in range(30)
		]
		lines = [self._judge_line(item_group=child) for _n in range(20)]
		frappe.clear_cache(doctype="Pricing Rule")
		self._judge([frappe._dict(line) for line in lines])  # warm the document cache
		original = frappe.db.sql
		queries = []

		def counting(query, *args, **kwargs):
			queries.append(str(query))
			return original(query, *args, **kwargs)

		frappe.db.sql = counting
		try:
			verdicts = self._judge(lines)
		finally:
			frappe.db.sql = original
		self.assertTrue(all(set(rules) <= set(v) for v in verdicts))
		tree = [q for q in queries if "tabItem Group" in q]
		self.assertLessEqual(len(tree), len(rules), f"{len(tree)} Item Group queries, {len(queries)} in all")
