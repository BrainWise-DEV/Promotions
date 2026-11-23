# Copyright (c) 2026, BrainWise and contributors
"""K1 / D-R4-05: every POS Coupon is a line discount, before tax, for every customer;
how it combines with F&F on one cart is a Friends and Family Settings choice.

The F&F comparison used to send a fixed-amount or capped-% coupon back as an order
discount, so an F&F customer got it as a Grand Total header after tax (100 - 40
promotion, + 9 VAT, - 40 coupon = 29) while a walk-in got it on the line (100 - 40 - 40
= 20, + 3 VAT = 23). Now both get 23. Against F&F, ``coupon_combination`` decides:
the whole cart (default), per line, or per line with the coupon fully used. The
invoice re-splits the coupon itself and refuses any other split (review I-2).

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute pos_next.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_coupon_line_level_fnf'}"
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.api.offers import calculate_coupon_discount, validate_coupon
from posnext_promotions.overrides.scoped_coupon import validate_scoped_coupon_lines
from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_paid_freeze import (
	PaidInvoiceFixture,
)
from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_config, ff_constants, ff_money
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY as FNF,
	apply_friends_and_family_on_invoice,
)
from posnext_promotions.promotions.friends_and_family_caps import FIELD_DISCOUNT_SOURCE
from posnext_promotions.promotions.friends_and_family_testing import set_coupon_combination
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	ITEM_C,
	_cart_payload,
	_line,
	_make_pos_coupon,
	_make_rule,
	_resolve_customer_group,
	_resolve_territory,
)

PREFIX = "_PNXT_K1_"
PLAIN = f"{PREFIX}PLAIN_CUSTOMER"
VAT = 15
SETTINGS = "Friends and Family Settings"
WHOLE, PER_LINE, FULL = ff_constants.COUPON_COMBINATIONS


def _exclusive_vat(tax_amount=0.0):
	return [{"charge_type": "On Net Total", "rate": VAT, "included_in_print_rate": 0, "tax_amount": tax_amount}]


def _money(value):
	return flt(value, 2)


class TestCouponLineLevelFnF(PaidInvoiceFixture, FrappeTestCase):
	def setUp(self):
		super().setUp()
		if not frappe.db.exists("Customer", PLAIN):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": PLAIN,
					"customer_group": _resolve_customer_group(),
					"territory": _resolve_territory(),
					"customer_type": "Individual",
				}
			).insert(ignore_permissions=True)

	def tearDown(self):
		super().tearDown()
		set_coupon_combination(None)  # the rollback took the row; drop the cached Settings too

	# ---- helpers ------------------------------------------------------------

	def _settings(self, **values):
		"""The fixture's save, keeping the mode the test chose (a save drops it)."""
		mode = frappe.db.get_value("Singles", {"doctype": SETTINGS, "field": "coupon_combination"}, "value", order_by=None)
		doc = super()._settings(**values)
		if mode:
			set_coupon_combination(mode)
		return doc

	def _coupon(self, name, **fields):
		code = f"{PREFIX}{name}"
		_make_pos_coupon(code, **fields)
		return code

	def _promoted(self, item_code, price, pct, rule, qty=1):
		"""A cart line a promotion already discounted, as the POS sends it (line totals)."""
		line = _line(self.ctx, item_code, qty=qty, price_list_rate=price)
		off = flt(price * pct / 100, 2)
		line.update(
			{"discount_percentage": pct, "discount_amount": off * qty, "rate": price - off, "pricing_rules": rule}
		)
		return line

	def _rule(self, name, item_code, pct):
		return _make_rule(
			f"{PREFIX}{name}",
			apply_on="Item Code",
			items=[{"item_code": item_code}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=pct,
		)

	def _fnf(self, code, lines, taxes=None):
		"""What CouponDialog.applyCouponAgainstFriendsAndFamily asks for an F&F customer."""
		data = _cart_payload(self.ctx, lines)
		if taxes is not None:
			data["taxes"] = taxes
		return calculate_coupon_discount(code, json.dumps(data), customer=self.ctx.customer, company=self.ctx.company)

	def _walk_in(self, code, lines, taxes=None, customer=PLAIN):
		"""What the POS asks for a walk-in (and every line re-check): validate_coupon with items."""
		return validate_coupon(
			code,
			customer=customer,
			company=self.ctx.company,
			items=json.dumps(lines),
			pos_profile=self.ctx.pos_profile,
			taxes=json.dumps(taxes) if taxes is not None else None,
		)

	@staticmethod
	def _priced(updates):
		"""The money of each line update, by line: what the cart applies."""
		return {
			u["line_key"]: (
				flt(u["rate"], 6),
				flt(u["discount_amount"], 6),
				flt(u["discount_percentage"], 6),
				flt(u["coupon_only_discount"], 6),
				bool(u.get("stacked")),
			)
			for u in updates
		}

	@staticmethod
	def _shares(response):
		return {u["line_key"]: _money(u["coupon_only_discount"]) for u in response["line_updates"]}

	def _grand_total(self, item_code, update):
		"""ERPNext's own totals for a one-line Sales Invoice priced by ``update``, at 15% VAT."""
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.ctx.company,
				"currency": self.ctx.currency,
				"conversion_rate": 1,
				"customer": self.ctx.customer,
				"items": [
					{
						"item_code": item_code,
						"qty": 1,
						"uom": "Nos",
						"conversion_factor": 1,
						"price_list_rate": 100,
						"rate": update["rate"],
						"discount_amount": update["discount_amount"],
					}
				],
				"taxes": [{"charge_type": "On Net Total", "rate": VAT, "description": "VAT"}],
			}
		)
		doc.calculate_taxes_and_totals()
		return flt(doc.grand_total)

	# ---- the setting ---------------------------------------------------------------

	def test_coupon_combination_setting(self):
		import os

		from posnext_promotions.posnext_promotions.doctype import friends_and_family_settings as pkg

		path = os.path.join(os.path.dirname(pkg.__file__), "friends_and_family_settings.json")
		with open(path) as handle:
			[field] = [f for f in json.load(handle)["fields"] if f["fieldname"] == "coupon_combination"]
		self.assertEqual(tuple(field["options"].split("\n")), ff_constants.COUPON_COMBINATIONS)
		self.assertEqual(field["default"], WHOLE)
		# Unset (before migrate, or blank) reads as the default; a saved choice is read back.
		set_coupon_combination(None)
		self.assertEqual(ff_config.resolve().coupon_combination.value, WHOLE)
		for mode in ff_constants.COUPON_COMBINATIONS:
			set_coupon_combination(mode)
			self.assertEqual(ff_config.resolve().coupon_combination.value, mode)

	# ---- D-R4-05 --------------------------------------------------------------

	def test_d_r4_05_fnf_customer_gets_the_walk_in_23(self):
		# 100, promotion C40 (40%), coupon O40: Amount 40, Add on top, Grand Total, 15% VAT.
		rule = self._rule("C40", ITEM_A, 40)
		code = self._coupon(
			"O40", discount_type="Amount", discount_amount=40, apply_on="Grand Total", promotion_conflict="Add on top"
		)
		lines = [self._promoted(ITEM_A, 100, 40, rule)]
		taxes = _exclusive_vat(9)
		walk_in = self._walk_in(code, lines, taxes)
		self.assertTrue(walk_in["valid"], walk_in.get("message"))
		for mode in ff_constants.COUPON_COMBINATIONS:
			with self.subTest(mode=mode):
				set_coupon_combination(mode)
				fnf = self._fnf(code, lines, taxes)
				self.assertTrue(fnf["valid"], fnf.get("message"))
				self.assertEqual(fnf["granularity"], bd.GRANULARITY_LINE)
				[update] = fnf["line_updates"]
				self.assertTrue(update["stacked"])
				self.assertAlmostEqual(update["coupon_only_discount"], 40)
				self.assertEqual(self._grand_total(ITEM_A, update), 23)
				self.assertEqual(self._priced(walk_in["line_updates"]), self._priced(fnf["line_updates"]))
				recheck = self._walk_in(code, lines, taxes, customer=self.ctx.customer)
				self.assertEqual(self._priced(recheck["line_updates"]), self._priced(fnf["line_updates"]))

		# The invoice keeps the line: promotion 40 + coupon 40 beats F&F 30, and the
		# coupon's own share (80 less the 40 promotion the server finds) is its 40.
		for paid in (False, True):
			with self.subTest(paid=paid):
				row = self._row(ITEM_A, rules=rule)
				row.update(
					{
						"price_list_rate": 100,
						"rate": 20,
						"discount_percentage": 0,
						"discount_amount": 80,
						"posnext_coupon_code": code,
						"posnext_coupon_discount": 40,
					}
				)
				doc = self._sale([row], code, paid=paid)
				self._validate_invoice(doc)
				self.assertEqual(self._prices(doc), [(20, 0, 80, 100)])
				self.assertEqual(row.posnext_coupon_code, code)
				self.assertNotEqual(row.get(FIELD_DISCOUNT_SOURCE), FNF)

	# ---- exact totals -----------------------------------------------------------

	def test_fixed_amount_split_over_mixed_qty_and_tax_sums_to_the_cent(self):
		# 150 over 3 x 50, 1 x 80 and 7 x 20 (rooms 150 : 80 : 140): every share beats
		# F&F 30% on its line (60.81 > 45, 32.43 > 24, 56.76 > 42), so every mode agrees.
		code = self._coupon("A150", discount_type="Amount", discount_amount=150, apply_on="Grand Total")
		lines = [_line(self.ctx, ITEM_A, qty=3), _line(self.ctx, ITEM_B, qty=1), _line(self.ctx, ITEM_C, qty=7)]
		taxes = [
			{"charge_type": "On Net Total", "rate": 5, "included_in_print_rate": 1, "tax_amount": 17.62},
			*_exclusive_vat(55.5),
		]
		walk_in = self._walk_in(code, lines, taxes)
		for mode in ff_constants.COUPON_COMBINATIONS:
			with self.subTest(mode=mode):
				set_coupon_combination(mode)
				fnf = self._fnf(code, lines, taxes)
				self.assertTrue(fnf["valid"], fnf.get("message"))
				self.assertEqual(self._shares(fnf), {0: 60.81, 1: 32.43, 2: 56.76})
				self.assertAlmostEqual(sum(u["coupon_only_discount"] for u in fnf["line_updates"]), 150, places=6)
				self.assertAlmostEqual(fnf["discount"], 150, places=6)  # the coupon's money, pre-tax (M-2)
				self.assertEqual(self._priced(walk_in["line_updates"]), self._priced(fnf["line_updates"]))

	def test_capped_percentage_coupon_in_each_mode(self):
		# 40% (max 30) over 50 + 80: 20 + 32 = 52, capped to 30 -> 11.54 + 18.46. ITEM_B is
		# outside F&F; on ITEM_A F&F 30% (15) beats the coupon's 11.54.
		code = self._coupon("P40CAP30", discount_percentage=40, max_amount=30)
		lines = [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)]
		self.assertEqual(self._shares(self._walk_in(code, lines)), {0: 11.54, 1: 18.46})
		self._settings(excluded_items=[{"item_code": ITEM_B}])
		expected = {
			PER_LINE: ({1: 18.46}, False),  # A keeps F&F, the 11.54 is lost
			FULL: ({1: 30.0}, False),  # the 11.54 moves to B (room 32 at 40%)
			WHOLE: ({0: 11.54, 1: 18.46}, True),  # coupon 30 beats F&F 15 on the cart
		}
		for mode, (shares, whole_cart) in expected.items():
			with self.subTest(mode=mode):
				set_coupon_combination(mode)
				fnf = self._fnf(code, lines)
				self.assertTrue(fnf["valid"], fnf.get("message"))
				self.assertEqual(self._shares(fnf), shares)
				self.assertAlmostEqual(fnf["discount"], sum(shares.values()), places=2)
				self.assertEqual(fnf["replaces_friends_and_family_on_cart"], whole_cart)

	def test_minimum_is_judged_on_the_eligible_cart_before_fnf_splits_it(self):
		# Review I-1: Amount 30, minimum 100, over 50 + 80 (ITEM_B outside F&F). The walk-in
		# gets 30; F&F on ITEM_A must not drop the coupon for "items worth 80".
		code = self._coupon("A30MIN", discount_type="Amount", discount_amount=30, min_amount=100)
		lines = [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)]
		self.assertEqual(self._shares(self._walk_in(code, lines)), {0: 11.54, 1: 18.46})
		self._settings(excluded_items=[{"item_code": ITEM_B}])
		for mode, shares in ((PER_LINE, {1: 18.46}), (FULL, {1: 30.0}), (WHOLE, {0: 11.54, 1: 18.46})):
			with self.subTest(mode=mode):
				set_coupon_combination(mode)
				fnf = self._fnf(code, lines)
				self.assertTrue(fnf["valid"], fnf.get("message"))
				self.assertEqual(self._shares(fnf), shares)

	def test_whole_cart_fnf_wins_a_tie_and_takes_the_cart(self):
		# Amount 30 over 50 + 80, both under F&F 30% (15 + 24 = 39): F&F keeps the cart.
		# Amount 39 ties at 39: F&F still wins. Amount 40 takes the whole cart.
		lines = [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)]
		set_coupon_combination(WHOLE)
		for amount, wins in ((30, False), (39, False), (40, True)):
			with self.subTest(amount=amount):
				code = self._coupon(f"WC{amount}", discount_type="Amount", discount_amount=amount)
				fnf = self._fnf(code, lines)
				self.assertEqual(bool(fnf["valid"]), wins, fnf.get("message"))
				if wins:
					self.assertEqual(sorted(self._shares(fnf)), [0, 1])
					self.assertTrue(fnf["replaces_friends_and_family_on_cart"])
				else:
					self.assertEqual(fnf["reason"], "better_discount_exists")
					self.assertEqual(fnf["best_discount"]["order"]["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)

	def test_grand_total_fixed_or_capped_coupon_counts_its_tax_saving_like_fnf(self):
		# 100 at 15% VAT, F&F 30% saves 34.50 with its tax. A coupon line discount lowers
		# the tax the same way: 31 saves 35.65 and wins; 30 ties (34.50) and F&F keeps it.
		lines = [_line(self.ctx, ITEM_A, price_list_rate=100)]
		taxes = _exclusive_vat()
		for name, fields, wins in (
			("GTA31", {"discount_type": "Amount", "discount_amount": 31}, True),
			("GTP40C31", {"discount_percentage": 40, "max_amount": 31}, True),
			("GTA30", {"discount_type": "Amount", "discount_amount": 30}, False),
		):
			with self.subTest(coupon=name):
				code = self._coupon(name, apply_on="Grand Total", **fields)
				fnf = self._fnf(code, lines, taxes)
				self.assertEqual(bool(fnf["valid"]), wins, fnf.get("message"))
				if wins:
					self.assertAlmostEqual(fnf["line_updates"][0]["coupon_only_discount"], 31)
					# M-2: the coupon's money is its line money, within max_amount; the tax
					# it also saves is not added.
					self.assertAlmostEqual(fnf["discount"], 31)
					self.assertAlmostEqual(fnf["coupon_value"], 31)
				else:
					self.assertEqual(fnf["reason"], "better_discount_exists")

	def test_pos_max_discount_still_limits_or_refuses_the_stack(self):
		# Promotion 40% on 100, Add on top Amount 40: Max Discount 50% leaves 10 for the
		# coupon (50 on the line, beats F&F 30); at 40% the line is already at the limit.
		rule = self._rule("C40MAX", ITEM_A, 40)
		code = self._coupon("O40MAX", discount_type="Amount", discount_amount=40, promotion_conflict="Add on top")
		lines = [self._promoted(ITEM_A, 100, 40, rule)]
		with patch("posnext_promotions.api.coupon_engine.max_discount_allowed", return_value=50):
			fnf = self._fnf(code, lines)
			walk_in = self._walk_in(code, lines)
		[update] = fnf["line_updates"]
		self.assertAlmostEqual(update["coupon_only_discount"], 10)
		self.assertAlmostEqual(update["discount_amount"], 50)
		self.assertEqual(self._priced(walk_in["line_updates"]), self._priced(fnf["line_updates"]))
		with patch("posnext_promotions.api.coupon_engine.max_discount_allowed", return_value=40):
			refused = self._fnf(code, lines)
		self.assertFalse(refused["valid"])
		self.assertFalse(refused["line_updates"])

	# ---- the refusal names the coupon's value -----------------------------------

	def test_refused_fixed_coupon_is_named_by_its_value_not_one_lines_share(self):
		# Amount 50 over 2 x 50 and 1 x 80 (27.78 + 22.22) loses to F&F 30% (30 + 24):
		# the message names the coupon's 50, not the 27.78 it would give the first line.
		code = self._coupon("A50MSG", discount_type="Amount", discount_amount=50)
		lines = [_line(self.ctx, ITEM_A, qty=2), _line(self.ctx, ITEM_B)]
		self._assert_named_by(code, lines, ff_money.fmt(50, self.ctx.currency), ff_money.fmt(27.78, self.ctx.currency))

	def test_refused_capped_percentage_coupon_is_named_by_its_capped_money(self):
		# M-1: 40% capped at 30 over 50 + 80 (11.54 + 18.46) loses to F&F (15 + 24). "40%"
		# would read as more than F&F's 30%: the message names the 30 it gives.
		code = self._coupon("P40C30MSG", discount_percentage=40, max_amount=30)
		lines = [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)]
		self._assert_named_by(code, lines, ff_money.fmt(30, self.ctx.currency), "(40%)")

	def _assert_named_by(self, code, lines, shown, hidden):
		for mode in (WHOLE, PER_LINE):
			set_coupon_combination(mode)
			for response in (self._fnf(code, lines), self._walk_in(code, lines, customer=self.ctx.customer)):
				with self.subTest(mode=mode):
					self.assertFalse(response["valid"])
					self.assertEqual(response["reason"], "better_discount_exists")
					for key in ("message", "message_en", "message_ar"):
						self.assertIn(shown, response[key], key)
						self.assertNotIn(hidden, response[key], key)

	# ---- the invoice holds the server's split (review I-2) -------------------------

	def _sale(self, rows, code, paid=True):
		return self._invoice(rows, paid=paid, coupon_code=code)

	def _validate_invoice(self, doc):
		"""The Sales Invoice validate hooks, in hooks.py order."""
		validate_scoped_coupon_lines(doc)
		apply_friends_and_family_on_invoice(doc)

	def _coupon_row(self, item_code, share):
		row = self._row(item_code)
		price = flt(row.price_list_rate)
		row.update(
			{
				"rate": price - share,
				"discount_percentage": 0,
				"discount_amount": share,
				"posnext_coupon_code": self._code,
				"posnext_coupon_discount": share,
			}
		)
		return row

	def _split_cart(self, mode):
		"""Amount 30 over 50 + 80, F&F 30% on ITEM_A only (15 vs the coupon's 11.54)."""
		self._code = self._coupon(f"A30S{ff_constants.COUPON_COMBINATIONS.index(mode)}", discount_type="Amount", discount_amount=30)
		self._settings(excluded_items=[{"item_code": ITEM_B}])
		set_coupon_combination(mode)
		return self._code

	def _refused(self, rows, text, paid_only=False):
		for paid in (True,) if paid_only else (False, True):
			with self.subTest(paid=paid), self.assertRaises(frappe.ValidationError) as caught:
				self._validate_invoice(self._sale([r.copy() for r in rows], self._code, paid=paid))
			self.assertIn(text, str(caught.exception))

	def test_per_line_invoice_refuses_the_unused_share_moved_onto_another_line(self):
		# The 45 exploit: F&F 15 on ITEM_A and the whole 30 on ITEM_B. The server's split
		# is 18.46 on ITEM_B; that sale is accepted, paid and unpaid, as the cart priced it.
		code = self._split_cart(PER_LINE)
		for paid in (False, True):
			with self.subTest(paid=paid):
				doc = self._sale([self._row(ITEM_A, pct=30, marked=True), self._coupon_row(ITEM_B, 18.46)], code, paid=paid)
				self._validate_invoice(doc)
				self.assertEqual(self._prices(doc), [(35, 30, 15, 50), (61.54, 0, 18.46, 80)])
		forged = [self._row(ITEM_A, pct=30, marked=True), self._coupon_row(ITEM_B, 30)]
		self._refused(forged, "more than its share", paid_only=True)
		# Unpaid, "Allow and log" (the fixture's): re-priced down to the share (R1-M2).
		doc = self._sale([r.copy() for r in forged], code, paid=False)
		with patch("posnext_promotions.overrides.coupon_split.frappe.log_error") as log_error:
			self._validate_invoice(doc)
		self.assertEqual(self._prices(doc)[1], (61.54, 0, 18.46, 80))
		self.assertTrue(log_error.called)  # the correction is logged (R2-M1)
		# The F&F line tagged with the coupon too: not one of the coupon's lines. Paid, refused;
		# unpaid, re-priced as before: the coupon comes off ITEM_A and F&F takes it.
		rows = [self._coupon_row(ITEM_A, 15), self._coupon_row(ITEM_B, 15)]
		self._refused(rows, "does not give the best discount", paid_only=True)
		doc = self._sale([r.copy() for r in rows], code, paid=False)
		self._validate_invoice(doc)
		self.assertEqual(self._prices(doc), [(35, 30, 15, 50), (65, 0, 15, 80)])
		self.assertEqual([r.posnext_coupon_code for r in doc["items"]], [None, code])

	def test_fully_used_invoice_refuses_fnf_plus_the_full_coupon_where_the_coupon_wins_both(self):
		# Amount 60 over 50 + 80 (23.08 / 36.92) beats F&F (15 / 24) on both lines. A client
		# keeping F&F on ITEM_A and putting the whole 60 on ITEM_B is refused.
		code = self._coupon("A60FULL", discount_type="Amount", discount_amount=60)
		self._code = code
		set_coupon_combination(FULL)
		fnf = self._fnf(code, [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)])
		self.assertEqual(self._shares(fnf), {0: 23.08, 1: 36.92})
		doc = self._sale([self._coupon_row(ITEM_A, 23.08), self._coupon_row(ITEM_B, 36.92)], code)
		self._validate_invoice(doc)
		self._refused(
			[self._row(ITEM_A, pct=30, marked=True), self._coupon_row(ITEM_B, 60)], "more than its share", paid_only=True
		)

	def test_whole_cart_invoice_never_mixes_fnf_and_the_coupon(self):
		# The coupon (30) beats F&F (15) on the cart, so it takes both lines (11.54 / 18.46).
		# That invoice is kept as sold, and an unpaid one is not given F&F on ITEM_A again.
		code = self._split_cart(WHOLE)
		for paid in (False, True):
			with self.subTest(paid=paid):
				doc = self._sale([self._coupon_row(ITEM_A, 11.54), self._coupon_row(ITEM_B, 18.46)], code, paid=paid)
				self._validate_invoice(doc)
				self.assertEqual(self._prices(doc), [(38.46, 0, 11.54, 50), (61.54, 0, 18.46, 80)])
				self.assertEqual([r.posnext_coupon_code for r in doc["items"]], [code, code])
		# The per-line split (F&F on ITEM_A next to the coupon): paid, refused; unpaid, F&F
		# comes off ITEM_A (the server never mixes them), the coupon stays on ITEM_B.
		rows = [self._row(ITEM_A, pct=30, marked=True), self._coupon_row(ITEM_B, 18.46)]
		self._refused(rows, "cannot be combined", paid_only=True)
		doc = self._sale([r.copy() for r in rows], code, paid=False)
		self._validate_invoice(doc)
		self.assertEqual(self._prices(doc)[1], (61.54, 0, 18.46, 80))
		self.assertEqual(flt(doc["items"][0].rate), 50)
		self.assertNotEqual(doc["items"][0].get(FIELD_DISCOUNT_SOURCE), FNF)

	def test_unpaid_row_above_its_share_follows_the_cap_breach_setting(self):
		# R1-M2: "Allow and log" re-prices the unpaid draft to the server's share; "Block
		# submit" refuses it, as it refuses an F&F cap breach.
		code = self._split_cart(PER_LINE)
		for policy, refused in (("Allow and log", False), ("Block submit", True)):
			with self.subTest(policy=policy):
				self._settings(excluded_items=[{"item_code": ITEM_B}], on_cap_breach_at_submit=policy)
				doc = self._sale([self._row(ITEM_A, pct=30, marked=True), self._coupon_row(ITEM_B, 25)], code, paid=False)
				if refused:
					with self.assertRaises(frappe.ValidationError) as caught:
						self._validate_invoice(doc)
					self.assertIn("more than its share", str(caught.exception))
				else:
					REPRICED_LOG_TITLE = "POS Coupon line re-priced to its share"
					with patch("posnext_promotions.overrides.coupon_split.frappe.log_error") as log_error:
						self._validate_invoice(doc)
					self.assertEqual(self._prices(doc)[1], (61.54, 0, 18.46, 80))
					# R2-M1: "Allow and log" logs what it corrected, before re-pricing.
					[logged] = [c.kwargs for c in log_error.call_args_list if c.kwargs.get("title") == REPRICED_LOG_TITLE]
					details = json.loads(logged["message"].split("\n", 1)[1])
					self.assertEqual(
						{k: details[k] for k in ("invoice", "row", "item", "coupon", "client_share", "server_share")},
						{
							"invoice": doc.name,
							"row": 2,
							"item": ITEM_B,
							"coupon": code,
							"client_share": 25.0,
							"server_share": 18.46,
						},
					)

	def test_forged_shift_at_qty_100_is_judged_on_the_line(self):
		# R1-M1: ITEM_C x 100 at 0.80 (outside F&F) and ITEM_A 50 (F&F 15). Amount 30 gives
		# ITEM_C 18.46 (0.1846 a unit). 0.2346 a unit is only 0.05 more per unit but 23.46 on
		# the line, 5 more: refused. The cart's own per-unit cents (0.18 x 100) pass.
		code = self._coupon("A30Q100", discount_type="Amount", discount_amount=30)
		self._code = code
		self._settings(excluded_items=[{"item_code": ITEM_C}])
		set_coupon_combination(PER_LINE)
		line_c = _line(self.ctx, ITEM_C, qty=100, price_list_rate=0.8)
		fnf = self._fnf(code, [_line(self.ctx, ITEM_A), line_c])
		self.assertEqual(self._shares(fnf), {1: 18.46})

		def row_c(per_unit):
			row = self._row(ITEM_C, qty=100)
			row.update(
				{
					"price_list_rate": 0.8,
					"rate": 0.8 - per_unit,
					"discount_percentage": 0,
					"discount_amount": per_unit,
					"posnext_coupon_code": code,
					"posnext_coupon_discount": per_unit,
				}
			)
			return row

		doc = self._sale([self._row(ITEM_A, pct=30, marked=True), row_c(0.18)], code)
		self._validate_invoice(doc)
		self._refused([self._row(ITEM_A, pct=30, marked=True), row_c(0.2346)], "more than its share", paid_only=True)

	def test_offline_sync_keeps_a_forged_split_and_logs_it(self):
		# The customer has left: an offline sync keeps the sale and logs each breach.
		from posnext_promotions.overrides.scoped_coupon import OFFLINE_KEPT_LOG_TITLE

		code = self._split_cart(PER_LINE)
		doc = self._invoice(
			[self._row(ITEM_A, pct=30, marked=True), self._coupon_row(ITEM_B, 30)],
			coupon_code=code,
			sync_flag=True,
			offline=True,
		)
		with patch("posnext_promotions.overrides.scoped_coupon.frappe.log_error") as log_error:
			validate_scoped_coupon_lines(doc)
		titles = [call.kwargs.get("title") for call in log_error.call_args_list]
		self.assertIn(OFFLINE_KEPT_LOG_TITLE, titles)
		self.assertTrue(any("more than its share" in call.kwargs.get("message", "") for call in log_error.call_args_list))


class TestCouponLineLevelFnFSaleAndReturn(PaidInvoiceFixture, FrappeTestCase):
	"""A real sale through pos_next's checkout, then ERPNext's own return of it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from posnext_promotions.test_promotions import _pos_invoice_pipeline

		submit_invoice, update_invoice = _pos_invoice_pipeline()
		if update_invoice is None:
			raise __import__("unittest").SkipTest("pos_next invoice APIs are not installed")
		cls.submit_invoice, cls.update_invoice = staticmethod(submit_invoice), staticmethod(update_invoice)

	def tearDown(self):
		super().tearDown()
		set_coupon_combination(None)  # the rollback took the row; drop the cached Settings too

	def test_whole_cart_sale_and_its_return_keep_the_coupon_lines(self):
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		from posnext_promotions.overrides.coupon_share import FIELD_SHARE

		code = f"{PREFIX}A30RET"
		_make_pos_coupon(code, discount_type="Amount", discount_amount=30)
		self._settings(excluded_items=[{"item_code": ITEM_B}])
		set_coupon_combination(WHOLE)
		lines = [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)]
		payload = _cart_payload(self.ctx, lines)
		priced = calculate_coupon_discount(code, json.dumps(payload), customer=self.ctx.customer, company=self.ctx.company)
		self.assertTrue(priced["valid"], priced.get("message"))
		# The cart applies the server's lines, as posCart.applyCouponLineUpdates does.
		for update in priced["line_updates"]:
			line = payload["items"][update["line_key"]]
			share = flt(update["coupon_only_discount"], 2)
			line.update(
				{
					"rate": flt(line["price_list_rate"]) - share,
					"discount_amount": share,
					"posnext_coupon_code": code,
					"posnext_coupon_discount": share,
				}
			)
		payload["coupon_code"] = code
		expected = {line["item_code"]: flt(line["rate"], 2) for line in payload["items"]}
		self.assertEqual(expected, {ITEM_A: 38.46, ITEM_B: 61.54})

		draft = self.update_invoice(json.dumps(payload))
		self.assertEqual({r["item_code"]: flt(r["rate"], 2) for r in draft["items"]}, expected)
		draft["payments"] = [{"mode_of_payment": self.ctx.mode_of_payment, "amount": flt(draft["grand_total"])}]
		self.submit_invoice(invoice=json.dumps(draft, default=str), data=json.dumps({"change_amount": 0, "write_off_amount": 0}))
		sale = frappe.get_doc("Sales Invoice", draft["name"])
		self.assertEqual(sale.docstatus, 1)
		self.assertEqual({r.item_code: flt(r.rate, 2) for r in sale.items}, expected)
		self.assertTrue(all(r.posnext_coupon_code for r in sale.items))
		self.assertFalse(any(r.get(FIELD_DISCOUNT_SOURCE) == FNF for r in sale.items))

		ret = make_sales_return(sale.name)
		ret.insert(ignore_permissions=True)
		self.assertEqual({r.item_code: flt(r.rate, 2) for r in ret.items}, expected)
		# The line discounts come over with the rates; the coupon tag is no_copy by design,
		# and the coupon's share is the original row's (coupon_share._copy_return_shares).
		self.assertEqual({r.item_code: flt(r.discount_amount, 2) for r in ret.items}, {ITEM_A: 11.54, ITEM_B: 18.46})
		if frappe.get_meta("Sales Invoice Item").has_field(FIELD_SHARE):
			self.assertEqual(
				{r.item_code: flt(r.get(FIELD_SHARE), 2) for r in ret.items},
				{r.item_code: flt(r.get(FIELD_SHARE), 2) for r in sale.items},
			)
