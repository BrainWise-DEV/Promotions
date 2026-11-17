# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Cart and invoice apply the same F&F compatibility rules (ff_compatibility).

* Buy X Get Y lines get no F&F on the invoice either, tagged or not.
* Allow Free Gift with F&F off: the invoice removes a gift whose trigger line got F&F.
* Parity: rows built from the cart output, as the POS submits them, keep the
  same meaning after invoice validate.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, cstr, flt, nowdate

from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.promotions import ff_compatibility, ff_config
from posnext_promotions.promotions import friends_and_family_gifts as gifts
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY as FNF,
	apply_friends_and_family_on_invoice,
)
from posnext_promotions.promotions.friends_and_family_caps import FIELD_DISCOUNT_SOURCE
from posnext_promotions.promotions.friends_and_family_testing import (
	approved_domain_rows,
	ff_settings_guard,
	give_verified_email,
)
from posnext_promotions.test_promotions import ITEM_A, ITEM_B, ITEM_C, ITEM_PRICES, _cart_payload, _ctx, _line, _make_rule

PREFIX = "_PNXT_FFC_"


def _source(row) -> str:
	return FNF if FNF in (cstr(row.get("discount_source")), cstr(row.get(FIELD_DISCOUNT_SOURCE))) else ""


def _pos_rows(resp) -> list:
	"""Rows the POS submits from an apply_offers response (formatItemsForSubmission).

	Only the fields the POS really sends: no ``discount_source``, ``free_qty`` or
	``gwp_free_qty``. Each row also carries the cart's ``discount_source`` under
	``_cart_source`` / ``_cart_unit_discount`` for the comparison (stripped before validate).
	"""
	rows = []

	def paid(ri):
		qty = flt(ri.get("qty"))
		plr = flt(ri.get("price_list_rate"))
		discount = flt(ri.get("discount_amount"))
		return {
			"item_code": ri.get("item_code"),
			"qty": qty,
			"rate": (plr * qty - discount) / qty if qty else plr,
			"price_list_rate": plr,
			"uom": ri.get("uom") or "Nos",
			"conversion_factor": 1,
			"discount_percentage": flt(ri.get("discount_percentage")),
			"discount_amount": discount,
			"pricing_rules": cstr(ri.get("pricing_rules")),
			"is_free_item": 0,
			"_cart_source": _source(ri),
			# Cart discount_amount is the line total; invoice rows hold it per unit.
			"_cart_unit_discount": discount / qty if qty else 0,
		}

	def free(code, qty, rules):
		return {
			"item_code": code,
			"qty": qty,
			"rate": 0,
			"price_list_rate": 0,
			"uom": "Nos",
			"conversion_factor": 1,
			"discount_percentage": 0,
			"discount_amount": 0,
			"pricing_rules": cstr(rules),
			"is_free_item": 1,
			"_cart_source": "",
			"_cart_unit_discount": 0,
		}

	free_items = resp.get("free_items") or []
	for ri in resp.get("items") or []:
		if cint(ri.get("is_free_item")):
			continue
		rows.append(paid(ri))
		fq = flt(ri.get("free_qty"))
		if fq > 0 and not any(f.get("item_code") == ri.get("item_code") for f in free_items):
			rows.append(free(ri.get("item_code"), fq, ri.get("pricing_rules")))
	for fi in free_items:
		fq = flt(fi.get("qty"))
		if cint(fi.get("gwp_same_item_row")):
			for row in rows:
				if not row["is_free_item"] and row["item_code"] == fi.get("item_code"):
					row["qty"] = max(0, row["qty"] - fq)
					break
		rows.append(free(fi.get("item_code"), fq, fi.get("pricing_rules")))
	return rows


def _tuple(row, source, unit_discount):
	return (
		cstr(row.get("item_code")),
		cint(row.get("is_free_item")),
		flt(row.get("qty")),
		source,
		round(flt(unit_discount), 2),
	)


class TestFFCompatibility(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ctx = _ctx()
		give_verified_email(cls.ctx.customer)

	def setUp(self):
		frappe.set_user("Administrator")
		self._stack = contextlib.ExitStack()
		self._stack.enter_context(ff_settings_guard())
		frappe.db.savepoint("ff_compat")
		for name in frappe.get_all("Pricing Rule", filters={"title": ["like", f"{PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("Pricing Rule", name, force=True, ignore_permissions=True)
		gifts.clear_rule_cache()
		self._settings()

	def tearDown(self):
		frappe.db.rollback(save_point="ff_compat")
		self._stack.close()
		gifts.clear_rule_cache()
		ff_config.clear_cache()

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
		ff_config.clear_cache()
		return doc

	# ---- fixtures ----------------------------------------------------------

	def _bxgy(self, **extra):
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
		return _make_rule(f"{PREFIX}BXGY", **fields)

	def _gift(self, trigger=ITEM_B, promotion_type=None, triggers=None):
		rule = _make_rule(
			f"{PREFIX}GIFT",
			apply_on="Item Code",
			items=[{"item_code": code} for code in (triggers or [trigger])],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=0,
			free_item=ITEM_C,
			min_qty=1,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
		)
		if promotion_type:
			if not frappe.get_meta("Pricing Rule").has_field("promotion_type"):
				self.skipTest("Pricing Rule.promotion_type custom field missing")
			frappe.db.set_value("Pricing Rule", rule, "promotion_type", promotion_type)
			frappe.clear_document_cache("Pricing Rule", rule)
			gifts.clear_rule_cache()
		return rule

	def _row(self, item_code, qty=1, rules="", free=False, idx=None):
		plr = 0 if free else ITEM_PRICES[item_code]
		return frappe._dict(
			{
				"idx": idx,
				"item_code": item_code,
				"qty": qty,
				"rate": plr,
				"price_list_rate": plr,
				"discount_percentage": 0,
				"discount_amount": 0,
				"pricing_rules": rules,
				"is_free_item": 1 if free else 0,
			}
		)

	def _invoice(self, rows):
		for position, row in enumerate(rows, start=1):
			row.idx = row.idx or position
		return frappe._dict(
			{
				"doctype": "POS Invoice",
				"is_pos": 1,
				"company": self.ctx.company,
				"customer": self.ctx.customer,
				"pos_profile": self.ctx.pos_profile,
				"selling_price_list": self.ctx.price_list,
				"posting_date": nowdate(),
				"additional_discount_percentage": 0,
				"discount_amount": 0,
				"items": rows,
			}
		)

	def _apply(self, lines, rules):
		payload = _cart_payload(self.ctx, [_line(self.ctx, code, qty=qty) for code, qty in lines])
		return apply_offers(invoice_data=json.dumps(payload), selected_offers=json.dumps(list(rules)))

	@staticmethod
	def _paid(doc, item_code):
		return next(r for r in doc.get("items") if r.item_code == item_code and not cint(r.is_free_item))

	# ---- BXGY on the invoice ---------------------------------------------

	def test_invoice_blocks_bxgy_line(self):
		rule = self._bxgy()
		doc = self._invoice(
			[self._row(ITEM_A, 2, rule), self._row(ITEM_A, 1, rule, free=True), self._row(ITEM_B, 1)]
		)
		apply_friends_and_family_on_invoice(doc)
		line_a = self._paid(doc, ITEM_A)
		self.assertNotEqual(_source(line_a), FNF)
		self.assertEqual(flt(line_a.discount_percentage), 0)
		self.assertEqual(_source(self._paid(doc, ITEM_B)), FNF)
		self.assertEqual(flt(self._paid(doc, ITEM_B).discount_percentage), 30)

	def test_invoice_blocks_untagged_bxgy_line_above_max_qty(self):
		# Capped B1G1 at qty 4: ERPNext tags nothing and adds no free row, but the
		# cart still blocks the line by scope + min_qty. The invoice must agree.
		rule = self._bxgy(max_qty=3)
		cart = self._apply([(ITEM_A, 4), (ITEM_B, 1)], [rule])
		cart_a = next(i for i in cart["items"] if i["item_code"] == ITEM_A and not i.get("is_free_item"))
		self.assertNotEqual(_source(cart_a), FNF)

		doc = self._invoice([self._row(ITEM_A, 4), self._row(ITEM_B, 1)])
		apply_friends_and_family_on_invoice(doc)
		self.assertNotEqual(_source(self._paid(doc, ITEM_A)), FNF)
		self.assertEqual(_source(self._paid(doc, ITEM_B)), FNF)

	def test_block_ineligible_lines_returns_positions(self):
		rule = self._bxgy()
		rows = [self._row(ITEM_B, 1, idx=7), self._row(ITEM_A, 2, rule, idx=3)]
		self.assertEqual(ff_compatibility.block_ineligible_lines(rows), {1})
		self.assertEqual(rows[1].get(gifts.FIELD_FNF_BLOCKED_BY), rule)

	# ---- gifts on the invoice --------------------------------------------

	def _gift_invoice(self, rule):
		return self._invoice([self._row(ITEM_B, 1, rule), self._row(ITEM_C, 1, rule, free=True)])

	def test_invoice_removes_gift_when_not_allowed(self):
		rule = self._gift(promotion_type=gifts.PROMOTION_TYPE_GWP)
		doc = self._gift_invoice(rule)
		apply_friends_and_family_on_invoice(doc)
		self.assertFalse([r for r in doc.get("items") if cint(r.is_free_item)])
		line_b = self._paid(doc, ITEM_B)
		self.assertEqual(_source(line_b), FNF)
		self.assertNotIn(rule, gifts._item_rule_names(line_b))

	def test_invoice_document_removes_gift_row_and_renumbers(self):
		rule = self._gift(promotion_type=gifts.PROMOTION_TYPE_GWP)
		doc = frappe.get_doc(
			{
				"doctype": "POS Invoice",
				"company": self.ctx.company,
				"customer": self.ctx.customer,
				"pos_profile": self.ctx.pos_profile,
				"is_pos": 1,
				"currency": self.ctx.currency,
				"conversion_rate": 1,
				"selling_price_list": self.ctx.price_list,
				"price_list_currency": self.ctx.currency,
				"plc_conversion_rate": 1,
				"posting_date": nowdate(),
				"items": [
					{**self._row(ITEM_C, 1, rule, free=True), "idx": None, "warehouse": self.ctx.warehouse, "uom": "Nos", "conversion_factor": 1},
					{**self._row(ITEM_B, 1, rule), "idx": None, "warehouse": self.ctx.warehouse, "uom": "Nos", "conversion_factor": 1},
				],
			}
		)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual([(r.idx, r.item_code) for r in doc.items], [(1, ITEM_B)])
		self.assertEqual(_source(doc.items[0]), FNF)
		self.assertAlmostEqual(flt(doc.items[0].rate), ITEM_PRICES[ITEM_B] * 0.7, places=2)

	def test_invoice_gift_policy_stable_across_revalidate(self):
		# Save then submit validate twice; F&F replaces a weaker 10% promotion here.
		gift = self._gift(promotion_type=gifts.PROMOTION_TYPE_GWP)
		price = _make_rule(
			f"{PREFIX}PRICE10",
			apply_on="Item Code",
			items=[{"item_code": ITEM_B}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=10,
		)
		trigger = self._row(ITEM_B, 1, f"{gift},{price}")
		trigger.update({"discount_percentage": 10, "discount_amount": 8, "rate": 72})
		doc = self._invoice([trigger, self._row(ITEM_C, 1, gift, free=True)])

		def state():
			return [
				(r.item_code, cint(r.is_free_item), flt(r.qty), _source(r), flt(r.rate, 2), flt(r.discount_amount, 2))
				for r in doc.get("items")
			]

		apply_friends_and_family_on_invoice(doc)
		first = state()
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(state(), first)
		self.assertEqual(first, [(ITEM_B, 0, 1.0, FNF, 56.0, 24.0)])

	def test_invoice_keeps_gift_when_allowed(self):
		self._settings(allow_free_gift_with_ff=1)
		rule = self._gift(promotion_type=gifts.PROMOTION_TYPE_GWP)
		doc = self._gift_invoice(rule)
		apply_friends_and_family_on_invoice(doc)
		free_rows = [r for r in doc.get("items") if cint(r.is_free_item)]
		self.assertEqual([r.item_code for r in free_rows], [ITEM_C])
		self.assertEqual(_source(self._paid(doc, ITEM_B)), FNF)

	def test_invoice_keeps_gift_when_trigger_has_no_fnf(self):
		self._settings(excluded_items=[{"item_code": ITEM_B}])
		rule = self._gift()
		doc = self._invoice([self._row(ITEM_B, 1, rule), self._row(ITEM_C, 1, rule, free=True), self._row(ITEM_A, 1)])
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual([r.item_code for r in doc.get("items") if cint(r.is_free_item)], [ITEM_C])
		self.assertEqual(_source(self._paid(doc, ITEM_A)), FNF)

	def test_normalize_keys_rows_not_positions(self):
		rule = self._gift()
		rows = [self._row(ITEM_B, 1, rule, idx=5), self._row(ITEM_C, 1, rule, free=True, idx=9)]
		snapshot = ff_compatibility.normalize(rows, [rows[1]])
		self.assertEqual(list(snapshot.lines), [5, 9])
		self.assertEqual(len(snapshot.gifts), 1)
		gift = snapshot.gifts[0]
		self.assertEqual((gift.rule_name, gift.item_code, gift.qty, gift.trigger_keys), (rule, ITEM_C, 1, (5,)))

	# ---- fix round 1 ------------------------------------------------------

	def _price_rule(self, item_code, pct=10, title="PRICE10"):
		return _make_rule(
			f"{PREFIX}{title}",
			apply_on="Item Code",
			items=[{"item_code": item_code}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=pct,
		)

	def test_one_time_bxgy_already_redeemed_does_not_block(self):
		if not frappe.get_meta("Pricing Rule").has_field("one_time_per_customer"):
			self.skipTest("one_time_per_customer field missing")
		# The test profile's default (walk-in) customer is the test customer: clear it.
		frappe.db.set_value("POS Profile", self.ctx.pos_profile, "customer", None)
		frappe.clear_document_cache("POS Profile", self.ctx.pos_profile)
		self.addCleanup(frappe.clear_document_cache, "POS Profile", self.ctx.pos_profile)
		rule = self._bxgy(one_time_per_customer=1)
		doc = self._invoice([self._row(ITEM_A, 2), self._row(ITEM_B, 1)])
		self.assertIn(rule, ff_compatibility.blocking_rule_map(doc))
		frappe.get_doc(
			{"doctype": "One Time Customer Offer Usage", "customer": self.ctx.customer, "pricing_rule": rule}
		).insert(ignore_permissions=True)
		self.assertNotIn(rule, ff_compatibility.blocking_rule_map(doc))
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(_source(self._paid(doc, ITEM_A)), FNF)

	def test_one_time_bxgy_never_blocks_anonymous_sale(self):
		if not frappe.get_meta("Pricing Rule").has_field("one_time_per_customer"):
			self.skipTest("one_time_per_customer field missing")
		rule = self._bxgy(one_time_per_customer=1)
		doc = self._invoice([self._row(ITEM_A, 2)])
		# The test customer is the profile's default (walk-in) customer.
		self.assertEqual(frappe.db.get_value("POS Profile", self.ctx.pos_profile, "customer"), self.ctx.customer)
		self.assertNotIn(rule, ff_compatibility.blocking_rule_map(doc))
		doc.customer = None
		self.assertNotIn(rule, ff_compatibility.blocking_rule_map(doc))

	def test_cart_skips_one_time_rule_already_redeemed(self):
		if not frappe.get_meta("Pricing Rule").has_field("one_time_per_customer"):
			self.skipTest("one_time_per_customer field missing")
		# F&F off so the 10% rule is the only discount; the profile's walk-in customer cleared.
		self._settings(enabled=0)
		frappe.db.set_value("POS Profile", self.ctx.pos_profile, "customer", None)
		frappe.clear_document_cache("POS Profile", self.ctx.pos_profile)
		self.addCleanup(frappe.clear_document_cache, "POS Profile", self.ctx.pos_profile)
		rule = self._price_rule(ITEM_B)
		frappe.db.set_value("Pricing Rule", rule, "one_time_per_customer", 1)
		frappe.clear_document_cache("Pricing Rule", rule)

		for selected in ([rule], []):
			resp = self._apply([(ITEM_B, 1)], selected)
			self.assertIn(rule, resp.get("applied_pricing_rules") or [], f"selected={selected}: before redemption")
		frappe.get_doc(
			{"doctype": "One Time Customer Offer Usage", "customer": self.ctx.customer, "pricing_rule": rule}
		).insert(ignore_permissions=True)
		for selected in ([rule], []):
			resp = self._apply([(ITEM_B, 1)], selected)
			self.assertNotIn(rule, resp.get("applied_pricing_rules") or [], f"selected={selected}")
			line = next(i for i in resp["items"] if i["item_code"] == ITEM_B)
			self.assertEqual(flt(line.get("discount_percentage")), 0, f"selected={selected}")

	def test_invoice_keeps_free_row_without_rule(self):
		doc = self._invoice([self._row(ITEM_B, 1), self._row(ITEM_C, 1, "", free=True)])
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual([r.item_code for r in doc.get("items") if cint(r.is_free_item)], [ITEM_C])
		self.assertEqual(_source(self._paid(doc, ITEM_B)), FNF)

	def test_cleared_gift_stamp_keeps_price_rule(self):
		# A GWP stamp on an F&F line goes; the Price rule sharing the line stays attached.
		gwp = self._gift(promotion_type=gifts.PROMOTION_TYPE_GWP)
		price = self._price_rule(ITEM_B)
		row = self._row(ITEM_B, 1, f"{gwp},{price}", idx=1)
		row.update(
			{"discount_source": gifts.DISCOUNT_SOURCE_GWP, "discount_percentage": 10, "discount_amount": 8, "rate": 72}
		)
		applied = {gwp, price}
		suppressed = ff_compatibility.enforce_gift_policy(
			[row], {}, ff_config.resolve(), fnf_keys={1}, applied_rules=applied
		)
		self.assertEqual(suppressed, [gwp])
		self.assertEqual(gifts._item_rule_names(row), [price])
		self.assertEqual(applied, {price})
		self.assertEqual((flt(row.discount_amount), flt(row.rate)), (0, 80))

	def test_fallback_gift_stamp_detaches_only_the_unknown_rule(self):
		# The gift kind comes from discount_source only (its rule no longer resolves):
		# that rule goes, the Price rule stays.
		price = self._price_rule(ITEM_B)
		row = self._row(ITEM_B, 1, f"{PREFIX}DELETED-GIFT,{price}", idx=1)
		row.update(
			{"discount_source": gifts.DISCOUNT_SOURCE_GWP, "discount_percentage": 10, "discount_amount": 8, "rate": 72}
		)
		suppressed = ff_compatibility.enforce_gift_policy([row], {}, ff_config.resolve(), fnf_keys={1})
		self.assertEqual(suppressed, [f"{PREFIX}DELETED-GIFT"])
		self.assertEqual(gifts._item_rule_names(row), [price])
		self.assertEqual(flt(row.discount_amount), 0)

	def test_invoice_line_freed_from_gift_gets_fnf_on_rerun(self):
		# ITEM_B's only discount is the gift rule's (50 % beats F&F 30 %). ITEM_A gets F&F and
		# also triggers the gift, so the gift goes: ITEM_B loses the gift discount and the
		# re-run gives it F&F.
		gift = self._gift(triggers=[ITEM_A, ITEM_B])
		row_b = self._row(ITEM_B, 1, gift)
		row_b.update({"discount_percentage": 50, "discount_amount": 40, "rate": 40})
		doc = self._invoice([self._row(ITEM_A, 1, gift), row_b, self._row(ITEM_C, 1, gift, free=True)])
		apply_friends_and_family_on_invoice(doc)
		self.assertFalse([r for r in doc.get("items") if cint(r.is_free_item)])
		line_b = self._paid(doc, ITEM_B)
		self.assertEqual(_source(line_b), FNF)
		self.assertEqual(flt(line_b.discount_percentage), 30)
		self.assertNotIn(gift, gifts._item_rule_names(line_b))

	def test_clear_line_gift_stamp_detaches_only_named_rules(self):
		row = frappe._dict(
			{"pricing_rules": "GIFT-1,PRICE-1", "price_list_rate": 80, "rate": 72, "discount_amount": 8}
		)
		gifts._clear_line_gift_stamp(row, ["GIFT-1"])
		self.assertEqual(row.pricing_rules, "PRICE-1")
		gifts._clear_line_gift_stamp(row)
		self.assertEqual(row.pricing_rules, "")

	def test_removed_gift_keeps_other_rules_on_trigger_row(self):
		# ITEM_A is excluded from F&F and keeps a 10% Price rule; ITEM_B gets F&F, so
		# the gift (triggered by both) goes. ITEM_A loses only the gift tag.
		self._settings(excluded_items=[{"item_code": ITEM_A}])
		gift = self._gift(triggers=[ITEM_A, ITEM_B])
		price = self._price_rule(ITEM_A)
		row_a = self._row(ITEM_A, 1, f"{gift},{price}")
		row_a.update({"discount_percentage": 10, "discount_amount": 5, "rate": 45})
		doc = self._invoice([row_a, self._row(ITEM_B, 1, gift), self._row(ITEM_C, 1, gift, free=True)])
		apply_friends_and_family_on_invoice(doc)
		self.assertFalse([r for r in doc.get("items") if cint(r.is_free_item)])
		line_a = self._paid(doc, ITEM_A)
		self.assertEqual(gifts._item_rule_names(line_a), [price])
		self.assertEqual((flt(line_a.discount_percentage), flt(line_a.rate)), (10, 45))
		self.assertEqual(_source(self._paid(doc, ITEM_B)), FNF)

	def _group(self, name, parent="All Customer Groups"):
		if not frappe.db.exists("Customer Group", name):
			frappe.get_doc(
				{"doctype": "Customer Group", "customer_group_name": name, "parent_customer_group": parent, "is_group": 1}
			).insert(ignore_permissions=True)
		return name

	def test_customer_group_rule_matches_descendants(self):
		parent = self._group(f"{PREFIX}PARENT")
		child = self._group(f"{PREFIX}CHILD", parent)
		other = self._group(f"{PREFIX}OTHER")
		rule = self._bxgy(applicable_for="Customer Group", customer_group=parent)
		doc = self._invoice([self._row(ITEM_A, 2)])
		doc.customer_group = child
		self.assertIn(rule, ff_compatibility.blocking_rule_map(doc))
		doc.customer_group = other
		self.assertNotIn(rule, ff_compatibility.blocking_rule_map(doc))

	def _unclassified_invoice(self):
		rule = self._price_rule(ITEM_B)
		frappe.db.set_value("Pricing Rule", rule, "price_or_product_discount", "")
		frappe.clear_document_cache("Pricing Rule", rule)
		gifts.clear_rule_cache()
		self.assertEqual(ff_compatibility.classify_promotion(rule), "unclassified")
		row = self._row(ITEM_B, 1, rule)
		row.update({"discount_percentage": 10, "discount_amount": 8, "rate": 72})
		return self._invoice([row])

	def test_unclassified_rule_competes(self):
		self._settings(unclassified_promotion_policy=ff_compatibility.const.UNCLASSIFIED_POLICIES[0])
		doc = self._unclassified_invoice()
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(_source(self._paid(doc, ITEM_B)), FNF)
		self.assertEqual(flt(self._paid(doc, ITEM_B).discount_percentage), 30)

	def test_unclassified_rule_blocks_fnf_on_line(self):
		self._settings(unclassified_promotion_policy=ff_compatibility.POLICY_BLOCK_FNF)
		doc = self._unclassified_invoice()
		apply_friends_and_family_on_invoice(doc)
		line = self._paid(doc, ITEM_B)
		self.assertNotEqual(_source(line), FNF)
		self.assertEqual((flt(line.discount_percentage), flt(line.rate)), (10, 72))

	# ---- cart / invoice parity -------------------------------------------

	def _assert_parity(self, resp):
		rows = _pos_rows(resp)
		expected = Counter(_tuple(row, row["_cart_source"], row["_cart_unit_discount"]) for row in rows)
		doc = self._invoice([frappe._dict({k: v for k, v in row.items() if not k.startswith("_cart")}) for row in rows])
		apply_friends_and_family_on_invoice(doc)
		actual = Counter(_tuple(row, _source(row), row.get("discount_amount")) for row in doc.get("items"))
		self.assertEqual(actual, expected)

	def test_deselected_bxgy_blocks_cart_and_invoice(self):
		# Ruling 19: one rule source, independent of the offers the client selected.
		self._settings(allow_free_gift_with_ff=1)
		bxgy = self._bxgy()
		gift = self._gift()
		for selected in ([gift], []):
			with self.subTest(selected=selected):
				resp = self._apply([(ITEM_A, 2), (ITEM_B, 1)], selected)
				cart_a = next(i for i in resp["items"] if i["item_code"] == ITEM_A and not i.get("is_free_item"))
				cart_b = next(i for i in resp["items"] if i["item_code"] == ITEM_B and not i.get("is_free_item"))
				self.assertNotEqual(_source(cart_a), FNF)
				self.assertEqual(_source(cart_b), FNF)
				if selected:
					self.assertNotIn(bxgy, resp.get("applied_pricing_rules") or [])
				self._assert_parity(resp)

	def test_bxgy_source_respects_pos_profile_warehouse(self):
		other = frappe.db.get_value(
			"Warehouse", {"company": self.ctx.company, "is_group": 0, "name": ["!=", self.ctx.warehouse]}, "name"
		)
		if not other:
			self.skipTest("needs a second warehouse")
		self._bxgy(warehouse=other)
		doc = self._invoice([self._row(ITEM_A, 2), self._row(ITEM_B, 1)])
		self.assertEqual(ff_compatibility.blocking_rule_map(doc), {})
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(_source(self._paid(doc, ITEM_A)), FNF)

	def test_cart_invoice_parity(self):
		for allow, promotion_type in ((0, None), (1, None), (0, gifts.PROMOTION_TYPE_GWP), (1, gifts.PROMOTION_TYPE_GWP)):
			with self.subTest(allow_free_gift_with_ff=allow, promotion_type=promotion_type):
				self._settings(allow_free_gift_with_ff=allow)
				bxgy = self._bxgy()
				gift = self._gift(promotion_type=promotion_type)
				# Same-item GWP: the POS carves the free unit off the scanned qty, so scan 2.
				qty_b = 2 if promotion_type else 1
				resp = self._apply([(ITEM_A, 2), (ITEM_B, qty_b)], [bxgy, gift])
				# The cart itself: BXGY free row always, the gift only when allowed. A GWP rule
				# whose matching lines are one SKU gives that SKU free (same-item GWP), not free_item.
				free_codes = sorted(f["item_code"] for f in resp.get("free_items") or [])
				gift_code = ITEM_B if promotion_type else ITEM_C
				self.assertEqual(free_codes, sorted([ITEM_A, gift_code] if allow else [ITEM_A]))
				self._assert_parity(resp)
