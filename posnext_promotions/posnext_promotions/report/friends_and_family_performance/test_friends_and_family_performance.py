# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""PN-132 — Friends & Family performance report."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.posnext_promotions.report.friends_and_family_performance.friends_and_family_performance import (
	execute,
)
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	ensure_invoice_item_fields,
)
from posnext_promotions.promotions.friends_and_family_log import write_log

FROM, TO = "2031-03-01", "2031-04-30"
MARCH, APRIL = "2031-03-10", "2031-04-05"
COMPANY = "_Test FF Report Co"
OTHER_COMPANY = "_Test FF Report Other Co"
PROFILE = "_Test FF Report Profile"
CUST_A = "_Test FF Report Customer A"
CUST_B = "_Test FF Report Customer B"
PARENT_GROUP = "_Test FF Report Group"
CHILD_GROUP = "_Test FF Report Child Group"
OTHER_GROUP = "_Test FF Report Other Group"
BRAND = "_Test FF Report Brand"
ITEM_A = "_Test FF Report Item A"  # child group + brand
ITEM_B = "_Test FF Report Item B"  # other group, no brand
FNF = "friends_and_family"


class TestFriendsAndFamilyPerformance(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_invoice_item_fields()
		root = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""}) or "All Item Groups"
		for name, parent, is_group in (
			(PARENT_GROUP, root, 1),
			(CHILD_GROUP, PARENT_GROUP, 0),
			(OTHER_GROUP, root, 0),
		):
			if not frappe.db.exists("Item Group", name):
				frappe.get_doc(
					{"doctype": "Item Group", "item_group_name": name, "parent_item_group": parent, "is_group": is_group}
				).insert()
		if not frappe.db.exists("Brand", BRAND):
			frappe.get_doc({"doctype": "Brand", "brand": BRAND}).insert()
		for code, group, brand in ((ITEM_A, CHILD_GROUP, BRAND), (ITEM_B, OTHER_GROUP, None)):
			if not frappe.db.exists("Item", code):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": code,
						"item_name": code,
						"item_group": group,
						"brand": brand,
						"stock_uom": "Nos",
						"is_stock_item": 0,
					}
				).insert()

	def setUp(self):
		frappe.db.savepoint("pn132_case")
		self._seq = 0

	def tearDown(self):
		frappe.db.rollback(save_point="pn132_case")

	# ---- fixtures -----------------------------------------------------------

	def _invoice(
		self,
		lines,
		doctype="POS Invoice",
		customer=CUST_A,
		posting_date=MARCH,
		docstatus=1,
		is_return=0,
		return_against=None,
		is_consolidated=0,
		company=COMPANY,
		conversion_rate=1,
	):
		"""Bare submitted invoice. ``lines``: ``[(item_code, qty, base_net_amount, fnf_amount, source)]``."""
		self._seq += 1
		name = f"_T-PN132-{doctype[:3]}-{self._seq}"
		values = {
			"name": name,
			"customer": customer,
			"customer_name": customer,
			"company": company,
			"pos_profile": PROFILE,
			"posting_date": posting_date,
			"docstatus": docstatus,
			"is_return": is_return,
			"return_against": return_against,
			"conversion_rate": conversion_rate,
		}
		if frappe.db.has_column(doctype, "is_consolidated"):
			values["is_consolidated"] = is_consolidated
		cols = ", ".join(f"`{c}`" for c in values)
		frappe.db.sql(
			f"insert into `tab{doctype}` ({cols}) values ({', '.join(['%s'] * len(values))})",
			tuple(values.values()),
		)
		for idx, (item_code, qty, net, fnf, *source) in enumerate(lines, 1):
			group, brand = (CHILD_GROUP, BRAND) if item_code == ITEM_A else (OTHER_GROUP, None)
			frappe.db.sql(
				f"""insert into `tab{doctype} Item`
				(name, parent, parenttype, parentfield, idx, item_code, item_name, item_group, brand,
				 qty, base_net_amount, docstatus, `{FIELD_DISCOUNT_SOURCE}`, `{FIELD_FNF_AMOUNT}`)
				values (%s, %s, %s, 'items', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
				(
					f"{name}-{idx}",
					name,
					doctype,
					idx,
					item_code,
					item_code,
					group,
					brand,
					qty,
					net,
					docstatus,
					source[0] if source else FNF,
					fnf,
				),
			)
		return name

	def _log(self, log_type, invoice, doctype="POS Invoice", customer=CUST_A, posting_date=MARCH, **values):
		return write_log(
			log_type,
			reference_doctype=doctype,
			reference_name=invoice,
			customer=customer,
			company=COMPANY,
			pos_profile=PROFILE,
			posting_date=posting_date,
			**values,
		)

	def _base_data(self):
		"""Orders A (2 F&F lines + 1 plain), B (USD×2), return of A, and noise that must not count."""
		self.inv_a = self._invoice(
			[(ITEM_A, 1, 70, 30), (ITEM_B, 2, 140, 60), (ITEM_B, 1, 100, 0, None)]
		)
		self.inv_b = self._invoice(
			[(ITEM_B, 1, 100, 25)], doctype="Sales Invoice", customer=CUST_B, conversion_rate=2
		)
		self.ret_a = self._invoice(
			[(ITEM_A, -1, -70, -30)], is_return=1, return_against=self.inv_a, posting_date=APRIL
		)
		self._invoice([(ITEM_A, 1, 70, 30)], docstatus=2)  # cancelled
		self._invoice([(ITEM_A, 1, 70, 30)], docstatus=0)  # draft
		self._invoice([(ITEM_A, 1, 70, 30)], doctype="Sales Invoice", is_consolidated=1)  # merged POS
		self._invoice([(ITEM_A, 1, 70, 30)], posting_date="2031-05-02")  # outside period

	def _run(self, **filters):
		columns, data, _msg, chart, summary = execute({"from_date": FROM, "to_date": TO, **filters})
		return columns, data, chart, {s["label"]: s["value"] for s in summary}

	@staticmethod
	def _body(data):
		return [d for d in data if not d.get("is_total_row")]

	@staticmethod
	def _total(data):
		return next(d for d in data if d.get("is_total_row"))

	# ---- totals / reconciliation --------------------------------------------

	def test_totals_net_of_returns_and_reconcile_with_invoices(self):
		self._base_data()
		_cols, data, _chart, summary = self._run(view="Invoices", company=COMPANY)
		body = self._body(data)
		self.assertEqual({d["invoice"] for d in body}, {self.inv_a, self.inv_b, self.ret_a})
		by_inv = {d["invoice"]: d for d in body}
		self.assertEqual(flt(by_inv[self.inv_a]["discount"]), 90)
		self.assertEqual(flt(by_inv[self.inv_a]["net"]), 210)
		self.assertEqual(flt(by_inv[self.inv_a]["gross"]), 300)
		self.assertEqual(by_inv[self.inv_a]["lines"], 2)  # plain line not counted
		self.assertEqual(flt(by_inv[self.inv_b]["discount"]), 50)  # 25 USD × 2
		self.assertEqual(flt(by_inv[self.ret_a]["discount"]), -30)
		self.assertEqual(by_inv[self.ret_a]["is_return"], 1)

		total = self._total(data)
		self.assertEqual(flt(total["discount"]), 110)
		self.assertEqual(flt(total["net"]), 240)
		self.assertEqual(flt(total["gross"]), 350)
		self.assertEqual(summary["F&F Orders"], 2)
		self.assertEqual(summary["Returns"], 1)
		self.assertEqual(summary["Customers"], 2)
		self.assertEqual(flt(summary["F&F Discount (net)"]), 110)
		self.assertEqual(flt(summary["Net Sales"]), 240)
		self.assertEqual(flt(summary["Avg Discount / Order"]), 55)

		# Reconcile with the invoices directly.
		direct = 0.0
		for doctype in ("POS Invoice", "Sales Invoice"):
			direct += flt(
				frappe.db.sql(
					f"""select sum(i.`{FIELD_FNF_AMOUNT}` * p.conversion_rate)
					from `tab{doctype} Item` i join `tab{doctype}` p on p.name = i.parent
					where p.name in %s""",
					((self.inv_a, self.inv_b, self.ret_a),),
				)[0][0]
			)
		self.assertEqual(flt(total["discount"]), direct)

	def test_summary_by_period(self):
		self._base_data()
		_cols, data, chart, _s = self._run(company=COMPANY)
		body = {d["period"]: d for d in self._body(data)}
		self.assertEqual(set(body), {"2031-03", "2031-04"})
		self.assertEqual(flt(body["2031-03"]["discount"]), 140)
		self.assertEqual(body["2031-03"]["orders"], 2)
		self.assertEqual(flt(body["2031-04"]["discount"]), -30)
		self.assertEqual(body["2031-04"]["returns"], 1)
		self.assertEqual(flt(self._total(data)["discount"]), 110)
		self.assertEqual(self._total(data)["customers"], 2)  # unique, not summed
		self.assertEqual(chart["data"]["labels"], ["2031-03", "2031-04"])
		_cols, daily, _c, _s = self._run(company=COMPANY, periodicity="Daily")
		self.assertIn(MARCH, {d["period"] for d in self._body(daily)})

	def test_filters_company_customer_profile(self):
		self._base_data()
		self._invoice([(ITEM_A, 1, 70, 30)], company=OTHER_COMPANY)
		self.assertEqual(flt(self._run(company=COMPANY)[3]["F&F Discount (net)"]), 110)
		self.assertEqual(flt(self._run(company=OTHER_COMPANY)[3]["F&F Discount (net)"]), 30)
		self.assertEqual(flt(self._run(company=COMPANY, customer=CUST_B)[3]["F&F Discount (net)"]), 50)
		self.assertEqual(flt(self._run(company=COMPANY, pos_profile="Nope")[3]["F&F Discount (net)"]), 0)

	def test_filters_item_group_tree_and_brand(self):
		self._base_data()
		# Parent group includes its child group (ITEM_A): 30 - 30 return.
		by_group = self._run(company=COMPANY, item_group=PARENT_GROUP)[3]
		self.assertEqual(flt(by_group["F&F Discount (net)"]), 0)
		self.assertEqual(by_group["F&F Orders"], 1)
		self.assertEqual(flt(self._run(company=COMPANY, item_group=OTHER_GROUP)[3]["F&F Discount (net)"]), 110)
		self.assertEqual(flt(self._run(company=COMPANY, brand=BRAND)[3]["Net Sales"]), 0)  # 70 - 70

	def test_top_items(self):
		self._base_data()
		_cols, data, chart, _s = self._run(view="Items", company=COMPANY)
		body = self._body(data)
		self.assertEqual([d["item_code"] for d in body], [ITEM_B, ITEM_A])  # 110 vs 0
		self.assertEqual(flt(body[0]["discount"]), 110)
		self.assertEqual(flt(body[0]["qty"]), 3)
		self.assertEqual(chart["data"]["labels"][0], ITEM_B)
		_cols, top1, _c, _s = self._run(view="Items", company=COMPANY, top_n=1)
		self.assertEqual(len(self._body(top1)), 1)
		self.assertEqual(flt(self._total(top1)["discount"]), 110)  # total still covers all items

	def test_customers_who_hit_caps(self):
		self._base_data()
		self._log("Cap Hit", self.inv_b, doctype="Sales Invoice", customer=CUST_B, cap_hit="monthly")
		self._log("Cap Hit", self.inv_b, doctype="Sales Invoice", customer=CUST_B, cap_hit="monthly", item_code=ITEM_B)
		cancelled = self._log("Cap Hit", self.inv_a, cap_hit="order")
		frappe.db.set_value("Friends and Family Log", cancelled.name, "is_cancelled", 1)

		_cols, data, _chart, summary = self._run(view="Customers", company=COMPANY)
		body = {d["customer"]: d for d in self._body(data)}
		self.assertEqual(body[CUST_B]["cap_hits"], 1)  # one order, two entries
		self.assertEqual(body[CUST_B]["cap_types"], "monthly")
		self.assertEqual(body[CUST_A]["cap_hits"], 0)  # cancelled entry ignored
		self.assertEqual(summary["Customers Who Hit Caps"], 1)
		_cols, only, _c, _s = self._run(view="Customers", company=COMPANY, only_cap_hits=1)
		self.assertEqual([d["customer"] for d in self._body(only)], [CUST_B])

	def test_fnf_vs_coupon_and_promotion(self):
		self._base_data()
		# A: F&F beat a line coupon on two lines (one order).
		self._log("Discount Applied", self.inv_a, item_code=ITEM_A, competing_source="coupon", discount_source=FNF)
		self._log("Discount Applied", self.inv_a, item_code=ITEM_B, competing_source="coupon", discount_source=FNF)
		# B: coupon won a line.
		self._log(
			"Comparison Lost", self.inv_b, doctype="Sales Invoice", customer=CUST_B, item_code=ITEM_B,
			discount_source="coupon", competing_source=FNF,
		)
		# C: order coupon beat F&F (order entry + per-line entries count once).
		inv_c = self._invoice([(ITEM_B, 1, 100, 0)])
		self._log("Order Comparison", inv_c, discount_source="order_coupon", competing_source=FNF)
		self._log("Comparison Lost", inv_c, item_code=ITEM_B, discount_source="order_coupon", reason="order_discount_better")
		# D: F&F beat a promotion; E: cancelled invoice ignored.
		inv_d = self._invoice([(ITEM_A, 1, 70, 30)])
		self._log("Discount Applied", inv_d, item_code=ITEM_A, competing_source="promotion", discount_source=FNF)
		cancelled = self._log("Comparison Lost", inv_d, item_code=ITEM_B, discount_source="coupon")
		frappe.db.set_value("Friends and Family Log", cancelled.name, "is_cancelled", 1)

		_cols, data, chart, summary = self._run(view="Comparisons", company=COMPANY)
		rows = {d["competitor"]: d for d in data}
		self.assertEqual(rows["Coupon"]["fnf_won"], 1)
		self.assertEqual(rows["Coupon"]["competitor_won"], 2)
		self.assertEqual(rows["Coupon"]["fnf_won_lines"], 2)
		self.assertEqual(rows["Coupon"]["competitor_won_lines"], 1)
		self.assertAlmostEqual(rows["Coupon"]["fnf_win_rate"], 100 / 3, places=2)
		self.assertEqual(rows["Promotion"]["fnf_won"], 1)
		self.assertEqual(rows["Promotion"]["competitor_won"], 0)
		self.assertEqual(summary["F&F Won vs Coupon"], 1)
		self.assertEqual(summary["Coupon Won vs F&F"], 2)
		self.assertEqual(len(chart["data"]["labels"]), 3)

		# Brand filter: only ITEM_A lines (A's first line, D) — and order entries of matching invoices.
		rows = {d["competitor"]: d for d in self._run(view="Comparisons", company=COMPANY, brand=BRAND)[1]}
		self.assertEqual(rows["Coupon"]["fnf_won"], 1)
		self.assertEqual(rows["Coupon"]["competitor_won"], 0)
		self.assertEqual(rows["Promotion"]["fnf_won"], 1)

	# ---- misc ----------------------------------------------------------------

	def test_requires_valid_period(self):
		with self.assertRaises(frappe.ValidationError):
			execute({"from_date": None, "to_date": TO})
		with self.assertRaises(frappe.ValidationError):
			execute({"from_date": TO, "to_date": FROM})

	def test_empty_period(self):
		_cols, data, _chart, summary = self._run(company="_Nobody")
		self.assertEqual(data, [])
		self.assertEqual(summary["F&F Orders"], 0)

	def test_report_permissions(self):
		report = frappe.get_doc("Report", "Friends and Family Performance")
		self.assertEqual(report.report_type, "Script Report")
		self.assertEqual({r.role for r in report.roles}, {"System Manager", "Sales Manager", "Accounts Manager"})
		perms = {p.role: p for p in frappe.get_meta(report.ref_doctype).permissions}
		self.assertTrue(perms["Accounts Manager"].report)
		self.assertNotIn("Sales User", perms)
