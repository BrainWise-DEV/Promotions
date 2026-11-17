# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Task 6e — F&F is stripped from Sales Orders and Quotations (strip + log, never throw)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from posnext_promotions.promotions import ff_non_invoice
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	FIELD_FNF_PERCENTAGE,
)
from posnext_promotions.promotions.friends_and_family_log import (
	LOG_DOCTYPE,
	LOG_NON_INVOICE_STRIPPED,
)
from posnext_promotions.test_promotions import ITEM_A, ITEM_PRICES, _ctx

FNF = "friends_and_family"


class TestFFNonInvoice(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ctx = _ctx()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("ff_non_invoice")

	def tearDown(self):
		frappe.db.rollback(save_point="ff_non_invoice")

	def _line(self, pct=0, marked=False):
		plr = ITEM_PRICES[ITEM_A]
		line = {
			"item_code": ITEM_A,
			"qty": 1,
			"warehouse": self.ctx.warehouse,
			"delivery_date": add_days(nowdate(), 3),
			"price_list_rate": plr,
			"discount_percentage": pct,
			"rate": flt(plr * (1 - pct / 100.0), 2),
		}
		if marked:
			line.update({FIELD_DISCOUNT_SOURCE: FNF, FIELD_FNF_PERCENTAGE: pct})
			line[FIELD_FNF_AMOUNT] = flt(plr * pct / 100.0, 2)
		return line

	def _tax_rows(self):
		account = frappe.db.get_value(
			"Account", {"company": self.ctx.company, "is_group": 0, "account_type": "Tax"}, "name"
		)
		if not account:
			self.skipTest("no Tax account on the test company")
		return [
			{
				"charge_type": "On Net Total",
				"account_head": account,
				"description": "Test VAT",
				"rate": 10,
			}
		]

	def _doc(self, doctype, line, **header):
		doc = {
			"doctype": doctype,
			"company": self.ctx.company,
			"currency": self.ctx.currency,
			"selling_price_list": self.ctx.price_list,
			"transaction_date": nowdate(),
			"delivery_date": add_days(nowdate(), 3),
			"valid_till": add_days(nowdate(), 10),
			"items": [line],
			**header,
		}
		if doctype == "Quotation":
			doc.update({"quotation_to": "Customer", "party_name": self.ctx.customer})
		else:
			doc["customer"] = self.ctx.customer
		return frappe.get_doc(doc).insert()

	@staticmethod
	def _logs(doc):
		return frappe.get_all(
			LOG_DOCTYPE,
			filters={
				"reference_doctype": doc.doctype,
				"reference_name": doc.name,
				"log_type": LOG_NON_INVOICE_STRIPPED,
			},
			fields=["details", "customer"],
		)

	def test_sales_order_ff_line_stripped(self):
		doc = self._doc("Sales Order", self._line(30, marked=True))
		row = doc.items[0]
		self.assertEqual(flt(row.discount_percentage), 0)
		self.assertEqual(flt(row.rate), flt(row.price_list_rate))
		logs = self._logs(doc)
		self.assertEqual(len(logs), 1)
		self.assertIn('"lines": 1', logs[0].details)

	def test_quotation_ff_line_stripped(self):
		doc = self._doc("Quotation", self._line(30, marked=True))
		self.assertEqual(flt(doc.items[0].discount_percentage), 0)
		self.assertEqual(flt(doc.items[0].rate), flt(doc.items[0].price_list_rate))
		self.assertEqual(len(self._logs(doc)), 1)

	def _assert_priced_like_plain(self, doctype, with_taxes=False):
		extra = {"taxes": self._tax_rows()} if with_taxes else {}
		stripped = self._doc(doctype, self._line(30, marked=True), **extra)
		plain = self._doc(doctype, self._line(), **extra)
		row = stripped.items[0]
		self.assertEqual(flt(row.amount), flt(row.qty * row.price_list_rate))
		self.assertEqual(flt(row.net_amount), flt(plain.items[0].net_amount))
		self.assertEqual(flt(row.base_amount), flt(plain.items[0].base_amount))
		self.assertEqual(flt(stripped.net_total), flt(plain.net_total))
		self.assertEqual(flt(stripped.total_taxes_and_charges), flt(plain.total_taxes_and_charges))
		self.assertEqual(flt(stripped.grand_total), flt(plain.grand_total))
		if with_taxes:
			self.assertGreater(flt(stripped.total_taxes_and_charges), 0)

	def test_sales_order_totals_recalculated(self):
		self._assert_priced_like_plain("Sales Order")

	def test_quotation_totals_recalculated(self):
		self._assert_priced_like_plain("Quotation")

	def test_sales_order_taxes_recalculated(self):
		self._assert_priced_like_plain("Sales Order", with_taxes=True)

	def test_quotation_taxes_recalculated(self):
		self._assert_priced_like_plain("Quotation", with_taxes=True)

	def test_manual_discount_kept(self):
		doc = self._doc("Sales Order", self._line(30))
		self.assertEqual(flt(doc.items[0].discount_percentage), 30)
		self.assertEqual(self._logs(doc), [])

	def test_sales_order_without_ff_untouched(self):
		doc = self._doc("Sales Order", self._line())
		self.assertEqual(flt(doc.items[0].rate), ITEM_PRICES[ITEM_A])
		self.assertEqual(self._logs(doc), [])

	def test_percentage_or_amount_alone_marks_the_line(self):
		for key in (FIELD_FNF_PERCENTAGE, FIELD_FNF_AMOUNT):
			line = self._line(30)
			line[key] = 30
			doc = self._doc("Sales Order", line)
			self.assertEqual(flt(doc.items[0].discount_percentage), 0, key)

	def test_strip_ff_returns_count_and_removes_keys(self):
		doc = frappe.get_doc({"doctype": "Sales Order", "items": [self._line(30, marked=True), self._line()]})
		doc.posnext_fnf_total = 5
		self.assertEqual(ff_non_invoice.strip_ff(doc), 1)
		self.assertNotIn("posnext_fnf_total", vars(doc))
		for key in (FIELD_DISCOUNT_SOURCE, FIELD_FNF_AMOUNT, FIELD_FNF_PERCENTAGE):
			self.assertNotIn(key, vars(doc.items[0]))

	def test_no_usage_rows_for_sales_order(self):
		before = frappe.db.count("Friends and Family Usage")
		doc = self._doc("Sales Order", self._line(30, marked=True))
		doc.submit()
		self.assertEqual(frappe.db.count("Friends and Family Usage"), before)

	def test_hooks_declared(self):
		events = frappe.get_hooks("doc_events")
		for doctype in ("Sales Order", "Quotation"):
			validate = events[doctype]["validate"]
			validate = [validate] if isinstance(validate, str) else validate
			self.assertIn("posnext_promotions.promotions.ff_non_invoice.strip_ff", validate)
