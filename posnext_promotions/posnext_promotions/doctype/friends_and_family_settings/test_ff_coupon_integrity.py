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
from posnext_promotions.promotions import ff_money, ff_paid
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
		self.assertEqual(flt(validated["total_discount"]), 11)
		self.assertEqual(flt(calculated["discount"]), 11)

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
				self.assertEqual(flt(calculated["coupon_value"]), flt(pct * 1.1, 2))

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

	def test_resolver_granularity_is_shared(self):
		# Uncapped % → per line; fixed amount or capped % → whole order. One rule for both endpoints.
		lines = self._lines((ITEM_A, 1, 50, None))
		for fields, granularity in (
			({"discount_percentage": 10}, bd.GRANULARITY_LINE),
			({"discount_percentage": 10, "max_amount": 3}, bd.GRANULARITY_ORDER),
			({"discount_type": "Amount", "discount_amount": 3}, bd.GRANULARITY_ORDER),
		):
			coupon = frappe._dict(
				{"coupon_code": "X", "discount_type": "Percentage", "apply_on": "Net Total", **fields}
			)
			decision = decide_coupon_vs_fnf({"items": lines}, coupon, customer=self.ctx.customer)
			self.assertEqual(decision.granularity, granularity)

	# ---- cart / invoice parity ---------------------------------------------------

	def test_calculated_coupon_header_kept_by_invoice(self):
		"""The header the POS takes from calculate_coupon_discount is what the invoice accepts."""
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
		for conflict in ("Skip the line", "Better of the two", "Add on top"):
			for n, fields in enumerate(coupons):
				code = f"FFCI-PAR-{conflict[:4].upper()}-{n}"
				self._coupon(code, promotion_conflict=conflict, **fields)
				self._check_header_parity(code, rule)

	def _check_header_parity(self, code, rule):
		with self.subTest(code=code):
			promoted = _line(self.ctx, ITEM_A, qty=1)
			promoted.update(
				{"discount_percentage": 10, "discount_amount": 5, "rate": 45, "pricing_rules": rule}
			)
			plain = _line(self.ctx, ITEM_B, qty=1)
			calculated = self._calculate(code, [promoted, plain])
			header = flt(calculated["discount"])

			row_a = self._row(ITEM_A, pct=10, rules=rule)
			for paid in (False, True):
				doc = self._invoice(
					[row_a.copy(), self._row(ITEM_B)], paid=paid, coupon_code=code, discount_amount=header
				)
				apply_friends_and_family_on_invoice(doc)  # paid online: must not throw
				self.assertEqual(flt(doc.discount_amount), header)
