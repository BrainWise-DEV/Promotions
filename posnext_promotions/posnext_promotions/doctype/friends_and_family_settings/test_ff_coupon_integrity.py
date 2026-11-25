# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Task 6d — coupon vs Friends & Family integrity.

* A scoped coupon is valued on its in-scope lines only.
* ``validate_coupon`` and ``calculate_coupon_discount`` go through one resolver and
  always agree on the winner and the coupon's value.
* The header base adds only *exclusive* taxes (inclusive ones are inside the lines).
* The server caps an invoice header discount at its coupon's value, even when F&F
  applied nowhere; a coupon that gives nothing is dropped.
* A capped F&F is shown by its money value, not the program %.
* Paid invoices are never changed: a header above its coupon throws online, logs offline.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.api.coupon_engine import apply_coupon_discount, get_coupon_eligible_items
from posnext_promotions.api.offers import calculate_coupon_discount, validate_coupon
from posnext_promotions.overrides.scoped_coupon import validate_scoped_coupon_lines
from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_paid_freeze import (
	PaidInvoiceFixture,
)
from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_constants, ff_money, ff_paid
from posnext_promotions.promotions.friends_and_family_testing import set_coupon_combination
from posnext_promotions.promotions.best_discount_coupon import decide_coupon_vs_fnf
from posnext_promotions.promotions.friends_and_family_apply import apply_friends_and_family_on_invoice
from posnext_promotions.promotions.friends_and_family_log import (
	LOG_OFFLINE_DISAGREEMENT,
	LOG_PAID_DISAGREEMENT,
	log_invoice_submit,
)
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	_cart_payload,
	_ensure_group_tree,
	_line,
	_make_pos_coupon,
)

BRAND = "_PNXT_FFCI_BRAND"


def _inclusive_tax(rate=10, amount=10.0):
	return [{"charge_type": "On Net Total", "rate": rate, "included_in_print_rate": 1, "tax_amount": amount}]


def _exclusive_tax(rate=10, amount=10.0):
	return [{"charge_type": "On Net Total", "rate": rate, "included_in_print_rate": 0, "tax_amount": amount}]


class TestFFCouponIntegrity(PaidInvoiceFixture, FrappeTestCase):
	def setUp(self):
		super().setUp()
		if not frappe.db.exists("Brand", BRAND):
			frappe.get_doc({"doctype": "Brand", "brand": BRAND}).insert(ignore_permissions=True)

	# ---- helpers ------------------------------------------------------------

	def _coupon(self, code, **fields):
		return _make_pos_coupon(code, **fields)

	def _lines(self, *specs):
		"""``(item_code, qty, price_list_rate, brand)`` → cart lines as the POS sends them."""
		lines = []
		for item_code, qty, price, brand in specs:
			line = _line(self.ctx, item_code, qty=qty, price_list_rate=price)
			line["brand"] = brand
			if brand:
				# F&F reads scope fields from Item, never from the line (Task 6f).
				frappe.db.set_value("Item", item_code, "brand", brand)
				getattr(frappe.local, "request_cache", {}).clear()
			lines.append(line)
		return lines

	def _validate(self, code, lines, taxes=None):
		return validate_coupon(
			code,
			customer=self.ctx.customer,
			company=self.ctx.company,
			items=json.dumps(lines),
			pos_profile=self.ctx.pos_profile,
			taxes=json.dumps(taxes) if taxes is not None else None,
		)

	def _calculate(self, code, lines, taxes=None):
		data = _cart_payload(self.ctx, lines)
		if taxes is not None:
			data["taxes"] = taxes
			data["total_taxes_and_charges"] = sum(flt(t.get("tax_amount")) for t in taxes)
		return calculate_coupon_discount(
			code, json.dumps(data), customer=self.ctx.customer, company=self.ctx.company
		)

	def _excluded(self, *item_codes):
		self._settings(excluded_items=[{"item_code": code} for code in item_codes])

	# ---- scope --------------------------------------------------------------

	def test_scoped_coupon_values_only_scope_lines(self):
		coupon = self._coupon(
			"FFCI-BRAND20", discount_percentage=20, apply_scope="Brand", applicable_brand=BRAND
		)
		lines = self._lines((ITEM_A, 1, 100, BRAND), (ITEM_B, 1, 900, None))

		legacy = apply_coupon_discount(coupon, cart_total=1000, net_total=1000, items=lines)
		self.assertEqual(flt(legacy["discount"]), 20)

		decision = decide_coupon_vs_fnf(
			{"items": lines}, coupon, customer=self.ctx.customer, pos_profile=self.ctx.pos_profile
		)
		self.assertEqual(flt(decision.coupon_value), 20)
		self.assertFalse(decision.coupon_wins)  # F&F 30 on the in-scope line beats 20

		for response in (self._validate("FFCI-BRAND20", lines), self._calculate("FFCI-BRAND20", lines)):
			self.assertFalse(response["valid"])
			self.assertEqual(flt(response["coupon_value"]), 20)

	# ---- scope comes from the Item master (Ruling 78) ------------------------

	def _master(self, item_code, **values):
		frappe.db.set_value("Item", item_code, values)
		getattr(frappe.local, "request_cache", {}).clear()

	def test_coupon_scope_reads_item_master_not_the_line(self):
		_group_parent, group = _ensure_group_tree()
		self._master(ITEM_A, brand=BRAND)
		self._master(ITEM_B, brand=None)
		# The client claims the opposite of the Item master on every line.
		lines = [
			dict(_line(self.ctx, ITEM_A, qty=1, price_list_rate=100), brand=None),
			dict(_line(self.ctx, ITEM_B, qty=1, price_list_rate=900), brand=BRAND, item_group=group),
		]
		by_brand = self._coupon(
			"FFCI-M-BR", discount_percentage=10, apply_scope="Brand", applicable_brand=BRAND
		)
		excluding = self._coupon("FFCI-M-EX", discount_percentage=10, excluded_brands=[{"brand": BRAND}])
		by_group = self._coupon(
			"FFCI-M-GR", discount_percentage=10, apply_scope="Item Group", applicable_item_group=group
		)

		self.assertEqual(flt(apply_coupon_discount(by_brand, cart_total=1000, items=lines)["discount"]), 10)
		self.assertEqual([i["item_code"] for i in get_coupon_eligible_items(by_brand, lines)], [ITEM_A])
		self.assertEqual([i["item_code"] for i in get_coupon_eligible_items(excluding, lines)], [ITEM_B])
		self.assertEqual(get_coupon_eligible_items(by_group, lines), [])
		self.assertEqual(flt(apply_coupon_discount(by_group, cart_total=1000, items=lines)["discount"]), 0)

	def test_invoice_coupon_scope_check_reads_item_master(self):
		self._master(ITEM_A, brand=BRAND)
		self._master(ITEM_B, brand=None)
		self._coupon("FFCI-M-INV", discount_percentage=10, apply_scope="Brand", applicable_brand=BRAND)

		def doc(item_code, brand):
			row = frappe._dict(
				idx=1,
				item_code=item_code,
				qty=1,
				brand=brand,
				is_free_item=0,
				posnext_coupon_code="FFCI-M-INV",
				discount_percentage=10,
				discount_amount=0,
			)
			return frappe._dict(items=[row], coupon_code="FFCI-M-INV")

		# Scope only: these rows carry no prices for the split check to re-split.
		split = patch("posnext_promotions.overrides.coupon_split.check_coupon_split")
		split.start()
		self.addCleanup(split.stop)
		validate_scoped_coupon_lines(doc(ITEM_A, None))  # covered by its Item's brand
		with self.assertRaises(frappe.ValidationError):
			validate_scoped_coupon_lines(doc(ITEM_B, BRAND))  # the row's brand is not trusted

	# ---- Sales Order / Quotation (Ruling 75) ---------------------------------

	def test_fnf_is_no_rival_on_sales_order_or_quotation(self):
		self._coupon("FFCI-SO15", discount_percentage=15)
		lines = self._lines((ITEM_A, 1, 50, None))
		# On an invoice F&F 30 % beats the 15 % coupon.
		self.assertFalse(self._validate("FFCI-SO15", lines)["valid"])
		self.assertFalse(self._calculate("FFCI-SO15", lines)["valid"])
		self.assertIn("friends_and_family", validate_coupon("FFCI-SO15", self.ctx.customer, self.ctx.company))
		for doctype in ("Sales Order", "Quotation"):
			with self.subTest(doctype=doctype):
				validated = validate_coupon(
					"FFCI-SO15",
					customer=self.ctx.customer,
					company=self.ctx.company,
					items=json.dumps(lines),
					pos_profile=self.ctx.pos_profile,
					target_doctype=doctype,
				)
				self.assertTrue(validated["valid"])
				self.assertEqual(flt(validated["total_discount"]), 7.5)

				data = dict(_cart_payload(self.ctx, lines), doctype=doctype)
				calculated = calculate_coupon_discount(
					"FFCI-SO15", json.dumps(data), customer=self.ctx.customer, company=self.ctx.company
				)
				self.assertTrue(calculated["valid"])
				self.assertEqual(flt(calculated["discount"]), 7.5)
				self.assertFalse(calculated.get("suppress_friends_and_family"))

				hint = validate_coupon(
					"FFCI-SO15", self.ctx.customer, self.ctx.company, target_doctype=doctype
				)
				self.assertNotIn("friends_and_family", hint)

	# ---- one resolver -------------------------------------------------------

	def test_validate_and_calculate_agree(self):
		cases = [
			# (code, coupon fields, lines, taxes, coupon wins)
			("FFCI-P40", {"discount_percentage": 40}, self._lines((ITEM_A, 1, 50, None)), None, True),
			("FFCI-P15", {"discount_percentage": 15}, self._lines((ITEM_A, 1, 50, None)), None, False),
			(
				"FFCI-A50",
				{"discount_type": "Amount", "discount_amount": 50},
				self._lines((ITEM_A, 2, 50, None), (ITEM_B, 1, 80, None)),
				None,
				False,
			),
			# Tax-inclusive "Grand Total" coupon at a tie with F&F (33.00 each) → F&F
			(
				"FFCI-GT30",
				{"discount_percentage": 30, "apply_on": "Grand Total"},
				self._lines((ITEM_A, 1, 110, None)),
				_inclusive_tax(),
				False,
			),
			# One point above → coupon (34.10 vs 33.00)
			(
				"FFCI-GT31",
				{"discount_percentage": 31, "apply_on": "Grand Total"},
				self._lines((ITEM_A, 1, 110, None)),
				_inclusive_tax(),
				True,
			),
		]
		for code, fields, lines, taxes, coupon_wins in cases:
			with self.subTest(code=code):
				self._coupon(code, **fields)
				validated = self._validate(code, lines, taxes)
				calculated = self._calculate(code, lines, taxes)
				self.assertEqual(bool(validated["valid"]), coupon_wins)
				self.assertEqual(bool(calculated["valid"]), coupon_wins)
				self.assertEqual(flt(validated["coupon_value"]), flt(calculated["coupon_value"]))
				if coupon_wins:
					self.assertEqual(flt(validated["total_discount"]), flt(calculated["discount"]))
					self.assertTrue(calculated.get("suppress_friends_and_family"))
				else:
					self.assertEqual(calculated.get("reason"), validated.get("reason"))

	# ---- taxes in the header base -------------------------------------------

	def test_inclusive_tax_not_double_counted(self):
		self._excluded(ITEM_A)  # no F&F: the coupon's own value is what we read
		self._coupon("FFCI-GTI", discount_percentage=10, apply_on="Grand Total")
		lines = self._lines((ITEM_A, 1, 110, None))
		validated = self._validate("FFCI-GTI", lines, _inclusive_tax())
		calculated = self._calculate("FFCI-GTI", lines, _inclusive_tax())
		self.assertTrue(validated["valid"] and calculated["valid"])
		self.assertEqual(flt(validated["total_discount"]), 11)
		self.assertEqual(flt(calculated["discount"]), 11)

	def test_exclusive_tax_still_added(self):
		self._excluded(ITEM_A)
		self._coupon("FFCI-GTE", discount_percentage=10, apply_on="Grand Total")
		lines = self._lines((ITEM_A, 1, 100, None))
		validated = self._validate("FFCI-GTE", lines, _exclusive_tax())
		calculated = self._calculate("FFCI-GTE", lines, _exclusive_tax())
		self.assertTrue(validated["valid"] and calculated["valid"])
		# K1 M-2: the coupon's money is its line discount (10), before the tax it also saves.
		self.assertEqual(flt(validated["total_discount"]), 10)
		self.assertEqual(flt(calculated["discount"]), 10)

	def test_grand_total_coupon_compared_with_tax_on_both_sides(self):
		# Ruling 56: 10 % exclusive VAT, F&F 10 % saves 11.00 with its tax share.
		self._settings(discount_percentage=10)
		lines = self._lines((ITEM_A, 1, 100, None))
		for pct, coupon_wins in ((10, False), (9.5, False), (11, True)):
			with self.subTest(pct=pct):
				code = f"FFCI-GTX-{pct}"
				self._coupon(code, discount_percentage=pct, apply_on="Grand Total")
				validated = self._validate(code, lines, _exclusive_tax())
				calculated = self._calculate(code, lines, _exclusive_tax())
				self.assertEqual(bool(validated["valid"]), coupon_wins)
				self.assertEqual(bool(calculated["valid"]), coupon_wins)
				# The comparison counts the tax (pct x 1.1 vs 11); the coupon's money is pct (M-2).
				self.assertEqual(flt(calculated["coupon_value"]), flt(pct, 2))

	def test_invoice_grand_total_coupon_compared_with_tax_on_both_sides(self):
		self._settings(discount_percentage=10)
		for pct, coupon_wins in ((10, False), (9.5, False), (11, True)):
			with self.subTest(pct=pct):
				code = f"FFCI-GTXI-{pct}"
				self._coupon(code, discount_percentage=pct, apply_on="Grand Total")
				row = self._row()
				row.update({"price_list_rate": 100, "rate": 100, "discount_amount": 0})
				header = flt(pct * 1.1, 2)
				doc = self._invoice(
					[row],
					paid=False,
					coupon_code=code,
					discount_amount=header,
					taxes=[frappe._dict(t) for t in _exclusive_tax()],
				)
				apply_friends_and_family_on_invoice(doc)
				line = doc["items"][0]
				if coupon_wins:
					self.assertEqual(flt(doc.discount_amount, 2), header)
					self.assertEqual(flt(line.discount_percentage), 0)
				else:
					self.assertEqual(flt(doc.discount_amount), 0)
					self.assertEqual(flt(line.discount_percentage), 10)

	def test_invoice_header_base_adds_only_exclusive_tax(self):
		# Invoice side (scenario B coupon value): inclusive tax rows add nothing.
		self._excluded(ITEM_A, ITEM_B)
		self._coupon("FFCI-INV", discount_percentage=10, apply_on="Grand Total")
		for taxes, header, expected in ((_inclusive_tax(), 50, 11), (_exclusive_tax(), 50, 11)):
			with self.subTest(inclusive=taxes[0]["included_in_print_rate"]):
				price = 110 if taxes[0]["included_in_print_rate"] else 100
				row = self._row()
				row.update({"price_list_rate": price, "rate": price, "discount_amount": 0})
				doc = self._invoice(
					[row],
					paid=False,
					coupon_code="FFCI-INV",
					discount_amount=header,
					total_taxes_and_charges=10,
					taxes=[frappe._dict(t) for t in taxes],
				)
				apply_friends_and_family_on_invoice(doc)
				self.assertEqual(flt(doc.discount_amount), expected)

	# ---- header discount on the invoice ---------------------------------------

	def test_header_discount_capped_without_fnf_lines(self):
		self._excluded(ITEM_B)
		self._coupon("FFCI-FIX50", discount_type="Amount", discount_amount=50)
		doc = self._invoice(
			[self._row(ITEM_B, qty=10)], paid=False, coupon_code="FFCI-FIX50", discount_amount=500
		)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 50)
		self.assertEqual(doc.coupon_code, "FFCI-FIX50")

	def test_header_percentage_capped_without_fnf_lines(self):
		self._excluded(ITEM_B)
		self._coupon("FFCI-FIX40", discount_type="Amount", discount_amount=40)
		doc = self._invoice(
			[self._row(ITEM_B, qty=10)],
			paid=False,
			coupon_code="FFCI-FIX40",
			additional_discount_percentage=50,
			discount_amount=400,
		)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 40)
		self.assertEqual(flt(doc.additional_discount_percentage), 0)

	def test_header_below_coupon_value_not_raised(self):
		self._excluded(ITEM_B)
		self._coupon("FFCI-FIX60", discount_type="Amount", discount_amount=60)
		doc = self._invoice(
			[self._row(ITEM_B, qty=10)], paid=False, coupon_code="FFCI-FIX60", discount_amount=30
		)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 30)

	def test_zero_discount_clears_coupon_code(self):
		self._coupon("FFCI-ZERO", discount_percentage=10)
		doc = self._invoice([self._row(ITEM_B)], paid=False, coupon_code="FFCI-ZERO", discount_amount=0)
		apply_friends_and_family_on_invoice(doc)
		self.assertFalse(doc.coupon_code)

		# A line still carries the coupon: the code stays (line-level coupon usage).
		tagged = self._row(ITEM_A)
		tagged["posnext_coupon_code"] = "FFCI-ZERO"
		kept = self._invoice([tagged], paid=False, coupon_code="FFCI-ZERO", discount_amount=0)
		self._excluded(ITEM_A)
		apply_friends_and_family_on_invoice(kept)
		self.assertEqual(kept.coupon_code, "FFCI-ZERO")

		# A code that is not a POS Coupon is not ours to drop.
		other = self._invoice([self._row(ITEM_B)], paid=False, coupon_code="FFCI-NOT-A-POS-COUPON")
		apply_friends_and_family_on_invoice(other)
		self.assertEqual(other.coupon_code, "FFCI-NOT-A-POS-COUPON")

	# ---- capped F&F message ----------------------------------------------------

	def test_capped_fnf_message_uses_amount(self):
		self._settings(max_discount_per_order=10)  # F&F on ITEM_A: 15 → capped at 10
		lines = self._lines((ITEM_A, 1, 50, None))
		capped = ff_money.fmt(10, self.ctx.currency)
		for code, fields in (
			("FFCI-CAP-P", {"discount_percentage": 15}),  # line-level: 7.50 vs 10
			("FFCI-CAP-A", {"discount_type": "Amount", "discount_amount": 5}),  # order-level: 5 vs 10
		):
			with self.subTest(code=code):
				self._coupon(code, **fields)
				response = self._validate(code, lines)
				self.assertFalse(response["valid"])
				self.assertIn(f"Friends & Family discount ({capped})", response["message_en"])
				self.assertNotIn("30%", response["message_en"])
				self.assertNotIn("20%", response["message_en"])

	# ---- paid invoices: the cap never mutates ------------------------------------

	def test_paid_online_header_above_coupon_throws_without_fnf_lines(self):
		self._excluded(ITEM_B)
		self._coupon("FFCI-PAID50", discount_type="Amount", discount_amount=50)
		doc = self._invoice([self._row(ITEM_B, qty=10)], coupon_code="FFCI-PAID50", discount_amount=500)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertIn("FFCI-PAID50", str(caught.exception))
		self.assertEqual(flt(doc.discount_amount), 500)
		self.assertEqual(doc.coupon_code, "FFCI-PAID50")

	def test_paid_offline_header_above_coupon_logged_without_fnf_lines(self):
		self._excluded(ITEM_B)
		self._coupon("FFCI-OFF50", discount_type="Amount", discount_amount=50)
		doc = self._invoice(
			[self._row(ITEM_B, qty=10)],
			paid=False,
			offline=True,
			coupon_code="FFCI-OFF50",
			discount_amount=500,
		)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 500)
		self.assertEqual(doc.coupon_code, "FFCI-OFF50")
		log_invoice_submit(doc)
		self.assertEqual(
			[log.reason for log in self._logs(doc, LOG_OFFLINE_DISAGREEMENT)], [ff_paid.HEADER_DISCOUNT]
		)
		self.assertEqual(self._logs(doc, LOG_PAID_DISAGREEMENT), [])

	def _scoped_fnf_invoice(self, header, **kwargs):
		"""Probe R1: a 10% order coupon scoped to ITEM_A (worth 5 at list price); ITEM_A on
		an F&F line at 35, ITEM_B at 80 out of scope."""
		self._excluded(ITEM_B)
		self._coupon(
			"FFCI-SCOPED10",
			max_amount=1000,
			apply_scope="Item Code",
			applicable_items=[{"item_code": ITEM_A}],
			exclude_already_discounted_items=0,
		)
		rows = [self._row(ITEM_A, pct=30, marked=True), self._row(ITEM_B)]
		return self._invoice(rows, coupon_code="FFCI-SCOPED10", discount_amount=header, **kwargs)

	def test_paid_online_scoped_coupon_header_next_to_fnf_above_value_throws(self):
		"""Ruling 92: next to F&F lines, a header above the coupon's scoped value blocks online."""
		doc = self._scoped_fnf_invoice(11)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertIn("is more than coupon FFCI-SCOPED10 gives", str(caught.exception))
		self.assertEqual(flt(doc.discount_amount), 11)

	def test_paid_online_scoped_coupon_header_next_to_fnf_only_scope_refused(self):
		"""Ruling 98: a line that got F&F gets no coupon, so a coupon scoped to the F&F line
		alone is worth 0 on the paid invoice: any header above one unit is refused online
		(3.5 and 5 were the logged stacked case before)."""
		for header in (0.5, 3.5, 5):
			with self.subTest(header=header):
				doc = self._scoped_fnf_invoice(header)
				with self.assertRaises(frappe.ValidationError) as caught:
					apply_friends_and_family_on_invoice(doc)
				self.assertIn("is more than coupon FFCI-SCOPED10 gives", str(caught.exception))
				self.assertEqual(flt(doc.discount_amount), header)

	def test_scoped_coupon_next_to_fnf_cart_sends_no_header(self):
		"""Ruling 98 parity: in the R1 shape the cart's coupon header is 0 (F&F wins on
		ITEM_A, ITEM_B is out of scope), so the paid invoice the cart builds is saved."""
		self._excluded(ITEM_B)
		self._coupon(
			"FFCI-SCOPED10",
			max_amount=1000,
			apply_scope="Item Code",
			applicable_items=[{"item_code": ITEM_A}],
			exclude_already_discounted_items=0,
		)
		calculated = self._calculate(
			"FFCI-SCOPED10", self._lines((ITEM_A, 1, 50, None), (ITEM_B, 1, 80, None))
		)
		self.assertFalse(calculated["valid"])
		self.assertEqual(flt(calculated["discount"]), 0)
		doc = self._scoped_fnf_invoice(flt(calculated["discount"]))
		apply_friends_and_family_on_invoice(doc)  # paid online: must not throw
		self.assertEqual(flt(doc.discount_amount), 0)

	def test_paid_online_coupon_header_valued_on_lines_without_fnf(self):
		"""Ruling 98: the coupon is valued on ITEM_B only (8), never on the F&F line.
		8 next to F&F is the logged stacked case; 9 is refused."""
		self._excluded(ITEM_B)
		self._coupon("FFCI-NOFNF10", max_amount=1000)
		# The cart never sends more than that limit.
		calculated = self._calculate(
			"FFCI-NOFNF10", self._lines((ITEM_A, 1, 50, None), (ITEM_B, 1, 80, None))
		)
		self.assertLessEqual(flt(calculated["discount"]), 8)
		for header, refused in ((8, False), (9, True)):
			with self.subTest(header=header):
				rows = [self._row(ITEM_A, pct=30, marked=True), self._row(ITEM_B)]
				doc = self._invoice(rows, coupon_code="FFCI-NOFNF10", discount_amount=header)
				if refused:
					with self.assertRaises(frappe.ValidationError) as caught:
						apply_friends_and_family_on_invoice(doc)
					self.assertIn("is more than coupon FFCI-NOFNF10 gives", str(caught.exception))
					continue
				apply_friends_and_family_on_invoice(doc)
				self.assertEqual(flt(doc.discount_amount), header)
				entries = [e for e in self._entries(doc) if e["reason"] == ff_paid.HEADER_DISCOUNT]
				self.assertEqual([e.get("stacked") for e in entries], [1])

	def test_paid_online_coupon_header_all_lines_fnf_refused(self):
		"""Every paid line got F&F: the coupon is worth 0 there, never its % of the whole base.
		The cart sends no header there either, for an order-level or a line-level coupon."""
		self._coupon("FFCI-ALLFNF10", max_amount=1000)
		self._coupon("FFCI-ALLFNF10L", apply_on="Grand Total")  # uncapped %: per line
		for code in ("FFCI-ALLFNF10", "FFCI-ALLFNF10L"):
			with self.subTest(code=code):
				calculated = self._calculate(code, self._lines((ITEM_A, 1, 50, None)))
				self.assertEqual(flt(calculated["discount"]), 0)
		doc = self._invoice(
			[self._row(ITEM_A, pct=30, marked=True)], coupon_code="FFCI-ALLFNF10", discount_amount=2
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertIn("is more than coupon FFCI-ALLFNF10 gives", str(caught.exception))

	def test_paid_draft_header_above_scoped_value_logged(self):
		"""Ruling 97: pos_next flags a re-saved stored paid draft; its header is logged, not
		refused, as offline. The same invoice paid online and not a draft is refused."""
		doc = self._scoped_fnf_invoice(11)
		doc.flags[ff_paid.PAID_DRAFT_FLAG] = True
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 11)
		entries = [e for e in self._entries(doc) if e["reason"] == ff_paid.HEADER_DISCOUNT]
		self.assertEqual(len(entries), 1)
		log_invoice_submit(doc)
		self.assertEqual(
			[log.reason for log in self._logs(doc, LOG_PAID_DISAGREEMENT)], [ff_paid.HEADER_DISCOUNT]
		)
		self.assertEqual(self._logs(doc, LOG_OFFLINE_DISAGREEMENT), [])

		online = self._scoped_fnf_invoice(11)
		with self.assertRaises(frappe.ValidationError):
			apply_friends_and_family_on_invoice(online)

	def test_paid_draft_row_over_entitlement_still_refused(self):
		"""Ruling 97 covers the header only: a paid draft's F&F row above the entitlement
		(90% where 30% is due) has no other bound, so it is still refused online."""
		doc = self._invoice([self._row(ITEM_A, pct=90, marked=True)])
		doc.flags[ff_paid.PAID_DRAFT_FLAG] = True
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertIn("this customer is entitled to", str(caught.exception))

	def test_paid_offline_scoped_coupon_header_next_to_fnf_logged(self):
		doc = self._scoped_fnf_invoice(11, paid=False, offline=True)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 11)
		log_invoice_submit(doc)
		self.assertEqual(
			[log.reason for log in self._logs(doc, LOG_OFFLINE_DISAGREEMENT)], [ff_paid.HEADER_DISCOUNT]
		)

	def test_every_coupon_is_compared_per_line(self):
		# K1: uncapped %, capped % and fixed amount are all applied per line; set per line,
		# they are compared per line too.
		set_coupon_combination(ff_constants.COUPON_PER_LINE)
		self.addCleanup(set_coupon_combination, None)
		lines = self._lines((ITEM_A, 1, 50, None))
		for n, fields in enumerate(
			(
				{"discount_percentage": 40},
				{"discount_percentage": 40, "max_amount": 18},
				{"discount_type": "Amount", "discount_amount": 18},
			)
		):
			with self.subTest(fields=fields):
				code = f"FFCI-GRAN-{n}"
				self._coupon(code, **fields)
				calculated = self._calculate(code, lines)
				self.assertTrue(calculated["valid"], calculated.get("message"))
				self.assertEqual(calculated["granularity"], bd.GRANULARITY_LINE)
				self.assertEqual(calculated["best_discount"]["granularity"], bd.GRANULARITY_LINE)
				self.assertEqual([u["line_key"] for u in calculated["line_updates"]], [0])

	# ---- cart / invoice parity ---------------------------------------------------

	def test_calculated_coupon_header_kept_by_invoice(self):
		"""A header coupon (an older POS's offline sale, or the desk) is held to its value."""
		from posnext_promotions.test_promotions import _make_rule

		self._excluded(ITEM_A, ITEM_B)  # no F&F: only the coupon's own value matters
		rule = _make_rule(
			"_PNXT_FFP_PRICE10",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=10,
		)
		coupons = (
			{"discount_type": "Amount", "discount_amount": 60},
			{"discount_percentage": 20, "max_amount": 100},  # order-level %
		)
		# Ruling 93: a "Better of the two" order coupon only tops up the promoted line
		# (its share on ITEM_A less the 5 the line keeps): 60 → 55, 26 → 21.
		# Skip: ITEM_B alone (60; 16). Add on top: 60 over rooms 45 : 80; 9 + 16 = 25.
		expected = {
			"FFCI-PAR-SKIP-0": 60,
			"FFCI-PAR-SKIP-1": 16,
			"FFCI-PAR-BETT-0": 55,
			"FFCI-PAR-BETT-1": 21,
			"FFCI-PAR-ADD -0": 60,
			"FFCI-PAR-ADD -1": 25,
		}
		for conflict in ("Skip the line", "Better of the two", "Add on top"):
			for n, fields in enumerate(coupons):
				code = f"FFCI-PAR-{conflict[:4].upper()}-{n}"
				self._coupon(code, promotion_conflict=conflict, **fields)
				self._check_header_limit(code, rule, expected[code])

	def _price_rule(self, percentage):
		from posnext_promotions.test_promotions import _make_rule

		return _make_rule(
			f"_PNXT_FFP_PRICE{percentage}",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=percentage,
		)

	def test_better_order_coupon_line_discount_above_its_share_adds_nothing(self):
		"""A 20% order coupon (10 on ITEM_A) next to ITEM_A's own 30% (15): that line adds 0,
		so the header is the 16 on ITEM_B alone, on the unpaid and the paid invoice."""
		self._excluded(ITEM_A, ITEM_B)
		self._coupon(
			"FFCI-BETTER-ZERO", discount_percentage=20, max_amount=100, promotion_conflict="Better of the two"
		)
		self._check_header_limit("FFCI-BETTER-ZERO", self._price_rule(30), 16, promoted_pct=30)

	def test_better_order_coupon_mixed_lines_paid_header_capped(self):
		"""Promoted ITEM_A (10%: 5) tops up to the coupon's 10; plain ITEM_B gives 16: 21.
		The paid invoice keeps 21 and refuses 22 (the coupon never stacks on the promotion)."""
		self._excluded(ITEM_A, ITEM_B)
		self._coupon(
			"FFCI-BETTER-MIX", discount_percentage=20, max_amount=100, promotion_conflict="Better of the two"
		)
		rule = self._price_rule(10)
		self._check_header_limit("FFCI-BETTER-MIX", rule, 21)
		doc = self._invoice(
			[self._row(ITEM_A, pct=10, rules=rule), self._row(ITEM_B)],
			coupon_code="FFCI-BETTER-MIX",
			discount_amount=22,
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertIn("is more than coupon FFCI-BETTER-MIX gives", str(caught.exception))

	def test_calculated_coupon_header_kept_by_invoice_qty_two(self):
		"""Ruling 96: at qty 2 the saved row's discount_amount is per unit; the invoice values
		the line's own discount from its rate, so the header's value still holds. The 20%
		"Better of the two" coupon: ITEM_A ×2 tops up 20 − 10 = 10, ITEM_B 16: 26."""
		self._excluded(ITEM_A, ITEM_B)
		rule = self._price_rule(10)
		coupons = (
			{"discount_type": "Amount", "discount_amount": 60},
			{"discount_percentage": 20, "max_amount": 100},
		)
		# ITEM_A x2 at 45. Better of the two, Amount: 33.33 - 10 + 26.67 = 50.
		expected = {
			"FFCI-PQ2-SKIP-0": 60,
			"FFCI-PQ2-SKIP-1": 16,
			"FFCI-PQ2-BETT-0": 50,
			"FFCI-PQ2-BETT-1": 26,
			"FFCI-PQ2-ADD -0": 60,
			"FFCI-PQ2-ADD -1": 34,
		}
		for conflict in ("Skip the line", "Better of the two", "Add on top"):
			for n, fields in enumerate(coupons):
				code = f"FFCI-PQ2-{conflict[:4].upper()}-{n}"
				self._coupon(code, promotion_conflict=conflict, **fields)
				self._check_header_limit(code, rule, expected[code], qty=2)

	def test_better_order_coupon_forged_row_discount_amount_ignored(self):
		"""Ruling 96: a row that understates discount_amount (0.01) while charging rate 45
		(10% off 50) is still credited with its 5: the paid header caps at 21, not 25.99."""
		self._excluded(ITEM_A, ITEM_B)
		self._coupon(
			"FFCI-BETTER-FORGE",
			discount_percentage=20,
			max_amount=1000,
			promotion_conflict="Better of the two",
		)
		for header, refused in ((21, False), (22, True), (25.99, True)):
			with self.subTest(header=header):
				row = self._row(ITEM_A, pct=10)
				row.discount_amount = 0.01
				doc = self._invoice(
					[row, self._row(ITEM_B)], coupon_code="FFCI-BETTER-FORGE", discount_amount=header
				)
				if refused:
					with self.assertRaises(frappe.ValidationError) as caught:
						apply_friends_and_family_on_invoice(doc)
					self.assertIn("is more than coupon FFCI-BETTER-FORGE gives", str(caught.exception))
				else:
					apply_friends_and_family_on_invoice(doc)
					self.assertEqual(flt(doc.discount_amount), header)

	def _check_header_limit(self, code, rule, header, promoted_pct=10, qty=1):
		"""A header coupon (an older POS's offline sale, or the desk) is held to ``header``,
		the coupon's value worked out by hand: kept at it, capped or refused above it."""
		with self.subTest(code=code, qty=qty):
			row_a = self._row(ITEM_A, qty=qty, pct=promoted_pct, rules=rule)  # saved row: per unit

			def invoice(paid, amount):
				return self._invoice(
					[row_a.copy(), self._row(ITEM_B)], paid=paid, coupon_code=code, discount_amount=amount
				)

			for paid in (False, True):
				doc = invoice(paid, header)
				apply_friends_and_family_on_invoice(doc)  # paid online: must not throw
				self.assertEqual(flt(doc.discount_amount), header)
			if header <= 0:
				return
			# The coupon's value is the invoice's limit: unpaid caps at it, paid refuses above it.
			doc = invoice(False, header + 5)
			apply_friends_and_family_on_invoice(doc)
			self.assertAlmostEqual(flt(doc.discount_amount), header, places=2)
			with self.assertRaises(frappe.ValidationError):
				apply_friends_and_family_on_invoice(invoice(True, header + 1))
