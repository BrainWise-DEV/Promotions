# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family usage ledger: rows on submit, row lock, cap-breach policy, backfill."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_months, flt, get_first_day, getdate, nowdate

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_ledger
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
	apply_friends_and_family_on_invoice,
	compute_friends_and_family_discounts,
)
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	cap_conversion_rate,
	ensure_invoice_item_fields,
)
from posnext_promotions.promotions.friends_and_family_eligibility import FLAG_FIELD, ensure_customer_flag_field
from posnext_promotions.promotions.friends_and_family_testing import (
	approved_domain_rows,
	ensure_named_pricing_rule,
	give_verified_email,
)

FF_GROUP = "_Test FF Ledger Customer Group"
CUSTOMER = "_Test FF Ledger Customer"
LOCK_CUSTOMER = "_Test FF Ledger Lock Customer"
LOCK_CUSTOMER_2 = "_Test FF Ledger Lock Customer 2"
ITEM = "_Test FF Ledger Item"
PREFIX = "_T-FFL-"
DATE = "2026-11-10"
PERIOD = "2026-11"
FNF = DISCOUNT_SOURCE_FRIENDS_AND_FAMILY


def _company():
	return frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {})


def _other_company():
	"""A second company on the site (caps are per company)."""
	return frappe.db.get_value("Company", {"name": ["!=", _company()]}, "name", order_by="name asc")


def _ensure_fixtures():
	ensure_customer_flag_field()
	ensure_invoice_item_fields()
	root_cg = frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ""}) or (
		"All Customer Groups"
	)
	if not frappe.db.exists("Customer Group", FF_GROUP):
		frappe.get_doc(
			{"doctype": "Customer Group", "customer_group_name": FF_GROUP, "parent_customer_group": root_cg, "is_group": 0}
		).insert()
	for name in (CUSTOMER, LOCK_CUSTOMER, LOCK_CUSTOMER_2):
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": FF_GROUP}).insert()
		frappe.db.set_value("Customer", name, {FLAG_FIELD: 0, "disabled": 0})
		give_verified_email(name)
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_name": ITEM,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}) or "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()


def _usage_values(invoice, base_amount, customer=CUSTOMER, period=PERIOD):
	return {
		"customer": customer,
		"company": _company(),
		"invoice_doctype": "POS Invoice",
		"invoice": invoice,
		"invoice_item": f"{invoice}-1",
		"posting_date": DATE,
		"period_key": period,
		"amount": base_amount,
		"base_amount": base_amount,
	}


class TestFFLedger(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_fixtures()

	def setUp(self):
		frappe.db.savepoint("ffl_case")
		self._seq = 0
		self._configure()

	def tearDown(self):
		frappe.db.rollback(save_point="ffl_case")

	# ---- helpers ------------------------------------------------------------

	def _configure(self, **values):
		doc = frappe.get_single("Friends and Family Settings")
		doc.update(
			{
				"enabled": 1,
				"approved_domains": approved_domain_rows(),
				"discount_percentage": 30,
				"discount_scope": "Entire Cart",
				"start_date": None,
				"end_date": None,
				"applicable_brands": [],
				"applicable_item_groups": [],
				"applicable_items": [],
				"excluded_brands": [],
				"excluded_items": [],
				"excluded_item_groups": [],
				"eligibility_basis": "Customer Group",
				"eligible_customer_groups": [{"customer_group": FF_GROUP}],
				"max_discount_per_order": 0,
				"max_discount_per_customer_month": 0,
				"on_cap_breach_at_submit": "Allow and log",
			}
		)
		doc.update(values)
		doc.save()
		return doc

	def _name(self):
		self._seq += 1
		return f"{PREFIX}{frappe.generate_hash(length=6)}-{self._seq}"

	def _row(self, name, rate=100, qty=1, fnf_rate=None, **extra):
		"""An invoice row. ``fnf_rate`` = net rate of a row F&F already discounted."""
		row = frappe._dict(
			{
				"name": name,
				"item_code": ITEM,
				"qty": qty,
				"price_list_rate": rate,
				"rate": rate,
				"discount_percentage": 0,
				"discount_amount": 0,
				**extra,
			}
		)
		if fnf_rate is not None:
			row.rate = fnf_rate
			row.discount_percentage = (rate - fnf_rate) / rate * 100
			row[FIELD_DISCOUNT_SOURCE] = FNF
			row[FIELD_FNF_AMOUNT] = bd.money((rate - fnf_rate) * qty)
		return row

	def _invoice(self, rows=None, **header):
		name = header.pop("name", None) or self._name()
		rows = rows if rows is not None else [self._row(f"{name}-1")]
		return frappe._dict(
			{
				"doctype": "POS Invoice",
				"name": name,
				"company": _company(),
				"currency": frappe.get_cached_value("Company", _company(), "default_currency"),
				"conversion_rate": 1,
				"customer": CUSTOMER,
				"posting_date": DATE,
				"is_pos": 1,
				"items": rows,
				"additional_discount_percentage": 0,
				"discount_amount": 0,
				**header,
			}
		)

	def _fnf_invoice(self, rate=100, fnf_rate=70, **header):
		name = header.pop("name", None) or self._name()
		return self._invoice([self._row(f"{name}-1", rate=rate, fnf_rate=fnf_rate)], name=name, **header)

	def _rows(self, invoice):
		return frappe.get_all(
			ff_ledger.LEDGER_DOCTYPE,
			filters={"invoice": invoice},
			fields=["*"],
			order_by="invoice_item",
		)

	def _usage_row(self, base_amount, customer=CUSTOMER, period=PERIOD):
		name = self._name()
		ff_ledger.record_rows([_usage_values(name, base_amount, customer, period)])
		return name

	def _bare_invoice(self, doctype="POS Invoice", posting_date=DATE, rows=(), **values):
		"""Raw submitted invoice in the DB (no GL / stock side effects)."""
		name = values.pop("name", None) or self._name()
		header = {
			"name": name,
			"customer": CUSTOMER,
			"company": _company(),
			"currency": frappe.get_cached_value("Company", _company(), "default_currency"),
			"conversion_rate": 1,
			"posting_date": posting_date,
			"docstatus": 1,
			"is_pos": 1,
			**values,
		}
		cols = ", ".join(f"`{c}`" for c in header)
		frappe.db.sql(
			f"insert into `tab{doctype}` ({cols}) values ({', '.join(['%s'] * len(header))})", tuple(header.values())
		)
		for idx, (rate, fnf_rate, qty) in enumerate(rows, 1):
			frappe.db.sql(
				f"""insert into `tab{doctype} Item`
				(name, parent, parenttype, parentfield, idx, item_code, docstatus, qty, price_list_rate, rate,
				`{FIELD_DISCOUNT_SOURCE}`, `{FIELD_FNF_AMOUNT}`)
				values (%s, %s, %s, 'items', %s, %s, 1, %s, %s, %s, %s, %s)""",
				(f"{name}-{idx}", name, doctype, idx, ITEM, qty, rate, fnf_rate, FNF, (rate - fnf_rate) * qty),
			)
		return name

	# ---- submit / cancel ----------------------------------------------------

	def test_submit_writes_rows(self):
		name = self._name()
		doc = self._invoice(
			[self._row(f"{name}-1", fnf_rate=70), self._row(f"{name}-2", rate=50, qty=2, fnf_rate=35), self._row(f"{name}-3")],
			name=name,
		)
		ff_ledger.on_submit(doc)
		rows = self._rows(name)
		self.assertEqual([r.invoice_item for r in rows], [f"{name}-1", f"{name}-2"])
		self.assertEqual([flt(r.amount) for r in rows], [30, 30])
		self.assertTrue(all(r.customer == CUSTOMER and r.period_key == PERIOD and not r.is_cancelled for r in rows))
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 60)

	def test_submit_hook_idempotent(self):
		doc = self._fnf_invoice()
		ff_ledger.on_submit(doc)
		ff_ledger.on_submit(doc)
		self.assertEqual(len(self._rows(doc.name)), 1)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 30)

	def test_cancel_reverses(self):
		doc = self._fnf_invoice()
		ff_ledger.on_submit(doc)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 30)
		ff_ledger.on_cancel(doc)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 0)
		self.assertTrue(all(r.is_cancelled for r in self._rows(doc.name)))
		ff_ledger.on_cancel(doc)  # idempotent: nothing left to subtract
		self.assertEqual(self._total(), 0)

	def _total(self, customer=CUSTOMER, period=PERIOD):
		return flt(frappe.db.get_value(ff_ledger.TOTAL_DOCTYPE, ff_ledger.total_name(customer, _company(), period), "base_amount"))

	def test_counter_follows_submit_return_and_cancel(self):
		doc = self._fnf_invoice()
		ff_ledger.on_submit(doc)
		ff_ledger.on_submit(doc)  # a re-run adds nothing
		self.assertEqual(self._total(), 30)
		original = self._bare_invoice(rows=[(100, 70, 1)])
		name = self._name()
		ret = self._invoice([self._row(f"{name}-1", qty=-1, fnf_rate=70)], name=name, is_return=1, return_against=original)
		ff_ledger.on_submit(ret)
		self.assertEqual(self._total(), 0)
		ff_ledger.on_cancel(ret)
		self.assertEqual(self._total(), 30)

	def _ledger_sum(self, customer=CUSTOMER, period=PERIOD):
		return flt(
			frappe.db.get_value(
				ff_ledger.LEDGER_DOCTYPE,
				{"customer": customer, "company": _company(), "period_key": period, "is_cancelled": 0},
				"sum(base_amount)",
			)
		)

	def test_usage_never_below_zero_and_counter_equals_ledger(self):
		# A return of an invoice the ledger never recorded must not create allowance.
		name = self._name()
		ret = self._invoice(
			[self._row(f"{name}-1", qty=-1, fnf_rate=70)], name=name, is_return=1, return_against="_T-FFL-unknown"
		)
		ff_ledger.on_submit(ret)
		self.assertEqual(flt(self._rows(name)[0].amount), -30)
		self.assertEqual(self._total(), -30)  # stored raw: equals the ledger
		self.assertEqual(self._total(), self._ledger_sum())
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 0)  # read clamps
		# The return's credit gives no allowance at submit either.
		self._configure(max_discount_per_customer_month=30, on_cap_breach_at_submit="Block submit")
		with self.assertRaises(frappe.ValidationError):
			ff_ledger.on_submit(self._fnf_invoice(rate=200, fnf_rate=140))  # 60 > 30 even with -30 stored
		# Cancelling the return: counter and ledger both back to 0, and a rebuild agrees.
		ff_ledger.on_cancel(ret)
		self.assertEqual(self._total(), 0)
		self.assertEqual(self._total(), self._ledger_sum())
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 0)
		ff_ledger.rebuild_totals(PERIOD)
		self.assertEqual(self._total(), 0)

	def test_cap_tolerates_one_precision_unit(self):
		self._configure(max_discount_per_order=29.99, on_cap_breach_at_submit="Block submit")
		ff_ledger.on_submit(self._fnf_invoice())  # 30.00 vs 29.99: rounding, not a breach
		with self.assertRaises(frappe.ValidationError):
			ff_ledger.on_submit(self._fnf_invoice(fnf_rate=69.98))  # 30.02

	def test_ledger_amount_from_persisted_prices(self):
		# The stored stamp says 25; the row really charges 100 - 70 = 30. The ledger records 30.
		doc = self._fnf_invoice()
		doc["items"][0][FIELD_FNF_AMOUNT] = 25
		ff_ledger.on_submit(doc)
		self.assertEqual(flt(self._rows(doc.name)[0].amount), 30)

	def test_shared_allowance_between_invoices(self):
		self._configure(max_discount_per_customer_month=1000, discount_percentage=50)
		a_name, b_name = self._name(), self._name()
		a = self._invoice([self._row(f"{a_name}-1", rate=1600)], name=a_name)
		apply_friends_and_family_on_invoice(a)
		self.assertEqual(flt(a["items"][0].rate), 800)
		ff_ledger.on_submit(a)
		b = self._invoice([self._row(f"{b_name}-1", rate=1000)], name=b_name)  # 500 uncapped
		payload = apply_friends_and_family_on_invoice(b)
		saved = (flt(b["items"][0].price_list_rate) - flt(b["items"][0].rate)) * flt(b["items"][0].qty)
		self.assertLessEqual(bd.money(saved), 200)
		self.assertEqual(bd.money(saved), 200)
		self.assertEqual(payload["caps"]["monthly_used"], 800)

	def test_block_policy_throws(self):
		self._configure(max_discount_per_customer_month=100, on_cap_breach_at_submit="Block submit")
		doc = self._invoice()
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc["items"][0].rate), 70)  # usage 0 at validate: full F&F
		self._usage_row(80)  # another submit lands between validate and submit
		with self.assertRaises(frappe.ValidationError):
			ff_ledger.on_submit(doc)
		self.assertEqual(self._rows(doc.name), [])

	def test_block_policy_per_order_cap(self):
		self._configure(max_discount_per_order=20, on_cap_breach_at_submit="Block submit")
		doc = self._fnf_invoice()  # 30 of F&F without validate (e.g. a tampered row)
		with self.assertRaises(frappe.ValidationError):
			ff_ledger.on_submit(doc)

	def test_allow_policy_logs(self):
		self._configure(max_discount_per_customer_month=100)
		doc = self._invoice()
		apply_friends_and_family_on_invoice(doc)
		self._usage_row(80)
		ff_ledger.on_submit(doc)
		self.assertEqual(len(self._rows(doc.name)), 1)
		logs = frappe.get_all(
			"Friends and Family Log",
			filters={"reference_name": doc.name, "reason": ff_ledger.REASON_BREACH},
			fields=["log_type", "cap_hit", "capped", "discount_amount"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].log_type, "Cap Hit")
		self.assertEqual(logs[0].cap_hit, "monthly")
		self.assertEqual(flt(logs[0].discount_amount), 30)

	def test_no_breach_no_log(self):
		self._configure(max_discount_per_customer_month=100)
		doc = self._fnf_invoice()
		ff_ledger.on_submit(doc)
		self.assertFalse(frappe.db.exists("Friends and Family Log", {"reference_name": doc.name}))

	def test_return_next_month_credits_original_period(self):
		original = self._bare_invoice(posting_date="2026-11-20", rows=[(100, 70, 1)])
		name = self._name()
		ret = self._invoice(
			[self._row(f"{name}-1", qty=-1, fnf_rate=70)],
			name=name,
			is_return=1,
			return_against=original,
			posting_date="2026-12-03",
		)
		ff_ledger.on_submit(ret)
		rows = self._rows(name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(flt(rows[0].amount), -30)
		self.assertEqual(rows[0].period_key, "2026-11")
		self.assertEqual(str(rows[0].posting_date), "2026-12-03")
		self.assertTrue(rows[0].is_return)

	def test_non_pos_sales_invoice_ignored(self):
		doc = self._fnf_invoice(doctype="Sales Invoice", is_pos=0)
		ff_ledger.on_submit(doc)
		self.assertEqual(self._rows(doc.name), [])
		consolidated = self._fnf_invoice(doctype="Sales Invoice", is_pos=1, is_consolidated=1)
		ff_ledger.on_submit(consolidated)
		self.assertEqual(self._rows(consolidated.name), [])

	def test_ledger_row_has_company_and_base_amount(self):
		doc = self._fnf_invoice(rate=33.33, fnf_rate=23.33, conversion_rate=1.37)
		ff_ledger.on_submit(doc)
		row = self._rows(doc.name)[0]
		self.assertEqual(row.company, doc.company)
		self.assertEqual(row.currency, doc.currency)
		self.assertEqual(flt(row.amount), 10)
		self.assertEqual(flt(row.base_amount), bd.money(flt(row.amount) * 1.37))

	def test_caps_compare_in_company_currency(self):
		# Company currency IQD, invoice USD at 1300: 26000 IQD cap, 13000 IQD used → 10 USD left.
		self._configure(max_discount_per_customer_month=26000)
		self._usage_row(13000)
		doc = self._invoice(currency="USD", conversion_rate=1300)
		payload = apply_friends_and_family_on_invoice(doc)
		row = doc["items"][0]
		self.assertEqual(bd.money((flt(row.price_list_rate) - flt(row.rate)) * flt(row.qty)), 10)
		self.assertEqual(payload["caps"]["allowance"], 10)
		self.assertEqual(payload["caps"]["monthly_remaining"], 13000)
		self.assertEqual(payload["caps"]["monthly_remaining_after"], 0)
		self.assertIn("26,000.00", payload["message_en"])  # the cap, in company currency
		self._configure(max_discount_per_customer_month=26000, on_cap_breach_at_submit="Block submit")
		ff_ledger.on_submit(doc)  # 10 USD = 13000 IQD: exactly the allowance, no breach
		self.assertEqual(flt(self._rows(doc.name)[0].base_amount), 13000)

	def test_period_key_from_posting_date(self):
		posting_date = add_months(get_first_day(nowdate()), -1)  # last month, whatever today is
		doc = self._fnf_invoice(posting_date=posting_date)
		ff_ledger.on_submit(doc)
		period = self._rows(doc.name)[0].period_key
		self.assertEqual(period, getdate(posting_date).strftime("%Y-%m"))
		self.assertNotEqual(period, ff_ledger.period_key(nowdate()))

	# ---- per company (Ruling 32) ---------------------------------------------

	def test_usage_is_counted_per_company(self):
		other = _other_company()
		self._usage_row(80)  # default company
		name = self._name()
		ff_ledger.record_rows([{**_usage_values(name, 50), "company": other}])
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 80)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, other, PERIOD), 50)
		self.assertNotEqual(ff_ledger.total_name(CUSTOMER, _company(), PERIOD), ff_ledger.total_name(CUSTOMER, other, PERIOD))
		with self.assertRaises(frappe.ValidationError):
			ff_ledger.get_usage(CUSTOMER, None, PERIOD)

	def test_other_companys_usage_does_not_count_toward_this_cap(self):
		other = _other_company()
		other_currency = frappe.get_cached_value("Company", other, "default_currency")
		self._configure(max_discount_per_customer_month=100, on_cap_breach_at_submit="Block submit")
		self._usage_row(80)  # 80 used in the default company
		mine = self._invoice()
		apply_friends_and_family_on_invoice(mine)
		self.assertEqual(flt(mine["items"][0].rate), 80)  # default company: 20 left
		theirs = self._invoice(company=other, currency=other_currency)
		payload = apply_friends_and_family_on_invoice(theirs)
		self.assertEqual(flt(theirs["items"][0].rate), 70)  # other company: full 30
		self.assertEqual(payload["caps"]["monthly_used"], 0)
		ff_ledger.on_submit(theirs)  # no breach: the default company's 80 is not this company's
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, other, PERIOD), 30)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 80)
		ff_ledger.on_cancel(theirs)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, other, PERIOD), 0)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 80)

	def test_backfill_rebuilds_counters_per_company(self):
		other = _other_company()
		period = "2031-04"
		self._bare_invoice(posting_date="2031-04-10", rows=[(100, 70, 1)])
		self._bare_invoice(
			posting_date="2031-04-11",
			rows=[(100, 60, 1)],
			company=other,
			currency=frappe.get_cached_value("Company", other, "default_currency"),
		)
		with patch.object(frappe.local.db, "commit"):
			self.assertEqual(ff_ledger.backfill(period), {"invoices": 2, "rows": 2})
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), period), 30)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, other, period), 40)

	# ---- server-derived competitor amounts ----------------------------------

	def test_unit_competitor_value_follows_erpnext_pricing(self):
		# No Pricing Rule: ERPNext keeps the rate, the percentage is only a label.
		manual = self._row("m-1", discount_percentage=40)
		manual.rate = 95
		self.assertEqual(bd.line_discount_value(manual, bd.AMOUNT_BASIS_UNIT), 5)
		# Pricing Rule with a percentage: ERPNext re-prices from the percentage.
		ruled = self._row("r-1", discount_percentage=40, pricing_rules="_Any Rule")
		self.assertEqual(bd.line_discount_value(ruled, bd.AMOUNT_BASIS_UNIT), 40)
		# One valuation for cart and invoice (Ruling 28).
		self.assertEqual(bd.line_discount_value(manual, bd.AMOUNT_BASIS_LINE), 5)
		# Rate at list price: the cart's normal shape, the percentage carries the discount.
		cart = self._row("c-1", discount_percentage=10)
		self.assertEqual(bd.line_discount_value(cart, bd.AMOUNT_BASIS_LINE), 10)
		self.assertEqual(bd.line_discount_value(cart, bd.AMOUNT_BASIS_UNIT), 10)
		# A manual amount at list rate still counts.
		amount = self._row("a-1", discount_amount=15)
		self.assertEqual(bd.line_discount_value(amount, bd.AMOUNT_BASIS_LINE), 15)

	def test_invoice_compares_what_the_row_really_charges(self):
		name = self._name()
		row = self._row(f"{name}-1", discount_percentage=40)
		row.rate = 95  # a 40% label on a 5% price
		doc = self._invoice([row], name=name)
		payload = apply_friends_and_family_on_invoice(doc)
		self.assertTrue(payload["items"][0]["applies"])
		self.assertEqual(flt(row.rate), 70)
		ff_ledger.on_submit(doc)
		self.assertEqual(flt(self._rows(name)[0].amount), 30)

	def _parity_winner(self, make_row):
		"""Winner for the same line on the cart and on the invoice."""
		cart = compute_friends_and_family_discounts(CUSTOMER, [make_row("cart-1")], DATE)
		name = self._name()
		invoice = apply_friends_and_family_on_invoice(self._invoice([make_row(f"{name}-1")], name=name))
		return cart["items"][0]["applies"], invoice["items"][0]["applies"]

	def test_cart_and_invoice_value_a_line_the_same(self):
		rule = ensure_named_pricing_rule("_Test FF Ledger Parity Rule")

		def ruled(name):  # Pricing Rule 40%, rate not yet re-priced: the percentage counts
			return self._row(name, discount_percentage=40, pricing_rules=rule, discount_source="auto_discount")

		def labelled(name):  # 40% label on a 5% price, no rule: the rate counts
			row = self._row(name, discount_percentage=40)
			row.rate = 95
			return row

		def list_rate(name):  # 10% at list rate, no rule (cart shape): the percentage counts
			return self._row(name, discount_percentage=10)

		self.assertEqual(self._parity_winner(ruled), (False, False))  # promotion 40 > F&F 30
		self.assertEqual(self._parity_winner(labelled), (True, True))  # F&F 30 > 5
		self.assertEqual(self._parity_winner(list_rate), (True, True))  # F&F 30 > 10

	# ---- cart currency (Ruling 29) -----------------------------------------

	def test_cap_conversion_rate(self):
		company = _company()
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		other = "USD" if company_currency != "USD" else "EUR"
		profile = frappe._dict(company=company, currency=company_currency)
		target = "erpnext.setup.utils.get_exchange_rate"
		with patch("frappe.get_cached_value", side_effect=self._cached(profile)), patch(target) as rate:
			self.assertEqual(cap_conversion_rate("P"), 1.0)
			rate.assert_not_called()
			profile.currency = other
			rate.return_value = 1300
			self.assertEqual(cap_conversion_rate("P", "2026-11-10"), 1300)
			rate.assert_called_once_with(other, company_currency, "2026-11-10")
			rate.return_value = None
			with self.assertRaises(frappe.ValidationError):
				cap_conversion_rate("P", "2026-11-10")
		with self.assertRaises(frappe.ValidationError):
			cap_conversion_rate(None)  # caps set but no profile: never assume 1

	@staticmethod
	def _cached(profile):
		real = frappe.get_cached_value

		def fake(doctype, name, field, *args, **kwargs):
			if doctype == "POS Profile":
				return profile
			return real(doctype, name, field, *args, **kwargs)

		return fake

	def test_cart_cap_in_company_currency(self):
		self._configure(max_discount_per_customer_month=26000)
		self._usage_row(13000)
		with patch(
			"posnext_promotions.promotions.friends_and_family_apply.cart_cap_basis", return_value=(_company(), 1300)
		) as rate:
			payload = compute_friends_and_family_discounts(CUSTOMER, [self._row("c-1")], DATE, pos_profile="P")
		rate.assert_called_once_with("P", DATE)
		self.assertEqual(payload["caps"]["allowance"], 10)  # 13000 left / 1300
		self.assertEqual(payload["items"][0]["discount_amount"], 10)
		# No caps: no exchange-rate lookup on the cart.
		self._configure()
		with patch("posnext_promotions.promotions.friends_and_family_apply.cart_cap_basis") as rate:
			compute_friends_and_family_discounts(CUSTOMER, [self._row("c-1")], DATE, pos_profile="P")
		rate.assert_not_called()

	# ---- backfill -----------------------------------------------------------

	def test_backfill_current_month(self):
		period = "2031-01"  # no real invoices there
		name = self._bare_invoice(posting_date="2031-01-15", rows=[(100, 70, 1), (50, 35, 2)])
		self._bare_invoice(posting_date="2031-01-16", rows=[(100, 70, 1)], docstatus=2)  # cancelled
		self._bare_invoice(doctype="Sales Invoice", posting_date="2031-01-16", rows=[(100, 70, 1)], is_pos=0)
		with patch.object(frappe.local.db, "commit"):
			self.assertEqual(ff_ledger.backfill(period, dry_run=True), {"invoices": 1, "rows": 2})
			self.assertEqual(self._rows(name), [])
			self.assertEqual(ff_ledger.backfill(period, batch_size=1), {"invoices": 1, "rows": 2})
			self.assertEqual([flt(r.amount) for r in self._rows(name)], [30, 30])
			self.assertEqual(ff_ledger.backfill(period), {"invoices": 0, "rows": 0})
			self.assertEqual(ff_ledger.backfill(period, dry_run=True), {"invoices": 0, "rows": 0})
			self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), period), 60)
			# Counters are recomputed from the ledger, never added to twice.
			frappe.db.set_value(
				ff_ledger.TOTAL_DOCTYPE, ff_ledger.total_name(CUSTOMER, _company(), period), "base_amount", 999,
				update_modified=False,
			)
			ff_ledger.backfill(period)
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), period), 60)


class TestFFLedgerSchema(FrappeTestCase):
	"""DDL commits, so this class keeps no savepoint fixtures and restores the key itself."""

	def test_backfill_patch_adds_key_first(self):
		from posnext_promotions.patches.v3_0_0 import build_ff_usage_ledger

		table = f"tab{ff_ledger.LEDGER_DOCTYPE}"
		try:
			frappe.db.sql_ddl(f"alter table `{table}` drop index `{ff_ledger.UNIQUE_KEY}`")
			self.assertFalse(frappe.db.has_index(table, ff_ledger.UNIQUE_KEY))
			with patch.object(frappe.local.db, "commit"):
				build_ff_usage_ledger.execute()
				self.assertTrue(frappe.db.has_index(table, ff_ledger.UNIQUE_KEY))
				count = frappe.db.count(ff_ledger.LEDGER_DOCTYPE)
				build_ff_usage_ledger.execute()
				self.assertEqual(frappe.db.count(ff_ledger.LEDGER_DOCTYPE), count)
		finally:
			frappe.db.rollback()
			ff_ledger.ensure_ledger_unique_key()
		self.assertTrue(frappe.db.has_index(table, ff_ledger.UNIQUE_KEY))


class TestFFLedgerLock(FrappeTestCase):
	"""Two DB connections, as two tills submitting at the same time."""

	PERIODS = ("2031-02", "2031-03", "2031-05", "2031-06", "2031-07")

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_fixtures()
		frappe.db.commit()  # the second connection must see the customer rows

	def _cleanup(self):
		for conn in (self.primary_connection, self.secondary_connection):
			with conn():
				frappe.db.rollback()
		with self.primary_connection():
			frappe.db.delete(ff_ledger.LEDGER_DOCTYPE, {"invoice": ["like", f"{PREFIX}LOCK-%"]})
			frappe.db.delete(ff_ledger.TOTAL_DOCTYPE, {"period_key": ["in", self.PERIODS]})
			frappe.db.commit()

	def _short_lock_waits(self):
		for conn in (self.primary_connection, self.secondary_connection):
			with conn():
				frappe.db.sql("set session innodb_lock_wait_timeout = 1")

	def test_same_customer_waits_and_locking_read_sees_committed_usage(self):
		period = self.PERIODS[0]
		self.addCleanup(self._cleanup)
		self._short_lock_waits()
		with self.secondary_connection():
			# B opens its snapshot first (as validate does), before A commits.
			self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period), 0)
		with self.primary_connection():
			ff_ledger.lock_usage(LOCK_CUSTOMER, _company(), period)
			ff_ledger.record_rows([_usage_values(f"{PREFIX}LOCK-A", 800, LOCK_CUSTOMER, period)])
		with self.secondary_connection():
			# A holds the counter: B fails fast with a retry message (no retry loop).
			with self.assertRaises(frappe.ValidationError) as busy:
				ff_ledger.lock_usage(LOCK_CUSTOMER, _company(), period)
			self.assertIn("busy", str(busy.exception))
		with self.primary_connection():
			frappe.db.commit()  # A's submit ends: lock released
		with self.secondary_connection():
			# A plain read in B's transaction still returns its old snapshot...
			self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period), 0)
			# ...the submit path reads the counter under its lock and sees A's usage.
			self.assertEqual(ff_ledger.lock_usage(LOCK_CUSTOMER, _company(), period), 800)
			frappe.db.rollback()

	def test_different_customers_never_wait_on_each_other(self):
		"""Interleaved submits for two customers, on new and then existing counters, with a
		1 s lock-wait limit: any wait (gap lock) raises, and a deadlock raises QueryDeadlockError."""
		self.addCleanup(self._cleanup)
		self._short_lock_waits()
		for round_, period in enumerate(self.PERIODS * 2):
			a, b = self.primary_connection, self.secondary_connection
			with a():
				ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period)
				ff_ledger.lock_usage(LOCK_CUSTOMER, _company(), period)
			with b():
				ff_ledger.get_usage(LOCK_CUSTOMER_2, _company(), period)
				ff_ledger.lock_usage(LOCK_CUSTOMER_2, _company(), period)
			with a():
				ff_ledger.record_rows([_usage_values(f"{PREFIX}LOCK-A{round_}", 100, LOCK_CUSTOMER, period)])
			with b():
				ff_ledger.record_rows([_usage_values(f"{PREFIX}LOCK-B{round_}", 50, LOCK_CUSTOMER_2, period)])
			with a():
				frappe.db.commit()
			with b():
				frappe.db.commit()
		with self.primary_connection():
			for period in self.PERIODS:
				self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period), 200)
				self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER_2, _company(), period), 100)

	def test_one_customer_in_two_companies_never_waits(self):
		"""Same customer, same period, two companies: separate counters, no lock wait."""
		self.addCleanup(self._cleanup)
		self._short_lock_waits()
		period = self.PERIODS[2]
		other = _other_company()
		with self.primary_connection():
			ff_ledger.lock_usage(LOCK_CUSTOMER, _company(), period)
		with self.secondary_connection():
			ff_ledger.lock_usage(LOCK_CUSTOMER, other, period)  # would raise "busy" if it waited
			ff_ledger.record_rows([{**_usage_values(f"{PREFIX}LOCK-C", 70, LOCK_CUSTOMER, period), "company": other}])
		with self.primary_connection():
			ff_ledger.record_rows([_usage_values(f"{PREFIX}LOCK-D", 40, LOCK_CUSTOMER, period)])
			frappe.db.commit()
		with self.secondary_connection():
			frappe.db.commit()
		with self.primary_connection():
			self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period), 40)
			self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, other, period), 70)

	def _commit_usage(self, invoice, amount, period):
		with self.primary_connection():
			ff_ledger.record_rows([_usage_values(invoice, amount, LOCK_CUSTOMER, period)])
			frappe.db.commit()

	def _ledger_sum(self, period):
		return flt(
			frappe.db.sql(
				f"""select sum(base_amount) from `tab{ff_ledger.LEDGER_DOCTYPE}`
				where customer = %s and company = %s and period_key = %s and is_cancelled = 0""",
				(LOCK_CUSTOMER, _company(), period),
			)[0][0]
		)

	def test_rebuild_sees_submits_committed_after_its_snapshot(self):
		period = self.PERIODS[3]
		self.addCleanup(self._cleanup)
		self._short_lock_waits()
		self._commit_usage(f"{PREFIX}LOCK-R1", 100, period)
		with self.secondary_connection():
			# The rebuild's transaction has read before (as backfill's last batch query does).
			self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period), 100)
		self._commit_usage(f"{PREFIX}LOCK-R2", 40, period)  # a till submits meanwhile
		with self.secondary_connection():
			ff_ledger.rebuild_totals(period)
			frappe.db.commit()
		with self.primary_connection():
			self.assertEqual(self._ledger_sum(period), 140)
			self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period), 140)
		# A submit still in flight holds its counter: the rebuild waits instead of overwriting.
		with self.primary_connection():
			ff_ledger.lock_usage(LOCK_CUSTOMER, _company(), period)
			ff_ledger.record_rows([_usage_values(f"{PREFIX}LOCK-R3", 10, LOCK_CUSTOMER, period)])
		with self.secondary_connection():
			with self.assertRaises(frappe.QueryTimeoutError):
				ff_ledger.rebuild_totals(period)
			frappe.db.rollback()
		with self.primary_connection():
			frappe.db.commit()
		with self.secondary_connection():
			ff_ledger.rebuild_totals(period)
			frappe.db.commit()
		with self.primary_connection():
			self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period), 150)

	def test_stale_second_cancel_subtracts_nothing(self):
		period = self.PERIODS[4]
		self.addCleanup(self._cleanup)
		self._short_lock_waits()
		self._commit_usage(f"{PREFIX}LOCK-X", 100, period)
		self._commit_usage(f"{PREFIX}LOCK-Y", 50, period)
		invoice = frappe._dict(doctype="POS Invoice", name=f"{PREFIX}LOCK-X")
		with self.secondary_connection():
			# B's snapshot still shows X's row as counted.
			self.assertEqual(ff_ledger._recorded_for_invoice("POS Invoice", invoice.name), 100)
		with self.primary_connection():
			ff_ledger.on_cancel(invoice)
			frappe.db.commit()
		with self.secondary_connection():
			ff_ledger.on_cancel(invoice)  # stale re-read finds the row; its flip changes nothing
			frappe.db.commit()
		with self.primary_connection():
			self.assertEqual(ff_ledger.get_usage(LOCK_CUSTOMER, _company(), period), 50)
