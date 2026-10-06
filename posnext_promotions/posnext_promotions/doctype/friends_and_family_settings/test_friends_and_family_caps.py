# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""PN-129 — Friends & Family discount caps (per order + monthly per customer)."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions.best_discount_coupon import resolve_coupon_best_discount
from posnext_promotions.promotions.friends_and_family import get_active_program
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
	REASON_APPLIED,
	REASON_CAP_REACHED,
	REASON_OTHER_BETTER,
	apply_friends_and_family_on_invoice,
	apply_friends_and_family_to_items,
	compute_friends_and_family_discounts,
)
from posnext_promotions.promotions.friends_and_family_caps import (
	CAP_MONTHLY,
	CAP_ORDER,
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	allocate,
	ensure_invoice_item_fields,
	get_monthly_usage,
)
from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
	ensure_customer_flag_field,
)

DATE = "2026-10-05"
FF_GROUP = "_Test FF Caps Customer Group"
CUSTOMER = "_Test FF Caps Customer"
OTHER_CUSTOMER = "_Test FF Caps Other Customer"
ITEM = "_Test FF Caps Item"
ITEM_2 = "_Test FF Caps Item 2"
PROMO_RULE = "_Test FF Caps Promo Rule"
LEDGER_PREFIX = "_T-PN129-"


class TestFriendsAndFamilyCaps(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()
		ensure_invoice_item_fields()
		root_cg = frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ""}) or (
			"All Customer Groups"
		)
		if not frappe.db.exists("Customer Group", FF_GROUP):
			frappe.get_doc(
				{
					"doctype": "Customer Group",
					"customer_group_name": FF_GROUP,
					"parent_customer_group": root_cg,
					"is_group": 0,
				}
			).insert()
		for name in (CUSTOMER, OTHER_CUSTOMER):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": FF_GROUP}).insert()
			frappe.db.set_value("Customer", name, {FLAG_FIELD: 0, "disabled": 0})
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or "Products"
		for code in (ITEM, ITEM_2):
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
		frappe.db.savepoint("pn129_case")
		self._configure()
		self._seq = 0

	def tearDown(self):
		frappe.db.rollback(save_point="pn129_case")

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
				"excluded_items": [],
				"excluded_item_groups": [],
				"eligibility_basis": "Customer Group",
				"eligible_customer_groups": [{"customer_group": FF_GROUP}],
				"user_message": "Friends & Family applied",
				"max_discount_per_order": 0,
				"max_discount_per_customer_month": 0,
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

	def _promo_line(self, pct, **extra):
		line = self._line(pricing_rules=PROMO_RULE, discount_source="item_level_promotion", **extra)
		line.discount_percentage = pct
		line.discount_amount = flt(line.price_list_rate) * flt(line.qty) * pct / 100
		return line

	def _ledger(
		self,
		fnf_amounts,
		doctype="POS Invoice",
		customer=CUSTOMER,
		posting_date=DATE,
		docstatus=1,
		is_return=0,
		return_against=None,
		is_consolidated=0,
		name=None,
	):
		"""Insert a bare invoice + rows carrying F&F usage (no GL / stock side effects)."""
		self._seq += 1
		name = name or f"{LEDGER_PREFIX}{doctype[:3]}-{self._seq}"
		values = {
			"name": name,
			"customer": customer,
			"posting_date": posting_date,
			"docstatus": docstatus,
			"is_return": is_return,
			"return_against": return_against,
		}
		if frappe.db.has_column(doctype, "is_consolidated"):
			values["is_consolidated"] = is_consolidated
		cols = ", ".join(f"`{c}`" for c in values)
		marks = ", ".join(["%s"] * len(values))
		frappe.db.sql(f"insert into `tab{doctype}` ({cols}) values ({marks})", tuple(values.values()))
		for idx, amount in enumerate(fnf_amounts, 1):
			frappe.db.sql(
				f"""insert into `tab{doctype} Item`
				(name, parent, parenttype, parentfield, idx, item_code, docstatus, `{FIELD_DISCOUNT_SOURCE}`, `{FIELD_FNF_AMOUNT}`)
				values (%s, %s, %s, 'items', %s, %s, %s, %s, %s)""",
				(f"{name}-{idx}", name, doctype, idx, ITEM, docstatus, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY, amount),
			)
		return name

	# ---- settings ---------------------------------------------------------

	def test_settings_caps_exposed_and_validated(self):
		self._configure(max_discount_per_order=50, max_discount_per_customer_month=200)
		program = get_active_program(DATE)
		self.assertEqual(program.max_discount_per_order, 50)
		self.assertEqual(program.max_discount_per_customer_month, 200)
		self.assertTrue(program.has_caps)
		payload = program.to_client_payload()
		self.assertEqual(payload["max_discount_per_order"], 50)
		self.assertEqual(payload["max_discount_per_customer_month"], 200)
		with self.assertRaises(frappe.ValidationError):
			self._configure(max_discount_per_order=-1)

	def test_no_caps_by_default(self):
		payload = compute_friends_and_family_discounts(CUSTOMER, [self._line(rate=1000)], DATE)
		self.assertEqual(payload["items"][0]["discount_amount"], 300)
		self.assertFalse(payload["caps"]["active"])
		self.assertFalse(payload["caps"]["capped"])
		self.assertIsNone(payload["caps"]["allowance"])

	# ---- per-order cap ------------------------------------------------------

	def test_under_order_cap_full_fnf(self):
		self._configure(max_discount_per_order=100)
		lines = [self._line(), self._line(item_code=ITEM_2)]
		payload = apply_friends_and_family_to_items(lines, customer=CUSTOMER, posting_date=DATE)
		self.assertEqual([ln.discount_percentage for ln in lines], [30, 30])
		caps = payload["caps"]
		self.assertTrue(caps["active"])
		self.assertFalse(caps["capped"])
		self.assertEqual(caps["fnf_applied_total"], 60)
		self.assertEqual(caps["allowance"], 100)
		self.assertEqual(payload["message"], "Friends & Family applied")
		self.assertEqual([ln[FIELD_FNF_AMOUNT] for ln in lines], [30, 30])
		self.assertEqual([ln[FIELD_DISCOUNT_SOURCE] for ln in lines], [DISCOUNT_SOURCE_FRIENDS_AND_FAMILY] * 2)

	def test_partial_order_cap_split_proportionally(self):
		self._configure(max_discount_per_order=50)
		lines = [self._line(rate=100), self._line(item_code=ITEM_2, rate=200)]  # uncapped 30 + 60
		payload = apply_friends_and_family_to_items(lines, customer=CUSTOMER, posting_date=DATE)
		amounts = [r["discount_amount"] for r in payload["items"]]
		self.assertEqual(amounts, [16.66, 33.33])  # 50 * 30/90, 50 * 60/90 floored to the cent
		self.assertLessEqual(sum(amounts), 50)
		self.assertTrue(all(r["applies"] and r["reason"] == REASON_APPLIED for r in payload["items"]))
		self.assertAlmostEqual(flt(lines[0].rate), 83.34, places=2)
		self.assertAlmostEqual(flt(lines[1].discount_percentage), 16.665, places=2)
		caps = payload["caps"]
		self.assertTrue(caps["capped"])
		self.assertEqual(caps["cap_hit"], CAP_ORDER)
		self.assertEqual(caps["fnf_uncapped_total"], 90)
		self.assertEqual(caps["fnf_applied_total"], 49.99)
		self.assertEqual(payload["cap_hit"], CAP_ORDER)
		self.assertIn("per-order maximum of 50.00", payload["message_en"])
		self.assertIn("الحد الأقصى للفاتورة", payload["message_ar"])
		self.assertEqual(payload["best_discount"]["caps"]["cap_hit"], CAP_ORDER)

	def test_order_cap_idempotent(self):
		self._configure(max_discount_per_order=50)
		lines = [self._line(rate=100), self._line(item_code=ITEM_2, rate=200)]
		first = apply_friends_and_family_to_items(lines, customer=CUSTOMER, posting_date=DATE)
		second = apply_friends_and_family_to_items(lines, customer=CUSTOMER, posting_date=DATE)
		self.assertEqual(
			[r["discount_amount"] for r in first["items"]], [r["discount_amount"] for r in second["items"]]
		)

	def test_allocate_never_exceeds_allowance(self):
		lines = [(0, 33.33, 0), (1, 33.33, 0), (2, 33.34, 0)]
		alloc = allocate(lines, 10)
		self.assertLessEqual(round(sum(alloc.values()), 2), 10)
		self.assertEqual(set(alloc), {0, 1, 2})
		self.assertEqual(allocate(lines, 0), {})
		self.assertEqual(allocate(lines, 1000), {0: 33.33, 1: 33.33, 2: 33.34})

	# ---- monthly cap -------------------------------------------------------

	def test_monthly_cap_exhausted_no_fnf_with_message(self):
		self._configure(max_discount_per_customer_month=100)
		self._ledger([60, 40])
		line = self._line()
		payload = apply_friends_and_family_to_items([line], customer=CUSTOMER, posting_date=DATE)
		self.assertFalse(payload["items"][0]["applies"])
		self.assertEqual(payload["items"][0]["reason"], REASON_CAP_REACHED)
		self.assertEqual(payload["applied_count"], 0)
		self.assertEqual(flt(line.discount_percentage), 0)
		self.assertNotEqual(line.get("discount_source"), DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		caps = payload["caps"]
		self.assertEqual(caps["monthly_used"], 100)
		self.assertEqual(caps["monthly_remaining"], 0)
		self.assertEqual(caps["cap_hit"], CAP_MONTHLY)
		self.assertIn("Monthly Friends & Family limit of 100.00 reached", payload["message_en"])
		self.assertIn("الحد الشهري", payload["message_ar"])
		# Other customers are unaffected.
		other = compute_friends_and_family_discounts(OTHER_CUSTOMER, [self._line()], DATE)
		self.assertTrue(other["items"][0]["applies"])

	def test_monthly_cap_partial_reduces_fnf(self):
		self._configure(max_discount_per_customer_month=100, max_discount_per_order=50)
		self._ledger([80])
		payload = compute_friends_and_family_discounts(CUSTOMER, [self._line(), self._line(item_code=ITEM_2)], DATE)
		self.assertEqual([r["discount_amount"] for r in payload["items"]], [10, 10])
		caps = payload["caps"]
		self.assertEqual(caps["allowance"], 20)  # min(order 50, monthly 100 - 80)
		self.assertEqual(caps["cap_hit"], CAP_MONTHLY)
		self.assertEqual(caps["monthly_remaining_after"], 0)
		self.assertIn("reduced to 20.00", payload["message_en"])

	def test_monthly_usage_ledger_rules(self):
		self._ledger([25])  # counts
		self._ledger([5], doctype="Sales Invoice")  # counts
		self._ledger([100], docstatus=0)  # draft
		self._ledger([100], docstatus=2)  # cancelled
		self._ledger([100], posting_date="2026-09-30")  # other month
		self._ledger([100], customer=OTHER_CUSTOMER)
		self._ledger([100], doctype="Sales Invoice", is_consolidated=1)  # merged POS invoices
		self.assertEqual(get_monthly_usage(CUSTOMER, DATE), 30)
		self.assertEqual(get_monthly_usage(CUSTOMER, "2026-09-15"), 100)
		self.assertEqual(get_monthly_usage(None, DATE), 0)

	def test_returns_reduce_usage_of_original_month(self):
		october = self._ledger([30, 30])
		september = self._ledger([40], posting_date="2026-09-20")
		self._ledger([-30], is_return=1, return_against=october, posting_date="2026-10-06")
		self._ledger([-40], is_return=1, return_against=september, posting_date="2026-10-06")
		self._ledger([-30], is_return=1, return_against=october, docstatus=0)  # draft return ignored
		self.assertEqual(get_monthly_usage(CUSTOMER, DATE), 30)  # 60 - 30; September return not credited here
		self.assertEqual(get_monthly_usage(CUSTOMER, "2026-09-01"), 0)

	def test_exclude_current_invoice_from_usage(self):
		name = self._ledger([50])
		self.assertEqual(get_monthly_usage(CUSTOMER, DATE), 50)
		self.assertEqual(get_monthly_usage(CUSTOMER, DATE, exclude=name), 0)

	# ---- capped F&F vs competitors ----------------------------------------

	def test_cap_reduced_fnf_loses_to_promotion(self):
		# Uncapped F&F (30) beats the 20% promo; capped to 15 per line it loses there,
		# so the promo stays and the whole allowance goes to the plain line.
		self._configure(max_discount_per_order=30)
		promo = self._promo_line(20)
		plain = self._line(item_code=ITEM_2)
		payload = apply_friends_and_family_to_items([promo, plain], customer=CUSTOMER, posting_date=DATE)
		first, second = payload["items"]
		self.assertFalse(first["applies"])
		self.assertEqual(first["reason"], REASON_CAP_REACHED)
		self.assertEqual(first["best_discount"]["winner"], bd.SOURCE_PROMOTION)
		self.assertEqual(promo.pricing_rules, PROMO_RULE)
		self.assertEqual(flt(promo.discount_percentage), 20)
		self.assertTrue(second["applies"])
		self.assertEqual(second["discount_amount"], 30)
		self.assertEqual(payload["caps"]["fnf_applied_total"], 30)
		self.assertLessEqual(payload["caps"]["fnf_applied_total"], 30)

	def test_promotion_still_wins_when_bigger_than_uncapped(self):
		self._configure(max_discount_per_order=10)
		payload = compute_friends_and_family_discounts(CUSTOMER, [self._promo_line(40)], DATE)
		self.assertEqual(payload["items"][0]["reason"], REASON_OTHER_BETTER)
		self.assertFalse(payload["caps"]["capped"])

	def test_coupon_beats_cap_reduced_fnf(self):
		line = self._line()
		coupon = frappe._dict(
			coupon_code="FFCAP15",
			discount_type="Percentage",
			discount_percentage=15,
			discount_amount=0,
			max_amount=0,
			min_amount=0,
			apply_on="Net Total",
			exclude_already_discounted_items=1,
		)
		uncapped = resolve_coupon_best_discount(coupon, [line], customer=CUSTOMER, posting_date=DATE)
		self.assertFalse(uncapped["valid"])  # F&F 30 > coupon 15
		self._configure(max_discount_per_order=10)
		capped = resolve_coupon_best_discount(coupon, [line], customer=CUSTOMER, posting_date=DATE)
		self.assertTrue(capped["valid"])  # capped F&F 10 < coupon 15
		self.assertEqual(capped["best_discount"]["lines"][0]["winner"], bd.SOURCE_COUPON)

	# ---- invoice validate --------------------------------------------------

	def _invoice(self, items, **header):
		return frappe._dict(
			{
				"doctype": "POS Invoice",
				"customer": CUSTOMER,
				"posting_date": DATE,
				"is_pos": 1,
				"items": items,
				"additional_discount_percentage": 0,
				"discount_amount": 0,
				**header,
			}
		)

	def test_invoice_validate_enforces_order_cap(self):
		self._configure(max_discount_per_order=40)
		rows = [self._line(qty=3, rate=33.33), self._line(item_code=ITEM_2, rate=100)]
		payload = apply_friends_and_family_on_invoice(self._invoice(rows))
		saved = sum((flt(r.price_list_rate) - flt(r.rate)) * flt(r.qty) for r in rows)
		self.assertLessEqual(round(saved, 2), 40)
		self.assertGreater(saved, 39.9)
		self.assertEqual(round(sum(flt(r[FIELD_FNF_AMOUNT]) for r in rows), 2), round(saved, 2))
		self.assertEqual(payload["caps"]["fnf_applied_total"], round(saved, 2))
		for r in rows:
			self.assertEqual(flt(r.discount_amount), round(flt(r.price_list_rate) - flt(r.rate), 2))  # per unit
			self.assertEqual(r[FIELD_DISCOUNT_SOURCE], DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)

	def test_invoice_validate_monthly_cap_excludes_itself(self):
		self._configure(max_discount_per_customer_month=100)
		name = self._ledger([30])
		self._ledger([70])
		doc = self._invoice([self._line()], name=name)
		payload = apply_friends_and_family_on_invoice(doc)
		# Usage excludes this invoice's own stored 30 → 70 used, 30 left → full F&F.
		self.assertEqual(payload["caps"]["monthly_used"], 70)
		self.assertEqual(flt(doc["items"][0].rate), 70)

	def test_invoice_validate_monthly_cap_reached_strips_fnf(self):
		self._configure(max_discount_per_customer_month=50)
		self._ledger([50])
		row = self._line(discount_percentage=30, **{FIELD_DISCOUNT_SOURCE: DISCOUNT_SOURCE_FRIENDS_AND_FAMILY})
		row.rate = 70
		payload = apply_friends_and_family_on_invoice(self._invoice([row]))
		self.assertEqual(flt(row.rate), 100)
		self.assertEqual(flt(row.discount_percentage), 0)
		self.assertFalse(flt(row.get(FIELD_FNF_AMOUNT)))
		self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))
		self.assertEqual(payload["caps"]["cap_hit"], CAP_MONTHLY)

	def test_return_invoice_stamps_negative_usage(self):
		fnf_row = self._line(qty=-2, discount_percentage=30, **{FIELD_DISCOUNT_SOURCE: DISCOUNT_SOURCE_FRIENDS_AND_FAMILY})
		fnf_row.rate = 70
		other_row = self._line(item_code=ITEM_2, qty=-1, **{FIELD_FNF_AMOUNT: 12})
		doc = self._invoice([fnf_row, other_row], is_return=1, return_against="X")
		self.assertIsNone(apply_friends_and_family_on_invoice(doc))
		self.assertEqual(flt(fnf_row[FIELD_FNF_AMOUNT]), -60)
		self.assertEqual(flt(fnf_row.rate), 70)  # pricing untouched on returns
		self.assertEqual(flt(other_row[FIELD_FNF_AMOUNT]), 0)

	def test_real_sales_invoice_rows_carry_usage(self):
		self._configure(max_discount_per_order=25)
		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}
		)
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": company,
				"customer": CUSTOMER,
				"posting_date": DATE,
				"set_posting_time": 1,
				"is_pos": 1,
				"items": [{"item_code": ITEM, "qty": 2, "price_list_rate": 100, "rate": 100}],
			}
		)
		apply_friends_and_family_on_invoice(doc)
		row = doc.items[0]
		self.assertEqual(flt(row.rate), 87.5)
		self.assertEqual(flt(row.get(FIELD_FNF_AMOUNT)), 25)
		self.assertEqual(row.get(FIELD_DISCOUNT_SOURCE), DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(flt(doc.net_total), 175)
