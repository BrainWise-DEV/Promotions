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
from posnext_promotions.promotions import ff_config, ff_constants, ff_money, ff_paid
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
	def _logged(log_error, title):
		"""The keyword arguments of each patched ``frappe.log_error`` call under ``title``."""
		return [c.kwargs for c in log_error.call_args_list if c.kwargs.get("title") == title]

	@staticmethod
	def _raw(response):
		"""The coupon's own money of each line update, as the server sends it (not rounded)."""
		return {u["line_key"]: u["coupon_only_discount"] for u in response["line_updates"]}

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
				# D-R5-06: 56.76 over 7 units (8.1086 a unit) is no row's discount; the row holds
				# 8.10 x 7 = 56.70, and the 6 cents cut there and on ITEM_A go whole to ITEM_B.
				self.assertEqual(self._shares(fnf), {0: 60.81, 1: 32.49, 2: 56.70})
				self._assert_whole_cents(fnf["line_updates"], lines, 150)
				self.assertAlmostEqual(fnf["discount"], 150, places=6)  # the coupon's money, pre-tax (M-2)
				self.assertEqual(self._priced(walk_in["line_updates"]), self._priced(fnf["line_updates"]))

	def _assert_whole_cents(self, updates, lines, total):
		"""Each share is whole cents a unit (an invoice row holds it as it is) and they sum to ``total``."""
		for update in updates:
			qty = flt(lines[update["line_key"]]["qty"])
			cents = update["coupon_only_discount"] / qty * 100
			self.assertAlmostEqual(cents, round(cents), places=6, msg=update)
		self.assertEqual(flt(sum(u["coupon_only_discount"] for u in updates), 6), total)

	def test_fixed_amount_split_puts_the_lost_cent_on_one_line(self):
		# D-R5-06: Amount 40 over 100 + 80 is 22.2222 + 17.7778. The cart cuts each line to
		# whole cents a unit, so it sold 22.22 + 17.77 = 39.99. The server's shares are now
		# whole cents and the cent goes to the line that lost the most to rounding: 17.78.
		code = self._coupon("A40CENT", discount_type="Amount", discount_amount=40)
		lines = [_line(self.ctx, ITEM_A, price_list_rate=100), _line(self.ctx, ITEM_B, price_list_rate=80)]
		# The run-5 cart: ITEM_B outside F&F, so the coupon's 40 beats F&F's 30 on the cart.
		self._settings(excluded_items=[{"item_code": ITEM_B}])
		set_coupon_combination(WHOLE)
		for who, response in (("walk-in", self._walk_in(code, lines)), ("F&F", self._fnf(code, lines))):
			with self.subTest(customer=who):
				self.assertTrue(response["valid"], response.get("message"))
				self.assertEqual(self._raw(response), {0: 22.22, 1: 17.78})
				self._assert_whole_cents(response["line_updates"], lines, 40)
		# 3 x 50 + 80: 26.0870 + 13.9130. ITEM_A loses more (26.07) but 2 cents do not split
		# over 3 units, so ITEM_B takes them: 13.93.
		lines = [
			_line(self.ctx, ITEM_A, qty=3, price_list_rate=50),
			_line(self.ctx, ITEM_B, price_list_rate=80),
		]
		response = self._walk_in(code, lines)
		self.assertEqual(self._raw(response), {0: 26.07, 1: 13.93})
		self._assert_whole_cents(response["line_updates"], lines, 40)

	def test_capped_percentage_cent_stays_within_the_coupon_percentage(self):
		# 40% (max 40) over 3 x 33.34: 13.336 each is 40.008, capped to 13.3333 each. The POS
		# clamps a capped-% line to 40% of it (13.336) before cutting to cents, so no line can
		# hold the lost cent: 13.33 x 3 on both sides, never 13.34 on one line.
		code = self._coupon("P40C40CENT", discount_percentage=40, max_amount=40)
		lines = [_line(self.ctx, item, price_list_rate=33.34) for item in (ITEM_A, ITEM_B, ITEM_C)]
		response = self._walk_in(code, lines)
		self.assertEqual(self._raw(response), {0: 13.33, 1: 13.33, 2: 13.33})

	def test_header_coupon_value_keeps_the_coupon_whole(self):
		# A header coupon (older offline queue, desk) is valued on the unrounded split: Amount
		# 10 over 3 x 50 + 3 x 50 is worth 10, though its line shares can only be 5.01 + 4.98.
		from posnext_promotions.promotions.best_discount_coupon import coupon_value_on

		code = self._coupon("A10HEAD", discount_type="Amount", discount_amount=10)
		coupon = frappe.get_doc("POS Coupon", {"coupon_code": code})
		lines = [
			_line(self.ctx, ITEM_A, qty=3, price_list_rate=50),
			_line(self.ctx, ITEM_B, qty=3, price_list_rate=50),
		]
		self.assertAlmostEqual(coupon_value_on(coupon, lines), 10, places=6)
		shares = self._walk_in(code, lines)["line_updates"]
		self.assertEqual([u["coupon_only_discount"] for u in shares], [5.01, 4.98])

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

	def _assert_money_only(self, response, shown, hidden=()):
		"""D-R5-07: the note names both sides by their money, before tax, never by a %."""
		for key in ("message", "message_en", "message_ar"):
			for value in shown:
				self.assertIn(ff_money.fmt(value, self.ctx.currency), response[key], key)
			for value in hidden:
				self.assertNotIn(ff_money.fmt(value, self.ctx.currency), response[key], key)
			self.assertNotIn("%", response[key], key)

	def test_whole_cart_note_names_both_sides_by_their_pre_tax_money(self):
		# The run-5 cart: ITEM_A 100 (F&F 30) + ITEM_B 80 outside F&F, 15% VAT.
		lines = [_line(self.ctx, ITEM_A, price_list_rate=100), _line(self.ctx, ITEM_B, price_list_rate=80)]
		taxes = _exclusive_vat()
		self._settings(excluded_items=[{"item_code": ITEM_B}])
		set_coupon_combination(WHOLE)
		# (a) Amount 40 on Grand Total: "40.00", not its 46.00 with tax; F&F by its 30.00.
		o40 = self._coupon("O40NOTE", discount_type="Amount", discount_amount=40, apply_on="Grand Total")
		won = self._fnf(o40, lines, taxes)
		self.assertTrue(won["valid"], won.get("message"))
		self.assertTrue(won["replaces_friends_and_family_on_cart"])
		self._assert_money_only(won["best_discount"], shown=(40, 30), hidden=(46, 34.5))
		# (b) 20% saves 36 on the cart against F&F's 30: "20%" against "30%" read wrong.
		l20 = self._coupon("L20NOTE", discount_percentage=20)
		won = self._fnf(l20, lines, taxes)
		self.assertTrue(won["valid"], won.get("message"))
		self._assert_money_only(won["best_discount"], shown=(36, 30))
		# F&F wins the cart: the refusal names F&F's 30.00 and the coupon's 25.00.
		a25 = self._coupon("A25NOTE", discount_type="Amount", discount_amount=25)
		lost = self._fnf(a25, lines, taxes)
		self.assertFalse(lost["valid"])
		self._assert_money_only(lost, shown=(30, 25))

	def test_per_line_refusal_of_a_fixed_coupon_names_fnf_by_its_money(self):
		# Amount 50 over 2 x 50 + 80 loses to F&F on every line (30 + 24 = 54 of F&F).
		code = self._coupon("A50MONEY", discount_type="Amount", discount_amount=50)
		lines = [_line(self.ctx, ITEM_A, qty=2), _line(self.ctx, ITEM_B)]
		set_coupon_combination(PER_LINE)
		self._assert_money_only(self._fnf(code, lines), shown=(54, 50), hidden=(27.78,))

	def test_per_line_refusal_wording_follows_the_cart_not_one_tied_line(self):
		# Review M5: Amount 38.99 over 50 + 80 is 15.00 + 23.99 after the cut. On ITEM_A it ties
		# F&F's 15, on ITEM_B F&F's 24 is better: 39.00 against 38.99 is no tie on the cart.
		code = self._coupon("A3899", discount_type="Amount", discount_amount=38.99)
		set_coupon_combination(PER_LINE)
		refused = self._fnf(code, [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B)])
		self.assertFalse(refused["valid"])
		self._assert_money_only(refused, shown=(39, 38.99))
		self.assertIn("this cart saves more with Friends & Family", refused["message_en"])

	def test_per_line_refusal_names_a_stacked_line_by_the_coupon_money_only(self):
		# Re-review R1-M2: when the coupon's face value is above F&F's money, the refusal sets
		# F&F against the coupon on the lines F&F took. On a stacked line the compared figure
		# is the promotion plus the coupon (50 here); the coupon's own money is 10.
		from posnext_promotions.promotions.best_discount_coupon import CouponDecision, _rejection

		line = bd.decide(
			[
				bd.Candidate(source=bd.SOURCE_FRIENDS_AND_FAMILY, amount=60, percentage=30),
				bd.Candidate(source=bd.SOURCE_COUPON, amount=50, code="TOP10"),
			],
			idx=0,
			item_code=ITEM_A,
		)
		decision = CouponDecision(coupon_code="TOP10", result={"total_discount": 80}, items=[])
		decision.line_decisions = [line]
		decision.coupon_own = {0: 10.0}
		refused = _rejection(decision)
		self.assertIn(ff_money.fmt(10), refused["message_en"])
		self.assertNotIn(ff_money.fmt(50), refused["message_en"])

	def test_new_note_strings_have_arabic_rows(self):
		import csv
		import os

		import posnext_promotions

		path = os.path.join(os.path.dirname(posnext_promotions.__file__), "translations", "ar.csv")
		with open(path, encoding="utf-8") as handle:
			rows = {row[0]: row[1] for row in csv.reader(handle) if len(row) > 1}
		for en, ar in bd.CART_MESSAGES.values():
			self.assertEqual(rows.get(en), ar, en)
		# Review M7: the coupon's label is translatable too, and the Arabic reads naturally,
		# with ar.csv's word for a coupon (القسيمة).
		self.assertEqual(rows.get("coupon {0}"), "القسيمة {0}")
		for _en, ar in bd.CART_MESSAGES.values():
			self.assertIn("التوفير في هذه السلة", ar)
		messages = bd.build_cart_messages(
			bd.OUTCOME_OTHER_BETTER,
			bd.Candidate(source=bd.SOURCE_FRIENDS_AND_FAMILY, amount=30),
			bd.Candidate(source=bd.SOURCE_COUPON, amount=40, code="O40"),
		)
		self.assertIn("القسيمة O40", messages["ar"])
		self.assertNotIn("الكوبون", messages["ar"])

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

	def _stacked_sale(self, code, cart):
		"""Walk-in cart of ``(item, qty, price, promotion %)`` lines under an "Add on top" coupon,
		priced by validate_coupon and saved as the POS saves it: each line at the server's
		unit rate to the cent (D-R4-03), the coupon's share per unit on the row."""
		rules = {}
		lines, rows = [], []
		for item, qty, price, pct in cart:
			if pct and item not in rules:
				rules[item] = self._rule(f"L2P{len(rules)}", item, pct)
			line = _line(self.ctx, item, qty=qty, price_list_rate=price)
			if pct:
				# As posCart sends it: the unit rate to the cent and the line's discount from it
				# (currency.unitRateLineDiscount), not the exact % of the line.
				rate = flt(price * (1 - pct / 100), 2)
				line.update(
					{
						"discount_percentage": pct,
						"discount_amount": flt((price - rate) * qty, 2),
						"rate": rate,
						"pricing_rules": rules[item],
					}
				)
			lines.append(line)
		response = self._walk_in(code, lines, customer=PLAIN)
		self.assertTrue(response["valid"], response.get("message"))
		updates = {u["line_key"]: u for u in response["line_updates"]}
		for key, (item, qty, price, _pct) in enumerate(cart):
			row = self._row(item, qty=qty, rules=rules.get(item, ""))
			row.update(
				{"price_list_rate": price, "rate": price, "discount_percentage": 0, "discount_amount": 0}
			)
			update = updates.get(key)
			if update:
				rate = flt(update["rate"], 2)
				row.update(
					{
						"rate": rate,
						"discount_amount": flt(price - rate, 2),
						"posnext_coupon_code": code,
						"posnext_coupon_discount": flt(update["coupon_only_discount"] / qty, 2),
					}
				)
			rows.append(row)
		doc = self._sale(rows, code)
		doc.customer = PLAIN
		return doc

	def test_paid_add_on_top_cart_is_accepted_as_the_cart_split_it(self):
		# Review I1: the cart values a promoted line from its unit rate to the cent and the
		# server from the promotion's exact %, so the two whole-cents cuts can move the lost
		# cents to different lines. A paid sale the cart priced must still be accepted.
		import random

		codes = {
			amount: self._coupon(
				f"TOP{amount}",
				discount_type="Amount",
				discount_amount=amount,
				promotion_conflict="Add on top",
			)
			for amount in (17, 25, 40, 61)
		}
		a, b, c = ITEM_A, ITEM_B, ITEM_C
		carts = [
			(40, [(a, 1, 10.25, 33.33), (b, 1, 100, 0), (c, 3, 50, 33.33)]),  # the reviewer's
			# Refused before the fix (row above its share by the moved cents):
			(17, [(c, 1, 50, 15), (b, 2, 100, 0), (a, 7, 10.25, 33.33)]),
			(40, [(a, 2, 7.77, 15), (c, 3, 19.99, 15)]),
			(61, [(a, 7, 7.77, 33.33), (b, 1, 7.77, 33.33), (c, 7, 7.77, 15)]),
			(61, [(c, 2, 33.34, 12.5), (b, 2, 19.99, 0), (a, 2, 7.77, 33.33)]),
			(25, [(c, 1, 19.99, 15), (a, 2, 7.77, 33.33), (b, 2, 7.77, 15)]),
			(40, [(b, 7, 50, 0), (a, 1, 50, 33.33), (c, 3, 19.99, 12.5)]),
			(61, [(b, 1, 19.99, 15), (c, 1, 10.25, 0), (a, 3, 10.25, 12.5)]),
			(61, [(c, 7, 33.34, 12.5), (a, 3, 100, 0), (b, 3, 33.34, 15)]),
		]
		rnd = random.Random(5)  # and a fixed random set of such carts
		for _ in range(40):
			items = rnd.sample([a, b, c], rnd.choice([2, 3]))
			cart = [
				(
					item,
					rnd.choice([1, 2, 3, 7]),
					rnd.choice([7.77, 10.25, 19.99, 33.34, 50, 100]),
					rnd.choice([0, 12.5, 15, 33.33]),
				)
				for item in items
			]
			carts.append((rnd.choice(list(codes)), cart))
		for amount, cart in carts:
			with self.subTest(amount=amount, cart=cart):
				frappe.db.savepoint("l2_cart")
				try:
					self._code = codes[amount]
					doc = self._stacked_sale(codes[amount], cart)
					validate_scoped_coupon_lines(doc)  # paid: refused if the split disagrees
				finally:
					frappe.db.rollback(save_point="l2_cart")

	def test_paid_uncapped_percentage_add_on_top_cart_is_accepted(self):
		# Re-review R1-I1: an uncapped % "Add on top" coupon on a promoted line. The cart holds
		# the promotion to the cent and the server exact, so a whole-cents cut of the coupon's
		# share would differ by a cent a unit (20% on 12 x 77.77 under 33.33%: 10.37 against
		# 10.36). Such a line is priced at the server's rate; the paid sale is accepted.
		import random

		code = self._coupon("P20TOP", discount_percentage=20, promotion_conflict="Add on top")
		self._code = code
		a, b, c = ITEM_A, ITEM_B, ITEM_C
		carts = [[(a, 12, 77.77, 33.33)]]
		rnd = random.Random(3)
		for _ in range(30):
			items = rnd.sample([a, b, c], rnd.choice([1, 2, 3]))
			carts.append(
				[
					(
						item,
						rnd.choice([1, 3, 7, 12]),
						rnd.choice([7.77, 19.99, 33.34, 77.77, 100]),
						rnd.choice([12.5, 15, 33.33]),
					)
					for item in items
				]
			)
		for cart in carts:
			with self.subTest(cart=cart):
				frappe.db.savepoint("l2_top")
				try:
					validate_scoped_coupon_lines(self._stacked_sale(code, cart))
				finally:
					frappe.db.rollback(save_point="l2_top")

	def test_paid_uncapped_percentage_add_on_top_at_large_qty_is_accepted(self):
		# Re-review R2-m1: a stacked row's share is its rate to the cent less the promotion,
		# so at qty 5-24 it can sit near the money check's tolerance. ~100 seeded carts priced
		# as the till prices them (with the reviewer's 5 x 47.70 under 15%, 5% coupon) go
		# through validate_coupon and the paid invoice check.
		import random

		a, b, c = ITEM_A, ITEM_B, ITEM_C
		codes = {
			pct: self._coupon(f"P{pct}TOPQ", discount_percentage=pct, promotion_conflict="Add on top")
			for pct in (5, 10, 20, 35)
		}
		carts = [(5, [(a, 5, 47.70, 15)])]
		rnd = random.Random(29)
		for _ in range(100):
			items = rnd.sample([a, b, c], rnd.choice([1, 2]))
			cart = [
				(
					item,
					rnd.randint(5, 24),
					rnd.choice([7.77, 12.35, 19.99, 33.34, 47.70, 77.77, 101.11]),
					rnd.choice([12.5, 15, 33.33, 7.5]),
				)
				for item in items
			]
			carts.append((rnd.choice(list(codes)), cart))
		for pct, cart in carts:
			with self.subTest(pct=pct, cart=cart):
				frappe.db.savepoint("l2_topq")
				try:
					self._code = codes[pct]
					validate_scoped_coupon_lines(self._stacked_sale(codes[pct], cart))
				finally:
					frappe.db.rollback(save_point="l2_topq")

	def test_forged_total_within_each_row_slack_is_refused(self):
		# Re-review R1-M1: per line, F&F keeps ITEM_A, the coupon's share of ITEM_C x 100 at
		# 0.80 is 18.46 exact (18.00 after the cut). One row may carry the cut's slack (1.00),
		# but the rows together are held to the exact split: 19.50 is refused.
		code = self._coupon("A30Q100T", discount_type="Amount", discount_amount=30)
		self._code = code
		self._settings(excluded_items=[{"item_code": ITEM_C}])
		set_coupon_combination(PER_LINE)
		row_c = self._row(ITEM_C, qty=100)
		row_c.update(
			{
				"price_list_rate": 0.8,
				"rate": 0.605,
				"discount_percentage": 0,
				"discount_amount": 0.195,
				"posnext_coupon_code": code,
				"posnext_coupon_discount": 0.195,
			}
		)
		rows = [self._row(ITEM_A, pct=30, marked=True), row_c]
		with self.assertRaises(frappe.ValidationError) as caught:
			self._validate_invoice(self._sale(rows, code))
		self.assertIn("on this invoice, more than its split of", str(caught.exception))

	def test_rows_together_are_held_to_the_split(self):
		# Review I1: any one row may carry the cents the cut can lose (a cent a unit: 0.12 on
		# 12 units), but the rows together are held to the server's split. Per line, F&F keeps
		# ITEM_A, so the coupon's lines hold less than its face value and only this catches it.
		code = self._coupon("A30TOT", discount_type="Amount", discount_amount=30)
		self._code = code
		self._settings(excluded_items=[{"item_code": ITEM_B}, {"item_code": ITEM_C}])
		set_coupon_combination(PER_LINE)
		lines = [_line(self.ctx, ITEM_A), _line(self.ctx, ITEM_B), _line(self.ctx, ITEM_C, qty=10)]
		shares = self._shares(self._fnf(code, lines))
		self.assertEqual(sorted(shares), [1, 2])
		row_c = self._coupon_row(ITEM_C, flt(shares[2] / 10 + 0.01, 2))  # 0.10 over on 10 units
		row_c.qty = 10
		rows = [self._row(ITEM_A, pct=30, marked=True), self._coupon_row(ITEM_B, shares[1] + 0.13), row_c]
		with self.assertRaises(frappe.ValidationError) as caught:
			self._validate_invoice(self._sale(rows, code))
		self.assertIn("on this invoice, more than its split of", str(caught.exception))

	def test_uncapped_percentage_on_a_promoted_line_is_whole_cents(self):
		# Review M3: under "Better of the two" a 40% coupon beats a 10% promotion on 3 x 33.34
		# and becomes a fixed amount there: 40.008 is no row's discount, 13.33 x 3 = 39.99 is.
		rule = self._rule("P10M3", ITEM_A, 10)
		code = self._coupon("P40BETTER", discount_percentage=40, promotion_conflict="Better of the two")
		line = self._promoted(ITEM_A, 33.34, 10, rule, qty=3)
		[update] = self._walk_in(code, [line])["line_updates"]
		self.assertEqual(update["coupon_only_discount"], 39.99)
		self.assertEqual(update["discount_amount"], 39.99)

	def test_paid_whole_cart_sale_leaving_a_coupon_line_without_it_is_logged(self):
		# Review M4: the coupon holds the cart (30 beats F&F's 15), but ITEM_A went out at full
		# price with neither the coupon nor F&F. The customer is owed no F&F, so the coupon
		# check logs the line the coupon should have had; the paid sale is kept. R1-M3: once a
		# sale, at submit, though validate runs on the draft's save and again on submit.
		from posnext_promotions.overrides.coupon_split import UNDER_APPLIED_LOG_TITLE, log_under_applied

		code = self._split_cart(WHOLE)
		doc = self._sale([self._row(ITEM_A), self._coupon_row(ITEM_B, 18.46)], code)
		with patch("posnext_promotions.overrides.coupon_split.frappe.log_error") as log_error:
			self._validate_invoice(doc)  # the paid draft's save
			self._validate_invoice(doc)  # submit validates again
			self.assertFalse(self._logged(log_error, UNDER_APPLIED_LOG_TITLE))
			log_under_applied(doc)  # on_submit
		[logged] = self._logged(log_error, UNDER_APPLIED_LOG_TITLE)
		details = json.loads(logged["message"].split("\n", 1)[1])
		self.assertEqual(details["rows"], [{"row": 1, "item": ITEM_A, "server_share": 11.54}])
		self.assertEqual(self._prices(doc)[0], (50, 0, 0, 50))  # kept as paid
		# Every line with its share: nothing to log.
		doc = self._sale([self._coupon_row(ITEM_A, 11.54), self._coupon_row(ITEM_B, 18.46)], code)
		with patch("posnext_promotions.overrides.coupon_split.frappe.log_error") as log_error:
			self._validate_invoice(doc)
			log_under_applied(doc)
		self.assertFalse(self._logged(log_error, UNDER_APPLIED_LOG_TITLE))
		self.assertIn(
			"posnext_promotions.overrides.coupon_split.log_under_applied",
			frappe.get_hooks("doc_events")["Sales Invoice"]["on_submit"],
		)

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
				# D-R5-05: the coupon holds the cart, so the customer is owed no F&F on this
				# invoice. A paid one records no disagreement and no F&F plan to log.
				flags = doc.flags
				self.assertEqual((flags.get(ff_paid.DISAGREEMENTS_FLAG) or {}).get("entries") or [], [])
				plan = flags.get(ff_paid.PLAN_FLAG) or {}
				self.assertFalse(any(line.get("applies") for line in plan.get("items") or []))
				self.assertFalse((plan.get("caps") or {}).get("capped"))
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
		# ITEM_C 18.46, which a row holds as 0.18 a unit: 18.00 (D-R5-06). 0.2346 a unit is
		# only 0.05 more per unit but 23.46 on the line, 5 more: refused. 0.18 x 100 passes.
		code = self._coupon("A30Q100", discount_type="Amount", discount_amount=30)
		self._code = code
		self._settings(excluded_items=[{"item_code": ITEM_C}])
		set_coupon_combination(PER_LINE)
		line_c = _line(self.ctx, ITEM_C, qty=100, price_list_rate=0.8)
		fnf = self._fnf(code, [_line(self.ctx, ITEM_A), line_c])
		self.assertEqual(self._shares(fnf), {1: 18.0})

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
		# D-R5-05: F&F is rightly absent, so the sale logs no F&F disagreement.
		self.assertEqual(
			frappe.get_all(
				"Friends and Family Log",
				filters={"reference_name": sale.name, "log_type": ("like", "%Disagreement")},
				pluck="reason",
			),
			[],
		)

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
