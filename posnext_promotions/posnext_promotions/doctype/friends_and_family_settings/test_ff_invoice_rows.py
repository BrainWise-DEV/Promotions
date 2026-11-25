# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""F&F invoice rows: line snapshot, explicit markers, consolidated skip, returns from the original row."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.promotions import ff_ledger
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
	apply_friends_and_family_on_invoice,
)
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	FIELD_FNF_PERCENTAGE,
	FIELD_FNF_PROGRAM,
	ensure_invoice_item_fields,
)
from posnext_promotions.promotions.friends_and_family_eligibility import FLAG_FIELD, ensure_customer_flag_field
from posnext_promotions.promotions.friends_and_family_log import LOG_DOCTYPE, LOG_RETURNED, log_invoice_submit
from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows, give_verified_email

FF_GROUP = "_Test FF Rows Customer Group"
OTHER_GROUP = "_Test FF Rows Other Group"
CUSTOMER = "_Test FF Rows Customer"
OUTSIDER = "_Test FF Rows Outsider"
ITEM = "_Test FF Rows Item"
PREFIX = "_T-FFR-"
DATE = "2026-11-10"
PERIOD = "2026-11"
FNF = DISCOUNT_SOURCE_FRIENDS_AND_FAMILY
LINK = {"Sales Invoice": "sales_invoice_item", "POS Invoice": "pos_invoice_item"}


def _company():
	return frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {})


def _currency():
	return frappe.get_cached_value("Company", _company(), "default_currency")


def _ensure_fixtures():
	ensure_customer_flag_field()
	ensure_invoice_item_fields()
	root_cg = frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ""}) or (
		"All Customer Groups"
	)
	for group in (FF_GROUP, OTHER_GROUP):
		if not frappe.db.exists("Customer Group", group):
			frappe.get_doc(
				{"doctype": "Customer Group", "customer_group_name": group, "parent_customer_group": root_cg, "is_group": 0}
			).insert()
	for name, group in ((CUSTOMER, FF_GROUP), (OUTSIDER, OTHER_GROUP)):
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": group}).insert()
		frappe.db.set_value("Customer", name, {FLAG_FIELD: 0, "disabled": 0, "customer_group": group})
	give_verified_email(CUSTOMER)
	# Eligibility = approved, OTP-verified email; the outsider has none.
	frappe.db.set_value("Customer", OUTSIDER, "email_id", None, update_modified=False)
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


class TestFFInvoiceRows(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_fixtures()

	def setUp(self):
		frappe.db.savepoint("ffr_case")
		self._seq = 0
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
		doc.save()

	def tearDown(self):
		frappe.db.rollback(save_point="ffr_case")

	# ---- helpers ------------------------------------------------------------

	def _name(self):
		self._seq += 1
		return f"{PREFIX}{frappe.generate_hash(length=6)}-{self._seq}"

	def _row(self, name, rate=100, qty=1, **extra):
		return frappe._dict(
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

	def _invoice(self, rows, doctype="POS Invoice", **header):
		return frappe._dict(
			{
				"doctype": doctype,
				"name": header.pop("name", None) or self._name(),
				"company": _company(),
				"currency": _currency(),
				"conversion_rate": 1,
				"customer": CUSTOMER,
				"posting_date": DATE,
				"is_pos": 1,
				"items": rows,
				"additional_discount_percentage": 0,
				"discount_amount": 0,
				"flags": frappe._dict(),
				**header,
			}
		)

	def _original(self, doctype="POS Invoice", rows=((100, 70, 1, FNF),), customer=CUSTOMER, **values):
		"""A submitted original invoice in the DB. Row = (price list rate, rate, qty, source)."""
		name = values.pop("name", None) or self._name()
		header = {
			"name": name,
			"customer": customer,
			"company": _company(),
			"currency": _currency(),
			"conversion_rate": 1,
			"posting_date": DATE,
			"docstatus": 1,
			"is_pos": 1,
			**values,
		}
		self._insert(doctype, header)
		for idx, (plr, rate, qty, source) in enumerate(rows, 1):
			fnf = flt((plr - rate) * qty, 2) if source == FNF else 0
			self._insert(
				f"{doctype} Item",
				{
					"name": f"{name}-{idx}",
					"parent": name,
					"parenttype": doctype,
					"parentfield": "items",
					"idx": idx,
					"item_code": ITEM,
					"docstatus": 1,
					"qty": qty,
					"price_list_rate": plr,
					"rate": rate,
					FIELD_DISCOUNT_SOURCE: source,
					FIELD_FNF_AMOUNT: fnf,
					FIELD_FNF_PERCENTAGE: 30 if source == FNF else 0,
				},
			)
		return name

	def _insert(self, table, values):
		cols = ", ".join(f"`{c}`" for c in values)
		frappe.db.sql(
			f"insert into `tab{table}` ({cols}) values ({', '.join(['%s'] * len(values))})", tuple(values.values())
		)

	def _return(self, original, qty=-1, doctype="POS Invoice", link=True, idx=1, **row_values):
		name = self._name()
		row = frappe._dict(
			{"name": f"{name}-1", "item_code": ITEM, "qty": qty, "rate": 70, "warehouse": None, "uom": "Nos", **row_values}
		)
		if link:
			row[LINK[doctype]] = f"{original}-{idx}"
		return self._invoice([row], doctype=doctype, name=name, is_return=1, return_against=original)

	def _submit_return(self, doc):
		"""Validate + persist as submitted (raw) + ledger, like a real submit."""
		apply_friends_and_family_on_invoice(doc)
		self._insert(
			doc.doctype,
			{
				"name": doc.name,
				"customer": doc.customer,
				"company": doc.company,
				"posting_date": doc.posting_date,
				"docstatus": 1,
				"is_pos": 1,
				"is_return": 1,
				"return_against": doc.return_against,
			},
		)
		link = LINK[doc.doctype]
		for idx, row in enumerate(doc["items"], 1):
			self._insert(
				f"{doc.doctype} Item",
				{
					"name": row.name,
					"parent": doc.name,
					"parenttype": doc.doctype,
					"parentfield": "items",
					"idx": idx,
					"item_code": row.item_code,
					"docstatus": 1,
					"qty": row.qty,
					"rate": row.rate,
					link: row.get(link),
					FIELD_FNF_AMOUNT: row.get(FIELD_FNF_AMOUNT),
					FIELD_DISCOUNT_SOURCE: row.get(FIELD_DISCOUNT_SOURCE),
				},
			)
		ff_ledger.on_submit(doc)
		return doc

	def _ledger(self, invoice):
		return [
			flt(r.amount)
			for r in frappe.get_all(ff_ledger.LEDGER_DOCTYPE, filters={"invoice": invoice}, fields=["amount"])
		]

	def _logs(self, invoice):
		return frappe.get_all(
			LOG_DOCTYPE,
			filters={"reference_name": invoice},
			fields=["log_type", "discount_amount", "reason", "pos_profile", "return_against", "item_code"],
		)

	# ---- line snapshot ------------------------------------------------------

	def test_stamp_records_percentage(self):
		row = self._row("r1")
		apply_friends_and_family_on_invoice(self._invoice([row]))
		self.assertEqual(row[FIELD_DISCOUNT_SOURCE], FNF)
		self.assertEqual(flt(row[FIELD_FNF_PERCENTAGE]), 30)
		self.assertFalse(row.get(FIELD_FNF_PROGRAM))  # program names arrive in Phase 3

	def test_capped_stamp_keeps_program_percentage(self):
		settings = frappe.get_single("Friends and Family Settings")
		settings.max_discount_per_order = 10
		settings.save()
		row = self._row("r1")
		apply_friends_and_family_on_invoice(self._invoice([row]))
		self.assertEqual(flt(row.rate), 90)
		self.assertEqual(flt(row[FIELD_FNF_PERCENTAGE]), 30)

	def test_cleared_stamp_clears_snapshot(self):
		row = self._row("r1", discount_percentage=30, **{FIELD_DISCOUNT_SOURCE: FNF, FIELD_FNF_PERCENTAGE: 30})
		row.rate = 70
		apply_friends_and_family_on_invoice(self._invoice([row], customer=OUTSIDER))
		self.assertEqual(flt(row.rate), 100)
		self.assertFalse(flt(row.get(FIELD_FNF_PERCENTAGE)))
		self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))

	def test_manual_discount_equal_to_ff_pct_not_treated_as_ff(self):
		# Customer not eligible: the manual 30 % stays.
		row = self._row("r1", rate=70, price_list_rate=100, discount_percentage=30)
		apply_friends_and_family_on_invoice(self._invoice([row], customer=OUTSIDER))
		self.assertEqual(flt(row.rate), 70)
		self.assertEqual(flt(row.discount_percentage), 30)
		# Eligible: the manual 30 % is a competitor, not mistaken for an F&F stamp.
		row = self._row("r2", rate=70, price_list_rate=100, discount_percentage=30)
		payload = apply_friends_and_family_on_invoice(self._invoice([row]))
		self.assertEqual(payload["items"][0]["competing_source"], "manual")

	def test_legacy_row_without_markers_left_alone(self):
		row = self._row("r1", rate=70, price_list_rate=100, discount_percentage=30)
		apply_friends_and_family_on_invoice(self._invoice([row], customer=OUTSIDER))
		self.assertEqual((flt(row.rate), flt(row.discount_percentage)), (70, 30))
		self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))
		self.assertFalse(flt(row.get(FIELD_FNF_AMOUNT)))

	def test_consolidated_invoice_skipped(self):
		for doctype in ("Sales Invoice", "POS Invoice"):
			row = self._row("r1")
			doc = self._invoice([row], doctype=doctype, is_consolidated=1)
			self.assertIsNone(apply_friends_and_family_on_invoice(doc))
			self.assertEqual((flt(row.rate), flt(row.discount_percentage)), (100, 0), doctype)
			self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))
			row[FIELD_DISCOUNT_SOURCE] = FNF
			row.rate = 70
			ff_ledger.on_submit(doc)
			self.assertEqual(self._ledger(doc.name), [], doctype)

	# ---- returns ------------------------------------------------------------

	def test_partial_return_usage(self):
		profile = frappe.db.get_value("POS Profile", {}, "name")
		original = self._original(rows=((1000, 900, 4, FNF),), pos_profile=profile)
		ret = self._return(original)
		self._submit_return(ret)
		row = ret["items"][0]
		self.assertEqual(flt(row[FIELD_FNF_AMOUNT]), -100)
		self.assertEqual(row[FIELD_DISCOUNT_SOURCE], FNF)
		self.assertEqual(flt(row[FIELD_FNF_PERCENTAGE]), 30)
		self.assertEqual(self._ledger(ret.name), [-100])
		self.assertEqual(ff_ledger.get_usage(CUSTOMER, _company(), PERIOD), 0)  # clamped on read
		log_invoice_submit(ret)
		logs = self._logs(ret.name)
		self.assertEqual(len(logs), 1)
		self.assertEqual((logs[0].log_type, flt(logs[0].discount_amount)), (LOG_RETURNED, -100))
		self.assertEqual(logs[0].pos_profile, profile)  # the original sale's profile

	def test_return_from_pos_payload_credits(self):
		original = self._original(doctype="Sales Invoice", rows=((100, 70, 2, FNF),))
		name = self._name()
		row = frappe._dict(
			{
				"name": f"{name}-1",
				"item_code": ITEM,
				"qty": -1,
				"rate": 70,
				"warehouse": None,
				"uom": "Nos",
				"sales_invoice_item": f"{original}-1",
			}
		)
		ret = self._invoice([row], doctype="Sales Invoice", name=name, is_return=1, return_against=original)
		self._submit_return(ret)
		self.assertEqual(flt(row[FIELD_FNF_AMOUNT]), -30)
		self.assertEqual(row[FIELD_DISCOUNT_SOURCE], FNF)
		self.assertEqual(self._ledger(name), [-30])

	def test_partial_returns_sum_to_original(self):
		original = self._original(rows=((10, 6.6667, 3, FNF),))  # recorded F&F 10.00 over 3 units
		self.assertEqual(flt(frappe.db.get_value("POS Invoice Item", f"{original}-1", FIELD_FNF_AMOUNT)), 10)
		credits = []
		for _ in range(3):
			ret = self._submit_return(self._return(original))
			credits.append(flt(ret["items"][0][FIELD_FNF_AMOUNT]))
		self.assertEqual(credits, [-3.33, -3.34, -3.33])
		self.assertEqual(flt(sum(credits), 2), -10)
		# Nothing left to return: an extra return credits 0.
		extra = self._submit_return(self._return(original))
		self.assertEqual(flt(extra["items"][0][FIELD_FNF_AMOUNT]), 0)

	def test_two_rows_of_one_return_on_the_same_original(self):
		original = self._original(rows=((10, 6.6667, 3, FNF),))
		ret = self._return(original)
		ret["items"].append(frappe._dict({**ret["items"][0], "name": f"{ret.name}-2", "qty": -2}))
		self._submit_return(ret)
		self.assertEqual([flt(r[FIELD_FNF_AMOUNT]) for r in ret["items"]], [-3.33, -6.67])

	def test_crafted_return_cannot_reset_cap(self):
		original = self._original(rows=((100, 70, 1, FNF),))
		crafted = {FIELD_DISCOUNT_SOURCE: FNF, "price_list_rate": 1e9, FIELD_FNF_AMOUNT: -1e6, "discount_amount": 1e9}
		ret = self._submit_return(self._return(original, **crafted))
		self.assertEqual(flt(ret["items"][0][FIELD_FNF_AMOUNT]), -30)
		self.assertEqual(self._ledger(ret.name), [-30])
		# Returning the same line again (qty beyond the original) credits nothing more.
		again = self._submit_return(self._return(original, **crafted))
		self.assertEqual(flt(again["items"][0][FIELD_FNF_AMOUNT]), 0)
		self.assertEqual(self._ledger(again.name), [0])  # recorded at 0: its qty counts for later returns

	def test_return_without_link_credits_zero(self):
		original = self._original(rows=((100, 70, 1, FNF),))
		ret = self._return(original, link=False, **{FIELD_DISCOUNT_SOURCE: FNF, "price_list_rate": 100})
		self._submit_return(ret)
		row = ret["items"][0]
		self.assertEqual(flt(row[FIELD_FNF_AMOUNT]), 0)
		self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))  # client marker not trusted
		self.assertEqual(self._ledger(ret.name), [])
		log_invoice_submit(ret)
		logs = self._logs(ret.name)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].log_type, LOG_RETURNED)
		self.assertEqual(flt(logs[0].discount_amount), 0)
		self.assertEqual(logs[0].reason, ff_ledger.RETURN_NO_LINK)
		self.assertEqual(logs[0].return_against, original)

	def test_return_link_to_another_invoice_credits_zero(self):
		original = self._original(rows=((100, 70, 1, FNF),))
		big = self._original(rows=((10000, 7000, 1, FNF),), customer=OUTSIDER)
		ret = self._return(original)
		ret["items"][0]["pos_invoice_item"] = f"{big}-1"  # another customer's F&F line
		self._submit_return(ret)
		self.assertEqual(flt(ret["items"][0][FIELD_FNF_AMOUNT]), 0)
		self.assertEqual(ret.flags.ff_return_warnings[0]["reason"], ff_ledger.RETURN_ORIGINAL_MISSING)

	def test_return_of_non_fnf_line_clears_client_markers_without_warning(self):
		original = self._original(rows=((100, 90, 1, None),))
		ret = self._return(original, **{FIELD_DISCOUNT_SOURCE: FNF, FIELD_FNF_PERCENTAGE: 30, FIELD_FNF_AMOUNT: -10})
		self._submit_return(ret)
		row = ret["items"][0]
		self.assertEqual(flt(row[FIELD_FNF_AMOUNT]), 0)
		self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))
		self.assertFalse(flt(row.get(FIELD_FNF_PERCENTAGE)))
		self.assertEqual(ret.flags.ff_return_warnings, [])

	def test_plain_return_without_link_not_warned(self):
		original = self._original(rows=((100, 90, 1, None),))
		ret = self._submit_return(self._return(original, link=False))
		self.assertEqual(ret.flags.ff_return_warnings, [])

	def test_return_against_consolidated_invoice_credits_zero(self):
		# A consolidated Sales Invoice's rows copy the F&F fields of POS Invoices already recorded.
		original = self._original(doctype="Sales Invoice", rows=((100, 70, 1, FNF),), is_consolidated=1)
		ret = self._submit_return(self._return(original, doctype="Sales Invoice"))
		self.assertEqual(flt(ret["items"][0][FIELD_FNF_AMOUNT]), 0)
		self.assertFalse(ret["items"][0].get(FIELD_DISCOUNT_SOURCE))
		self.assertEqual(self._ledger(ret.name), [])
		self.assertEqual(ret.flags.ff_return_warnings[0]["reason"], ff_ledger.RETURN_ORIGINAL_CONSOLIDATED)

	def test_return_of_a_return_credits_zero(self):
		original = self._original(rows=((100, 70, 1, FNF),))
		first = self._submit_return(self._return(original))
		self.assertEqual(flt(first["items"][0][FIELD_FNF_AMOUNT]), -30)
		again = self._submit_return(self._return(first.name, qty=1))  # "return" of the credit note
		self.assertEqual(flt(again["items"][0][FIELD_FNF_AMOUNT]), 0)
		self.assertEqual(self._ledger(again.name), [])
		self.assertEqual(again.flags.ff_return_warnings[0]["reason"], ff_ledger.RETURN_ORIGINAL_IS_RETURN)
		log_invoice_submit(again)
		self.assertEqual(self._logs(again.name)[0].reason, ff_ledger.RETURN_ORIGINAL_IS_RETURN)
