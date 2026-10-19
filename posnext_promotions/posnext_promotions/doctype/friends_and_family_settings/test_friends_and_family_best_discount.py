# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""PN-128 — best discount only (F&F vs promotion vs coupon vs manual), no stacking."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.api.apply_offers import (
	_decide_fnf_vs_order_promotion,
	_friends_and_family_only_response,
	_prune_suppressed_rules,
	_reconcile_fnf_after_free_passes,
	_strip_txn_header,
)
from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions.best_discount_coupon import (
	REASON_BETTER_DISCOUNT_EXISTS,
	resolve_coupon_best_discount,
)
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
	REASON_APPLIED,
	REASON_INELIGIBLE,
	REASON_ORDER_DISCOUNT_BETTER,
	REASON_OTHER_BETTER,
	apply_friends_and_family_on_invoice,
	apply_friends_and_family_to_items,
	compute_friends_and_family_discounts,
)
from posnext_promotions.promotions.friends_and_family_caps import FIELD_FNF_REPLACED
from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
	ensure_customer_flag_field,
)

DATE = "2026-10-05"
FF_GROUP = "_Test FF Best Customer Group"
OTHER_GROUP = "_Test FF Best Other Customer Group"
ELIGIBLE = "_Test FF Best Eligible Customer"
INELIGIBLE = "_Test FF Best Plain Customer"
ITEM = "_Test FF Best Item"
ITEM_2 = "_Test FF Best Item 2"
EXCLUDED_ITEM = "_Test FF Best Excluded Item"
PROMO_RULE = "_Test FF Best Promo Rule"


class TestFriendsAndFamilyBestDiscount(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()
		root_cg = frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ""}) or (
			"All Customer Groups"
		)
		for name in (FF_GROUP, OTHER_GROUP):
			if not frappe.db.exists("Customer Group", name):
				frappe.get_doc(
					{
						"doctype": "Customer Group",
						"customer_group_name": name,
						"parent_customer_group": root_cg,
						"is_group": 0,
					}
				).insert()
		for name, group in ((ELIGIBLE, FF_GROUP), (INELIGIBLE, OTHER_GROUP)):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": group}).insert()
			frappe.db.set_value("Customer", name, {FLAG_FIELD: 0, "disabled": 0})

		item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or "Products"
		for code in (ITEM, ITEM_2, EXCLUDED_ITEM):
			if not frappe.db.exists("Item", code):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": code,
						"item_name": code,
						"item_group": item_group,
						"stock_uom": "Nos",
						"is_stock_item": 0,
					}
				).insert()

	def setUp(self):
		self._configure()

	def _configure(self, **values):
		doc = frappe.get_single("Friends and Family Settings")
		doc.update(
			{
				"enabled": 1,
				"discount_percentage": 30,
				"discount_scope": "Entire Cart",
				"start_date": None,
				"end_date": None,
				"applicable_brands": [],
				"applicable_item_groups": [],
				"applicable_items": [],
				"excluded_brands": [],
				"excluded_items": [{"item_code": EXCLUDED_ITEM}],
				"excluded_item_groups": [],
				"eligibility_basis": "Customer Group",
				"eligible_customer_groups": [{"customer_group": FF_GROUP}],
			}
		)
		doc.update(values)
		doc.save()
		return doc

	def _line(self, item_code=ITEM, qty=1, rate=100, **extra):
		return frappe._dict(
			{
				"item_code": item_code,
				"qty": qty,
				"price_list_rate": rate,
				"rate": rate,
				"discount_percentage": 0,
				"discount_amount": 0,
				**extra,
			}
		)

	def _promo_line(self, pct=None, amount=None, **extra):
		line = self._line(pricing_rules=PROMO_RULE, discount_source="item_level_promotion", **extra)
		if pct is not None:
			line.discount_percentage = pct
			line.discount_amount = flt(line.price_list_rate) * flt(line.qty) * pct / 100
		if amount is not None:
			line.discount_amount = amount
		return line

	def _coupon(self, pct=None, amount=None, max_amount=None, code="FFBEST", **extra):
		return frappe._dict(
			{
				"coupon_code": code,
				"discount_type": "Percentage" if pct is not None else "Amount",
				"discount_percentage": pct or 0,
				"discount_amount": amount or 0,
				"max_amount": max_amount or 0,
				"min_amount": 0,
				"apply_on": "Net Total",
				"exclude_already_discounted_items": 1,
				**extra,
			}
		)

	# ---- engine primitives ------------------------------------------------

	def test_pick_best_tie_goes_to_fnf(self):
		fnf = bd.Candidate(bd.SOURCE_FRIENDS_AND_FAMILY, 30)
		for source in (bd.SOURCE_PROMOTION, bd.SOURCE_COUPON, bd.SOURCE_MANUAL, bd.SOURCE_ORDER_COUPON):
			other = bd.Candidate(source, 30.004)  # equal to the cent
			self.assertIs(bd.pick_best([other, fnf]), fnf)

	def test_classify_line_sources(self):
		self.assertEqual(bd.classify_line_source(self._promo_line(pct=10)), bd.SOURCE_PROMOTION)
		self.assertEqual(
			bd.classify_line_source(self._line(discount_percentage=10, discount_source="coupon", coupon_code="X")),
			bd.SOURCE_COUPON,
		)
		self.assertEqual(
			bd.classify_line_source(self._line(discount_percentage=10, posnext_coupon_code="X")), bd.SOURCE_COUPON
		)
		self.assertEqual(bd.classify_line_source(self._line(discount_percentage=10)), bd.SOURCE_MANUAL)
		self.assertEqual(bd.classify_line_source(self._line()), bd.SOURCE_NONE)
		self.assertEqual(bd.classify_line_source(self._line(discount_source="gwp", discount_amount=5)), bd.SOURCE_FREE_ITEM)

	# ---- F&F vs automatic promotion (per line) ---------------------------

	def test_fnf_bigger_than_promotion_wins_and_detaches_promo(self):
		line = self._promo_line(pct=10)
		payload = apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(payload["items"][0]["reason"], REASON_APPLIED)
		self.assertEqual(line.discount_percentage, 30)
		self.assertEqual(line.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertFalse(line.pricing_rules)
		self.assertEqual(payload["suppressed_pricing_rules"], [PROMO_RULE])

		decision = payload["items"][0]["best_discount"]
		self.assertEqual(decision["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(decision["outcome"], bd.OUTCOME_FNF_BETTER)
		self.assertEqual(decision["suppressed"], [bd.SOURCE_PROMOTION])
		amounts = {c["source"]: c["amount"] for c in decision["candidates"]}
		self.assertEqual(amounts, {bd.SOURCE_FRIENDS_AND_FAMILY: 30, bd.SOURCE_PROMOTION: 10})
		self.assertIn("Friends & Family", decision["message_en"])
		self.assertIn("الأصدقاء والعائلة", decision["message_ar"])

		summary = payload["best_discount"]
		self.assertEqual(summary["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(summary["fnf_total"], 30)
		self.assertFalse(summary["stacking"])

	def test_promotion_bigger_than_fnf_wins(self):
		line = self._promo_line(pct=40)
		payload = apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(payload["items"][0]["reason"], REASON_OTHER_BETTER)
		self.assertEqual(line.discount_percentage, 40)
		self.assertEqual(line.pricing_rules, PROMO_RULE)
		self.assertNotEqual(line.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		decision = payload["items"][0]["best_discount"]
		self.assertEqual(decision["winner"], bd.SOURCE_PROMOTION)
		self.assertEqual(decision["suppressed"], [bd.SOURCE_FRIENDS_AND_FAMILY])
		self.assertEqual(payload["suppressed_pricing_rules"], [])

	def test_tie_with_promotion_goes_to_fnf(self):
		line = self._promo_line(pct=30)
		payload = apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(payload["items"][0]["applies"])
		self.assertEqual(payload["items"][0]["best_discount"]["outcome"], bd.OUTCOME_TIE_FNF)
		self.assertEqual(line.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)

	def test_compared_in_money_not_percent(self):
		# Fixed 45 off a 100 line (no %) beats F&F 30%.
		line = self._promo_line(amount=45)
		payload = compute_friends_and_family_discounts(customer=ELIGIBLE, items=[line], posting_date=DATE)
		self.assertEqual(payload["items"][0]["reason"], REASON_OTHER_BETTER)
		self.assertEqual(payload["items"][0]["competing_amount"], 45)
		# Fixed 20 loses to F&F 30.
		line = self._promo_line(amount=20)
		payload = compute_friends_and_family_discounts(customer=ELIGIBLE, items=[line], posting_date=DATE)
		self.assertTrue(payload["items"][0]["applies"])

	def test_ineligible_customer_keeps_promotion_only(self):
		line = self._promo_line(pct=10)
		payload = apply_friends_and_family_to_items([line], customer=INELIGIBLE, posting_date=DATE)
		self.assertEqual(payload["items"][0]["reason"], REASON_INELIGIBLE)
		self.assertEqual(line.discount_percentage, 10)
		self.assertEqual(line.pricing_rules, PROMO_RULE)
		self.assertEqual(payload["applied_count"], 0)

	def test_no_stacking_ever_and_idempotent(self):
		lines = [
			self._promo_line(pct=10),
			self._promo_line(pct=45, item_code=ITEM_2),
			self._line(discount_percentage=30, discount_source=DISCOUNT_SOURCE_FRIENDS_AND_FAMILY),
			self._line(item_code=EXCLUDED_ITEM, discount_percentage=5),
			self._line(discount_percentage=15, discount_source="coupon", coupon_code="X"),
		]
		for _round in range(3):
			apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
			expected = [30, 45, 30, 5, 30]
			self.assertEqual([flt(i.discount_percentage) for i in lines], expected)
			for line in lines:
				if line.discount_source == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
					# F&F lines never keep the promotion or coupon that lost.
					self.assertFalse(line.get("pricing_rules"))
					self.assertFalse(line.get("coupon_code"))
				# discount on any line never exceeds the single best candidate
				self.assertLessEqual(flt(line.discount_percentage), 45)

	def test_gift_lines_not_compared_but_bare_gwp_tag_is_not_a_gift(self):
		# GWP rule merely matched (no free units, no discount) → F&F still applies.
		bare = self._line(discount_source="gwp", pricing_rules="PR-GWP")
		# Line actually carrying a free-unit discount → gift kept, F&F skips it.
		gifted = self._line(item_code=ITEM_2, discount_source="gwp", discount_amount=50, gwp_free_qty=1)
		payload = apply_friends_and_family_to_items([bare, gifted], customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(payload["items"][0]["applies"])
		self.assertEqual(bare.pricing_rules, "PR-GWP")  # gift rule stays attached
		self.assertFalse(payload["items"][1]["applies"])
		self.assertEqual(flt(gifted.discount_amount), 50)

	# ---- re-validation keeps the replaced discount (PN-133) ---------------

	def test_revalidate_keeps_comparison_with_replaced_promotion(self):
		line = self._promo_line(pct=10)
		apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(line.get(FIELD_FNF_REPLACED))
		# Second run (e.g. submit after save): the promotion F&F beat is put back
		# and compared again, so the decision and its log survive.
		payload = apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		decision = payload["items"][0]["best_discount"]
		self.assertEqual(decision["outcome"], bd.OUTCOME_FNF_BETTER)
		self.assertEqual(payload["items"][0]["competing_amount"], 10)
		self.assertEqual(line.discount_percentage, 30)
		self.assertFalse(line.pricing_rules)

	def test_program_off_after_stamp_gives_promotion_back(self):
		line = self._promo_line(pct=10)
		apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self._configure(enabled=0)
		apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(flt(line.discount_percentage), 10)
		self.assertEqual(line.pricing_rules, PROMO_RULE)
		self.assertNotEqual(line.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertFalse(line.get(FIELD_FNF_REPLACED))

	def test_plain_line_without_competitor_stores_nothing(self):
		line = self._line()
		apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(line.discount_percentage, 30)
		self.assertFalse(line.get(FIELD_FNF_REPLACED))

	# ---- coupon vs F&F (Jira acceptance) ---------------------------------

	def test_coupon_15_vs_fnf_30_keeps_fnf(self):
		line = self._line(discount_percentage=30, discount_source=DISCOUNT_SOURCE_FRIENDS_AND_FAMILY, is_already_discounted=1)
		result = resolve_coupon_best_discount(self._coupon(pct=15), [line], customer=ELIGIBLE, posting_date=DATE)
		self.assertFalse(result["valid"])
		self.assertEqual(result["reason"], REASON_BETTER_DISCOUNT_EXISTS)
		self.assertEqual(result["line_updates"], [])
		self.assertIn("Friends & Family discount (30%) is better than coupon FFBEST (15%)", result["message_en"])
		self.assertIn("الكوبون FFBEST", result["message_ar"])
		self.assertEqual(result["best_discount"]["lines"][0]["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)

	def test_coupon_40_vs_fnf_30_applies_coupon(self):
		line = self._line(discount_percentage=30, discount_source=DISCOUNT_SOURCE_FRIENDS_AND_FAMILY, is_already_discounted=1)
		result = resolve_coupon_best_discount(self._coupon(pct=40), [line], customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(result["valid"])
		self.assertEqual(len(result["line_updates"]), 1)
		update = result["line_updates"][0]
		self.assertEqual(update["discount_percentage"], 40)
		self.assertEqual(update["replaces"], bd.SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(result["total_discount"], 40)
		decision = result["best_discount"]["lines"][0]
		self.assertEqual(decision["winner"], bd.SOURCE_COUPON)
		self.assertEqual(decision["suppressed"], [bd.SOURCE_FRIENDS_AND_FAMILY])
		self.assertIn("instead of Friends & Family", result["message_en"])

	def test_coupon_equal_to_fnf_keeps_fnf(self):
		line = self._line()
		result = resolve_coupon_best_discount(self._coupon(pct=30), [line], customer=ELIGIBLE, posting_date=DATE)
		self.assertFalse(result["valid"])
		self.assertEqual(result["best_discount"]["lines"][0]["outcome"], bd.OUTCOME_TIE_FNF)

	def test_coupon_per_line_on_mixed_cart(self):
		# F&F covers ITEM (30%) but not EXCLUDED_ITEM; a 20% coupon only wins the latter.
		lines = [self._line(), self._line(item_code=EXCLUDED_ITEM)]
		result = resolve_coupon_best_discount(self._coupon(pct=20), lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(result["valid"])
		self.assertEqual([u["line_key"] for u in result["line_updates"]], [1])
		self.assertEqual(result["total_discount"], 20)
		self.assertEqual(result["best_discount"]["winner"], "mixed")

	def test_fixed_amount_coupon_compared_on_total(self):
		lines = [self._line(), self._line(item_code=ITEM_2)]  # F&F would save 60
		weak = resolve_coupon_best_discount(self._coupon(amount=50), lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertFalse(weak["valid"])
		self.assertEqual(weak["best_discount"]["order"]["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)
		strong = resolve_coupon_best_discount(self._coupon(amount=80), lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(strong["valid"])
		self.assertEqual(len(strong["line_updates"]), 2)
		self.assertEqual(strong["total_discount"], 80)
		self.assertEqual(strong["best_discount"]["order"]["winner"], bd.SOURCE_COUPON)

	def test_coupon_for_ineligible_customer_unchanged(self):
		result = resolve_coupon_best_discount(self._coupon(pct=15), [self._line()], customer=INELIGIBLE, posting_date=DATE)
		self.assertTrue(result["valid"])
		self.assertEqual(result["line_updates"][0]["discount_percentage"], 15)

	def test_coupon_never_overrides_bigger_promotion(self):
		line = self._promo_line(pct=50)
		coupon = self._coupon(pct=20, exclude_already_discounted_items=0)
		result = resolve_coupon_best_discount(coupon, [line], customer=INELIGIBLE, posting_date=DATE)
		self.assertFalse(result["valid"])
		self.assertEqual(result["best_discount"]["lines"][0]["winner"], bd.SOURCE_PROMOTION)

	# ---- invoice validate (server-side enforcement) -----------------------

	def _invoice(self, items, **header):
		return frappe._dict(
			{
				"doctype": "POS Invoice",
				"customer": ELIGIBLE,
				"posting_date": DATE,
				"is_pos": 1,
				"items": items,
				"additional_discount_percentage": 0,
				"discount_amount": 0,
				**header,
			}
		)

	def test_invoice_line_coupon_weaker_than_fnf_is_replaced(self):
		line = self._line(discount_percentage=20, posnext_coupon_code="FFBEST")
		apply_friends_and_family_on_invoice(self._invoice([line]))
		self.assertEqual(line.discount_percentage, 30)
		self.assertFalse(line.posnext_coupon_code)

	def test_invoice_line_coupon_stronger_than_fnf_kept(self):
		line = self._line(discount_percentage=40, posnext_coupon_code="FFBEST")
		line.rate = 60
		apply_friends_and_family_on_invoice(self._invoice([line]))
		self.assertEqual(line.discount_percentage, 40)
		self.assertEqual(line.posnext_coupon_code, "FFBEST")

	def test_invoice_header_discount_weaker_than_fnf_is_removed(self):
		doc = self._invoice([self._line(), self._line(item_code=ITEM_2)], discount_amount=25, coupon_code="NOPE")
		payload = apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 0)
		self.assertFalse(doc.coupon_code)
		self.assertEqual([i.discount_percentage for i in doc.get("items")], [30, 30])
		self.assertEqual(payload["best_discount"]["order"]["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)

	def test_invoice_header_discount_stronger_than_fnf_strips_fnf(self):
		doc = self._invoice([self._line(), self._line(item_code=ITEM_2)], discount_amount=90)
		payload = apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 90)
		self.assertEqual([flt(i.discount_percentage) for i in doc.get("items")], [0, 0])
		self.assertEqual([flt(i.rate) for i in doc.get("items")], [100, 100])
		self.assertEqual(payload["reason"], REASON_ORDER_DISCOUNT_BETTER)
		self.assertEqual(payload["best_discount"]["order"]["winner"], bd.SOURCE_ORDER_MANUAL)

	def test_invoice_saved_fnf_rows_stripped_when_header_stronger(self):
		# Saved rows do not persist discount_source: 30% with no rule/coupon is our stamp.
		rows = [self._line(discount_percentage=30), self._line(item_code=ITEM_2, discount_percentage=30)]
		for row in rows:
			row.rate = 70
		doc = self._invoice(rows, discount_amount=90)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual([flt(i.discount_percentage) for i in rows], [0, 0])
		self.assertEqual([flt(i.rate) for i in rows], [100, 100])
		self.assertEqual(flt(doc.discount_amount), 90)

	def test_invoice_header_stronger_keeps_promotion_lines(self):
		# Header 150 beats F&F (30 + 30): the promotion line keeps its own 10%,
		# only F&F stamps are removed (PN-133 order-level fix).
		promo = self._promo_line(pct=10)
		promo.rate = 90
		doc = self._invoice([promo, self._line(item_code=ITEM_2)], discount_amount=150)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 150)
		self.assertEqual(flt(promo.discount_percentage), 10)
		self.assertEqual(promo.pricing_rules, PROMO_RULE)
		self.assertEqual(flt(doc.get("items")[1].discount_percentage), 0)

	def test_invoice_header_tie_goes_to_fnf(self):
		doc = self._invoice([self._line(), self._line(item_code=ITEM_2)], discount_amount=60)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 0)
		self.assertEqual([i.discount_percentage for i in doc.get("items")], [30, 30])

	def test_real_invoice_totals_recalculated_without_stacking(self):
		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}
		)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": company,
				"customer": ELIGIBLE,
				"posting_date": DATE,
				"set_posting_time": 1,
				"is_pos": 1,
				"items": [{"item_code": ITEM, "qty": 2, "price_list_rate": 100, "rate": 100}],
				"discount_amount": 20,
				"apply_discount_on": "Net Total",
			}
		)
		doc.calculate_taxes_and_totals()
		self.assertEqual(flt(doc.net_total), 180)  # header discount applied on 200

		apply_friends_and_family_on_invoice(doc)
		row = doc.items[0]
		self.assertEqual(flt(row.discount_percentage), 30)
		self.assertEqual(flt(row.rate), 70)
		self.assertEqual(flt(row.discount_amount), 30)  # per unit on saved rows
		self.assertEqual(flt(doc.discount_amount), 0)
		self.assertEqual(flt(doc.net_total), 140)  # F&F only — not 140 - 20

	# ---- apply_offers order-level helpers --------------------------------

	def test_order_promotion_vs_fnf_total_savings(self):
		fnf_lines = [self._line(discount_percentage=30, discount_amount=30), self._line(discount_percentage=30, discount_amount=30)]
		pre_lines = [self._line(), self._line()]
		pricing = [frappe._dict(qty=1, rate=100), frappe._dict(qty=1, rate=100)]
		ff = {"discount_percentage": 30}

		decision, keep_fnf = _decide_fnf_vs_order_promotion(
			fnf_lines, pre_lines, {"discount_amount": 50}, pricing, ff
		)
		self.assertTrue(keep_fnf)  # 60 > 50
		decision, keep_fnf = _decide_fnf_vs_order_promotion(
			fnf_lines, pre_lines, {"additional_discount_percentage": 40}, pricing, ff
		)
		self.assertFalse(keep_fnf)  # 80 > 60
		self.assertEqual(decision["winner"], bd.SOURCE_ORDER_PROMOTION)
		decision, keep_fnf = _decide_fnf_vs_order_promotion(
			fnf_lines, pre_lines, {"discount_amount": 60}, pricing, ff
		)
		self.assertTrue(keep_fnf)  # tie → F&F

	def test_fnf_only_response_strips_stale_stamp_for_ineligible(self):
		stale = self._line(
			discount_percentage=30, discount_amount=30, discount_source=DISCOUNT_SOURCE_FRIENDS_AND_FAMILY
		)
		stale.rate = 70
		manual = self._line(item_code=ITEM_2, discount_percentage=5, discount_amount=5)
		items = [dict(stale), dict(manual)]
		res = _friends_and_family_only_response(
			items, [frappe._dict(i) for i in items], INELIGIBLE, {"posting_date": DATE}
		)
		line, kept = res["items"]
		self.assertEqual((flt(line["rate"]), flt(line["discount_percentage"])), (100, 0))
		self.assertFalse(line.get("discount_source"))
		self.assertEqual(flt(kept["discount_percentage"]), 5)  # non-F&F line returned as sent
		self.assertFalse(res["friends_and_family"]["eligible"])
		self.assertIn("caps", res["friends_and_family"])

	def test_strip_txn_header_drops_price_rules_only(self):
		rule_map = {
			"PR-HDR": frappe._dict(price_or_product_discount="Price"),
			"PR-FREE": frappe._dict(price_or_product_discount="Product"),
		}
		stripped = _strip_txn_header(
			{"applied_rules": {"PR-HDR", "PR-FREE"}, "discount_amount": 10, "additional_discount_percentage": 5},
			rule_map,
		)
		self.assertEqual(stripped["applied_rules"], {"PR-FREE"})
		self.assertEqual(stripped["discount_amount"], 0)
		self.assertEqual(stripped["additional_discount_percentage"], 0)

	def test_reconcile_and_prune_after_free_passes(self):
		lines = [self._promo_line(pct=10), self._line(item_code=ITEM_2)]
		ff = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(ff["applied_count"], 2)
		# A later gift pass re-stamps line 2 with a free-unit discount.
		lines[1].update({"discount_source": "gwp", "discount_percentage": 0, "discount_amount": 50})
		_reconcile_fnf_after_free_passes(ff, lines)
		self.assertEqual(ff["applied_count"], 1)
		self.assertEqual(len(ff["best_discount"]["lines"]), 1)

		applied = {PROMO_RULE, "PR-OTHER"}
		_prune_suppressed_rules(applied, ff, lines, {})
		self.assertEqual(applied, {"PR-OTHER"})
