# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""PN-133 — Friends & Family end-to-end acceptance scenarios (PN-124 … PN-132).

Every scenario runs through real documents on the site: Friends and Family
Settings → Customer → POS Sales Invoice (real Item Prices and Pricing Rules,
``set_missing_values`` → insert/validate → submit) → Friends and Family Log →
Friends and Family Performance report. Each test runs inside a savepoint that
is rolled back (and ``frappe.db.commit`` is disabled for the class), so nothing
is left behind on the site.

Needs a POS Profile that can submit invoices: ``profile 1`` when present,
otherwise the first enabled profile (override with the
``FF_ACCEPTANCE_POS_PROFILE`` environment variable).
"""

import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows
from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.api.friends_and_family import apply_friends_and_family_discount
from posnext_promotions.api.offers import validate_coupon
from posnext_promotions.posnext_promotions.report.friends_and_family_performance.friends_and_family_performance import (
	execute as run_report,
)
from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import friends_and_family_demo
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	ensure_invoice_item_fields,
	get_monthly_usage,
)
from posnext_promotions.promotions.friends_and_family_demo import build_demo_data, setup_demo_data
from posnext_promotions.promotions.friends_and_family_eligibility import FLAG_FIELD
from posnext_promotions.promotions.friends_and_family_messages import EVENT_FF_ACTIVATED, get_customer_message

PREFIX = "_Test FF Acc"
FNF = "friends_and_family"
LOG = "Friends and Family Log"
CASHIER = "_test_ff_acc_cashier@example.com"


def _pick_pos_profile() -> str | None:
	wanted = os.environ.get("FF_ACCEPTANCE_POS_PROFILE")
	if wanted and frappe.db.exists("POS Profile", wanted):
		return wanted
	if frappe.db.exists("POS Profile", {"name": "profile 1", "disabled": 0}):
		return "profile 1"
	return frappe.db.get_value("POS Profile", {"disabled": 0})


class TestFriendsAndFamilyAcceptance(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._commit_patch = patch.object(frappe.db, "commit", lambda *a, **k: None)
		cls._commit_patch.start()
		ensure_invoice_item_fields()
		cls.profile = _pick_pos_profile()
		if not cls.profile:
			raise cls.skipTest(cls, "No POS Profile on this site")
		profile = frappe.get_doc("POS Profile", cls.profile)
		cls.company = profile.company
		cls.mode_of_payment = next(
			(
				p.mode_of_payment
				for p in profile.payments
				if frappe.db.get_value("Mode of Payment", p.mode_of_payment, "type") == "Cash"
			),
			profile.payments[0].mode_of_payment if profile.payments else None,
		)
		cls.demo = build_demo_data(prefix=PREFIX, company=cls.company, price_list=profile.selling_price_list)
		cls.items = cls.demo["items"]
		cls.staff = cls.demo["customers"]["staff"]
		cls.flagged = cls.demo["customers"]["flagged"]
		cls.public = cls.demo["customers"]["public"]
		cls.today = nowdate()

	@classmethod
	def tearDownClass(cls):
		cls._commit_patch.stop()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("pn133_case")
		self._settings()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="pn133_case")

	# ---- helpers --------------------------------------------------------------

	def _settings(self, **values):
		doc = frappe.get_single("Friends and Family Settings")
		doc.update(
			{
				"enabled": 1,
				"approved_domains": approved_domain_rows(self.demo["email_domain"]),
				"discount_percentage": 30,
				"discount_scope": "Entire Cart",
				"start_date": None,
				"end_date": None,
				"eligibility_basis": "Customer Group or Flag",
				"eligible_customer_groups": [{"customer_group": self.demo["customer_groups"]["staff"]}],
				"excluded_items": [{"item_code": self.items["gift_card"]}],
				"excluded_brands": [{"brand": self.demo["brands"]["x"]}],
				"excluded_item_groups": [],
				"max_discount_per_order": 0,
				"max_discount_per_customer_month": 0,
			}
		)
		doc.update(values)
		doc.save()
		return doc

	def _sell(self, customer, lines, submit=True, before_insert=None, **header):
		"""POS Sales Invoice built like the POS does: item details + Pricing Rules from ERPNext."""
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.company,
				"customer": customer,
				"is_pos": 1,
				"pos_profile": self.profile,
				"update_stock": 0,
				"items": [{"item_code": self.items[key], "qty": qty} for key, qty in lines],
			}
		)
		doc.set_missing_values()
		doc.update_stock = 0
		doc.update(header)
		if before_insert:
			before_insert(doc)
		doc.calculate_taxes_and_totals()
		doc.payments = []
		doc.append("payments", {"mode_of_payment": self.mode_of_payment, "amount": doc.rounded_total or doc.grand_total})
		doc.insert(set_name=f"_T-PN133-{frappe.generate_hash(length=10)}")
		if submit:
			self._pay_and_submit(doc)
		return doc

	def _pay_and_submit(self, doc):
		doc.payments[0].amount = doc.rounded_total or doc.grand_total
		doc.submit()

	def _cancel(self, doc):
		# Other apps (POS Next wallet / loyalty) attach submitted documents to the invoice.
		if frappe.db.exists("DocType", "Wallet Transaction"):
			for name in frappe.get_all(
				"Wallet Transaction", filters={"reference_name": doc.name, "docstatus": 1}, pluck="name"
			):
				frappe.get_doc("Wallet Transaction", name).cancel()
		doc.reload()
		doc.cancel()

	def _return(self, doc, keep=None):
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		ret = make_sales_return(doc.name)
		if keep is not None:
			ret.items = [row for row in ret.items if row.item_code in keep]
			for row in ret.items:
				row.qty = -abs(keep[row.item_code])
		ret.calculate_taxes_and_totals()
		for payment in ret.payments:
			payment.amount = 0
		if ret.payments:
			ret.payments[0].amount = ret.rounded_total or ret.grand_total
		ret.insert(set_name=f"_T-PN133-R{frappe.generate_hash(length=10)}")
		ret.submit()
		return ret

	def _row(self, doc, key):
		return next(row for row in doc.items if row.item_code == self.items[key])

	def _logs(self, doc, **filters):
		return frappe.get_all(
			LOG,
			filters={"reference_name": doc.name, **filters},
			fields=["log_type", "item_code", "discount_amount", "discount_source", "comparison_outcome",
				"competing_source", "competing_amount", "capped", "cap_hit", "reason", "is_cancelled", "message"],
			order_by="creation asc, name asc",
		)

	def _payload(self, doc):
		return doc.flags.friends_and_family

	def _cart(self, *lines, **extra):
		return [
			{
				"item_code": self.items[key],
				"qty": qty,
				"price_list_rate": self.demo["prices"][key],
				"rate": self.demo["prices"][key],
				**extra,
			}
			for key, qty in lines
		]

	def _report(self, customer, view="Invoices"):
		_cols, data, _m, _chart, summary = run_report(
			{"from_date": self.today, "to_date": self.today, "company": self.company, "customer": customer, "view": view}
		)
		return data, {s["label"]: s["value"] for s in summary}

	# ---- 1. eligible customer → auto F&F on covered items (PN-124/125/126) ----

	def test_01_eligible_customer_gets_fnf_on_submit(self):
		doc = self._sell(self.staff, [("laptop", 1), ("phone", 2)])
		laptop, phone = self._row(doc, "laptop"), self._row(doc, "phone")
		self.assertEqual(flt(laptop.discount_percentage), 30)
		self.assertEqual(flt(laptop.rate), 700)
		self.assertEqual(flt(phone.rate), 350)
		self.assertEqual(flt(doc.net_total), 1400)  # (1000 + 2×500) × 70%
		self.assertEqual(laptop.get(FIELD_DISCOUNT_SOURCE), FNF)
		self.assertEqual(flt(laptop.get(FIELD_FNF_AMOUNT)), 300)
		self.assertEqual(flt(phone.get(FIELD_FNF_AMOUNT)), 300)
		# Eligible message = the configured "ff_activated" Friends and Family Message, else the generic note.
		activated = get_customer_message(EVENT_FF_ACTIVATED, percent=friends_and_family_demo.DISCOUNT_PERCENTAGE) or {}
		self.assertEqual(
			self._payload(doc)["message"],
			activated.get("message") or frappe._("Customer is eligible for Friends & Family"),
		)
		logs = self._logs(doc)
		self.assertEqual({(log.log_type, log.item_code) for log in logs}, {
			("Discount Applied", self.items["laptop"]),
			("Discount Applied", self.items["phone"]),
		})
		self.assertTrue(all(log.discount_source == FNF for log in logs))
		self.assertEqual(sum(flt(log.discount_amount) for log in logs), 600)

	def test_01b_flagged_customer_eligible(self):
		doc = self._sell(self.flagged, [("laptop", 1)])
		self.assertEqual(flt(self._row(doc, "laptop").rate), 700)
		self._settings(eligibility_basis="Customer Group")  # flag no longer counts
		doc = self._sell(self.flagged, [("laptop", 1)])
		self.assertEqual(flt(self._row(doc, "laptop").rate), 1000)

	def test_01c_pos_cart_payload_contract(self):
		response = apply_offers(
			{
				"customer": self.staff,
				"pos_profile": self.profile,
				"company": self.company,
				"posting_date": self.today,
				"items": self._cart(("laptop", 1), ("headset", 1)),
			}
		)
		ff = response["friends_and_family"]
		self.assertTrue(ff["eligible"])
		self.assertEqual(ff["discount_percentage"], 30)
		self.assertEqual(ff["applied_count"], 1)
		lines = {line["item_code"]: line for line in ff["items"]}
		self.assertTrue(lines[self.items["laptop"]]["applies"])
		self.assertEqual(lines[self.items["laptop"]]["discount_amount"], 300)
		self.assertEqual(lines[self.items["headset"]]["reason"], "excluded")
		self.assertIn("caps", ff)
		self.assertEqual(response["best_discount"]["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)
		out = {row["item_code"]: row for row in response["items"]}
		self.assertEqual(flt(out[self.items["laptop"]]["discount_percentage"]), 30)

	# ---- 2. ineligible customer / cashier cannot force F&F (PN-125) ----

	def test_02_ineligible_customer_gets_nothing(self):
		doc = self._sell(self.public, [("laptop", 1)])
		self.assertEqual(flt(self._row(doc, "laptop").rate), 1000)
		self.assertFalse(self._row(doc, "laptop").get(FIELD_DISCOUNT_SOURCE))
		self.assertEqual(self._logs(doc), [])
		ff = apply_offers(
			{"customer": self.public, "pos_profile": self.profile, "company": self.company,
				"posting_date": self.today, "items": self._cart(("laptop", 1))}
		)["friends_and_family"]
		self.assertFalse(ff["eligible"])
		self.assertEqual(ff["discount_percentage"], 0)

	def _stale_fnf_line(self, key, pct=30):
		"""Cart line still carrying an F&F stamp from an earlier (eligible) pricing."""
		price = self.demo["prices"][key]
		return {
			"item_code": self.items[key],
			"qty": 1,
			"price_list_rate": price,
			"rate": price * (100 - pct) / 100,
			"discount_percentage": pct,
			"discount_amount": price * pct / 100,
			"discount_source": "friends_and_family",
			"is_already_discounted": 1,
			FIELD_DISCOUNT_SOURCE: "friends_and_family",
			FIELD_FNF_AMOUNT: price * pct / 100,
		}

	def _assert_fnf_stripped(self, line, list_price):
		self.assertEqual(flt(line.get("rate")), list_price)
		self.assertEqual(flt(line.get("discount_percentage")), 0)
		self.assertEqual(flt(line.get("discount_amount")), 0)
		self.assertNotEqual(line.get("discount_source"), "friends_and_family")
		self.assertFalse(line.get(FIELD_DISCOUNT_SOURCE))
		self.assertFalse(flt(line.get(FIELD_FNF_AMOUNT)))

	def test_02b_stale_fnf_stamps_stripped_for_ineligible_or_inactive(self):
		"""PN-130 finding: switching to an ineligible customer (or the program
		ending) must return the cart without the old F&F rate, on both the
		"no promotion applies" path and the full pricing path."""
		block_keys = {
			"eligible", "discount_percentage", "applied_count", "message", "reason",
			"items", "caps", "cap_hit",
		}

		def call(customer, *keys):
			return apply_offers(
				{"customer": customer, "pos_profile": self.profile, "company": self.company,
					"posting_date": self.today, "items": [self._stale_fnf_line(k) for k in keys]}
			)

		# No pricing rule on the cart → F&F-only response.
		res = call(self.public, "laptop")
		self._assert_fnf_stripped(res["items"][0], 1000)
		self.assertFalse(res["friends_and_family"]["eligible"])
		self.assertEqual(set(res["friends_and_family"]), block_keys)  # same shape as eligible

		# Cable has a 10% promotion → full pricing path; promo comes back, F&F does not.
		res = call(self.public, "laptop", "cable")
		laptop, cable = res["items"]
		self._assert_fnf_stripped(laptop, 1000)
		self.assertNotEqual(cable.get("discount_source"), "friends_and_family")
		self.assertEqual(flt(cable.get("discount_percentage")), 10)

		# Eligible customer, program disabled → also stripped.
		self._settings(enabled=0)
		res = call(self.staff, "laptop")
		self._assert_fnf_stripped(res["items"][0], 1000)
		self.assertFalse(res["friends_and_family"]["eligible"])

		# Control: eligible + enabled re-stamps the line at 30%.
		self._settings(enabled=1)
		res = call(self.staff, "laptop")
		self.assertEqual(flt(res["items"][0]["discount_percentage"]), 30)
		self.assertEqual(set(res["friends_and_family"]), block_keys)

	def test_03_cashier_cannot_force_flag_or_rate(self):
		if not frappe.db.exists("User", CASHIER):
			user = frappe.get_doc(
				{"doctype": "User", "email": CASHIER, "first_name": "FF Acc Cashier", "send_welcome_email": 0}
			)
			user.append("roles", {"role": "Sales User"})
			user.insert(ignore_permissions=True)
		self.assertFalse(frappe.has_permission("Friends and Family Settings", "write", user=CASHIER))
		frappe.set_user(CASHIER)
		try:
			customer = frappe.get_doc("Customer", self.public)
			customer.set(FLAG_FIELD, 1)
			with self.assertRaises(frappe.PermissionError):
				customer.save()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Customer", self.public, FLAG_FIELD), 0)

		# A forged 50% "F&F" line is re-stamped with the Settings rate.
		def forge(doc):
			row = self._row(doc, "laptop")
			row.discount_percentage = 50
			row.set(FIELD_DISCOUNT_SOURCE, FNF)

		doc = self._sell(self.staff, [("laptop", 1)], before_insert=forge)
		self.assertEqual(flt(self._row(doc, "laptop").discount_percentage), 30)
		self.assertEqual(flt(self._row(doc, "laptop").rate), 700)

		# The same forged line for an ineligible customer is stripped.
		doc = self._sell(self.public, [("laptop", 1)], before_insert=forge)
		self.assertEqual(flt(self._row(doc, "laptop").rate), 1000)

		# The API ignores a client-sent percentage.
		result = apply_friends_and_family_discount(
			customer=self.staff, items=self._cart(("laptop", 1), discount_percentage=90, discount_source=FNF)
		)
		self.assertEqual(result["items"][0]["discount_percentage"], 30)
		self.assertEqual(result["items"][0]["discount_amount"], 300)

	# ---- 3. exclusions / mixed cart with brand exclusions (PN-127) ----

	def test_04_excluded_item_gets_no_fnf(self):
		doc = self._sell(self.staff, [("gift_card", 1), ("laptop", 1)])
		self.assertEqual(flt(self._row(doc, "gift_card").rate), 300)
		self.assertEqual(flt(self._row(doc, "laptop").rate), 700)
		excluded = [log for log in self._logs(doc) if log.log_type == "Excluded"]
		self.assertEqual([log.item_code for log in excluded], [self.items["gift_card"]])

	def test_05_mixed_cart_with_brand_exclusion(self):
		doc = self._sell(self.staff, [("laptop", 1), ("headset", 1)])
		self.assertEqual(flt(self._row(doc, "laptop").rate), 700)
		self.assertEqual(flt(self._row(doc, "headset").rate), 400)
		self.assertEqual(flt(doc.net_total), 1100)
		self.assertIn(self.demo["brands"]["x"], self._payload(doc)["excluded_brands"])
		types = {log.item_code: log.log_type for log in self._logs(doc)}
		self.assertEqual(types[self.items["headset"]], "Excluded")

	def test_05b_all_items_excluded_message(self):
		result = apply_friends_and_family_discount(
			customer=self.staff, items=self._cart(("gift_card", 1), ("headset", 1))
		)
		self.assertTrue(result["eligible"])
		self.assertEqual(result["applied_count"], 0)
		self.assertEqual(result["reason"], "no_covered_items")
		self.assertIn("every item in the cart is excluded", result["message"])
		doc = self._sell(self.staff, [("gift_card", 1), ("headset", 1)])
		self.assertEqual(flt(doc.net_total), 700)
		self.assertEqual({log.log_type for log in self._logs(doc)}, {"Excluded"})

	# ---- 4–6. best-of: promo lower / higher / tie (PN-128) ----

	def test_06_lower_promotion_loses_to_fnf_with_message(self):
		doc = self._sell(self.staff, [("cable", 1)], submit=False)
		row = self._row(doc, "cable")
		self.assertEqual(flt(row.discount_percentage), 30)
		self.assertEqual(flt(row.rate), 70)
		self.assertFalse(row.pricing_rules)  # promotion detached — no stacking
		decision = self._payload(doc)["items"][0]["best_discount"]
		self.assertEqual(decision["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(decision["outcome"], bd.OUTCOME_FNF_BETTER)
		self.assertIn("Friends & Family", decision["message_en"])
		self.assertIn("الأصدقاء والعائلة", decision["message_ar"])
		self._pay_and_submit(doc)
		log = self._logs(doc)[0]
		self.assertEqual((log.log_type, log.comparison_outcome, log.competing_source), (
			"Discount Applied", bd.OUTCOME_FNF_BETTER, bd.SOURCE_PROMOTION))
		self.assertEqual(flt(log.competing_amount), 10)

	def test_07_higher_promotion_wins_with_message(self):
		doc = self._sell(self.staff, [("charger", 1)])
		row = self._row(doc, "charger")
		self.assertEqual(flt(row.discount_percentage), 40)
		self.assertEqual(flt(row.rate), 120)
		self.assertTrue(row.pricing_rules)
		self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))
		decision = self._payload(doc)["items"][0]["best_discount"]
		self.assertEqual(decision["winner"], bd.SOURCE_PROMOTION)
		self.assertTrue(decision["message_en"])
		self.assertTrue(decision["message_ar"])
		log = self._logs(doc)[0]
		self.assertEqual((log.log_type, log.discount_source), ("Comparison Lost", bd.SOURCE_PROMOTION))

	def test_08_tie_goes_to_fnf(self):
		doc = self._sell(self.staff, [("mouse", 1)])
		row = self._row(doc, "mouse")
		self.assertEqual(flt(row.rate), 70)
		self.assertEqual(row.get(FIELD_DISCOUNT_SOURCE), FNF)
		self.assertEqual(self._logs(doc)[0].comparison_outcome, bd.OUTCOME_TIE_FNF)

	def test_09_no_stacking_with_order_discount(self):
		weaker = self._sell(self.staff, [("laptop", 1)], apply_discount_on="Net Total", additional_discount_percentage=10)
		self.assertEqual(flt(weaker.additional_discount_percentage), 0)
		self.assertEqual(flt(weaker.discount_amount), 0)
		self.assertEqual(flt(weaker.net_total), 700)

		stronger = self._sell(self.staff, [("laptop", 1)], apply_discount_on="Net Total", additional_discount_percentage=50)
		self.assertEqual(flt(self._row(stronger, "laptop").rate), 1000)
		self.assertFalse(self._row(stronger, "laptop").get(FIELD_DISCOUNT_SOURCE))
		self.assertEqual(flt(stronger.net_total), 500)
		order = [log for log in self._logs(stronger) if log.log_type == "Order Comparison"]
		self.assertEqual(order[0].discount_source, bd.SOURCE_ORDER_MANUAL)

	def test_10_coupon_vs_fnf(self):
		coupons = self.demo["coupons"]
		if not coupons:
			self.skipTest("POS Coupon (POS Next) is not installed")
		cart = self._cart(("laptop", 1), discount_percentage=30, discount_source=FNF, rate=700)
		weak = validate_coupon(coupons["15"], customer=self.staff, company=self.company, items=cart)
		self.assertFalse(weak["valid"])
		self.assertEqual(weak["reason"], "better_discount_exists")
		self.assertIn("Friends & Family discount (30%) is better than coupon", weak["message_en"])
		self.assertTrue(weak["message_ar"])
		strong = validate_coupon(coupons["40"], customer=self.staff, company=self.company, items=cart)
		self.assertTrue(strong["valid"])
		self.assertEqual(strong["line_updates"][0]["discount_percentage"], 40)
		plain = validate_coupon(coupons["15"], customer=self.public, company=self.company, items=self._cart(("laptop", 1)))
		self.assertTrue(plain["valid"])

		def coupon(pct, code):
			def apply(doc):
				row = self._row(doc, "laptop")
				row.discount_percentage = pct
				row.posnext_coupon_code = code
			return apply

		kept = self._sell(self.staff, [("laptop", 1)], before_insert=coupon(40, coupons["40"]))
		self.assertEqual(flt(self._row(kept, "laptop").rate), 600)
		self.assertEqual(self._logs(kept)[0].discount_source, bd.SOURCE_COUPON)
		replaced = self._sell(self.staff, [("laptop", 1)], before_insert=coupon(15, coupons["15"]))
		self.assertEqual(flt(self._row(replaced, "laptop").rate), 700)
		self.assertFalse(self._row(replaced, "laptop").posnext_coupon_code)

	# ---- 7. caps (PN-129) ----

	def test_11_per_order_cap(self):
		self._settings(max_discount_per_order=100)
		doc = self._sell(self.staff, [("laptop", 1)])
		self.assertEqual(flt(self._row(doc, "laptop").rate), 900)
		self.assertEqual(flt(self._row(doc, "laptop").get(FIELD_FNF_AMOUNT)), 100)
		caps = self._payload(doc)["caps"]
		self.assertEqual((caps["cap_hit"], caps["fnf_uncapped_total"], caps["fnf_applied_total"]), ("order", 300, 100))
		self.assertIn("per-order maximum of 100.00", caps["message_en"])
		self.assertTrue(caps["message_ar"])
		types = [log.log_type for log in self._logs(doc)]
		self.assertEqual(sorted(types), ["Cap Hit", "Discount Applied"])

		split = self._sell(self.staff, [("laptop", 1), ("phone", 1)])  # 300 + 150 → 100
		amounts = sorted(flt(row.get(FIELD_FNF_AMOUNT)) for row in split.items)
		self.assertLessEqual(sum(amounts), 100)
		self.assertGreater(sum(amounts), 99.9)
		self.assertAlmostEqual(amounts[1] / amounts[0], 2, places=1)  # proportional to 300 : 150

	def test_12_monthly_cap_and_return_credit(self):
		self._settings(max_discount_per_customer_month=500)
		first = self._sell(self.staff, [("laptop", 1)])
		self.assertEqual(flt(self._row(first, "laptop").rate), 700)  # 300 used

		second = self._sell(self.staff, [("laptop", 1)])
		self.assertEqual(flt(self._row(second, "laptop").rate), 800)  # 200 left
		self.assertEqual(self._payload(second)["caps"]["cap_hit"], "monthly")
		self.assertIn("reduced to 200.00", self._payload(second)["message_en"])

		third = self._sell(self.staff, [("phone", 1)])
		self.assertEqual(flt(self._row(third, "phone").rate), 500)
		self.assertIn("Monthly Friends & Family limit of 500.00 reached", self._payload(third)["message_en"])
		self.assertEqual(self._logs(third, item_code=self.items["phone"])[0].log_type, "Cap Hit")
		self.assertEqual(get_monthly_usage(self.staff, self.today), 500)

		ret = self._return(first)
		self.assertEqual(flt(ret.items[0].get(FIELD_FNF_AMOUNT)), -300)
		self.assertEqual(get_monthly_usage(self.staff, self.today), 200)
		fourth = self._sell(self.staff, [("laptop", 1)])
		self.assertEqual(flt(self._row(fourth, "laptop").rate), 700)  # 300 allowance again

	# ---- 8. program end date / disabled → F&F stops immediately (PN-124) ----

	def test_13_program_expiry_stops_fnf_immediately(self):
		draft = self._sell(self.staff, [("laptop", 1)], submit=False)
		self.assertEqual(flt(self._row(draft, "laptop").rate), 700)

		self._settings(end_date=add_days(self.today, -1), start_date=add_days(self.today, -30))
		self.assertEqual(flt(self._row(self._sell(self.staff, [("laptop", 1)]), "laptop").rate), 1000)
		ff = apply_offers(
			{"customer": self.staff, "pos_profile": self.profile, "company": self.company,
				"posting_date": self.today, "items": self._cart(("laptop", 1))}
		)["friends_and_family"]
		self.assertFalse(ff["eligible"])

		# The draft priced while the program was live loses F&F on its next save.
		draft.reload()
		draft.save()
		self.assertEqual(flt(self._row(draft, "laptop").rate), 1000)
		self._pay_and_submit(draft)
		self.assertFalse([log for log in self._logs(draft) if log.log_type == "Discount Applied"])

		self._settings(start_date=add_days(self.today, 1), end_date=None)
		self.assertEqual(flt(self._row(self._sell(self.staff, [("laptop", 1)]), "laptop").rate), 1000)
		self._settings(enabled=0)
		self.assertEqual(flt(self._row(self._sell(self.staff, [("laptop", 1)]), "laptop").rate), 1000)

	# ---- cancel / return → log + report netting (PN-131/132) ----

	def test_14_cancel_return_log_and_report_netting(self):
		first = self._sell(self.staff, [("laptop", 1)])  # F&F 300
		second = self._sell(self.staff, [("phone", 2)])  # F&F 300
		ret = self._return(second, keep={self.items["phone"]: 1})  # −150
		self._cancel(first)

		first_logs = self._logs(first)
		self.assertTrue(all(log.is_cancelled for log in first_logs))
		self.assertEqual(
			sorted((log.log_type, flt(log.discount_amount)) for log in first_logs),
			[("Discount Applied", 300), ("Discount Reversed", -300)],
		)
		self.assertEqual([(log.log_type, flt(log.discount_amount)) for log in self._logs(ret)], [("Discount Returned", -150)])

		data, summary = self._report(self.staff)
		invoices = {row["invoice"]: row for row in data if not row.get("is_total_row")}
		self.assertEqual(set(invoices), {second.name, ret.name})  # cancelled invoice gone
		self.assertEqual(flt(summary["F&F Discount (net)"]), 150)
		self.assertEqual(summary["F&F Orders"], 1)
		self.assertEqual(summary["Returns"], 1)
		self.assertEqual(flt(summary["Net Sales"]), 350)  # 700 − 350
		# Reconciles with the submitted invoices themselves.
		direct = frappe.db.sql(
			f"""select sum(i.`{FIELD_FNF_AMOUNT}` * p.conversion_rate) from `tabSales Invoice Item` i
			join `tabSales Invoice` p on p.name = i.parent
			where p.docstatus = 1 and p.customer = %s and p.posting_date = %s""",
			(self.staff, self.today),
		)[0][0]
		self.assertEqual(flt(direct), 150)

	def test_15_settings_and_eligibility_changes_are_logged(self):
		self._settings(discount_percentage=25)
		change = frappe.get_all(
			LOG, filters={"log_type": "Settings Changed"}, fields=["details"], order_by="creation desc", limit=1
		)[0]
		self.assertIn("discount_percentage", change.details)
		customer = frappe.get_doc("Customer", self.public)
		customer.set(FLAG_FIELD, 1)
		customer.save()
		self.assertEqual(
			frappe.get_all(LOG, filters={"reference_name": self.public, "log_type": "Eligibility Applied"}, pluck="reason"),
			["customer_flag"],
		)
		doc = self._sell(self.public, [("laptop", 1)])
		self.assertEqual(flt(self._row(doc, "laptop").rate), 750)  # now eligible at 25%

	def test_16_seed_helper_guard_and_idempotency(self):
		with patch.object(friends_and_family_demo, "cint_conf", return_value=0):
			self.assertRaises(frappe.PermissionError, setup_demo_data, prefix=PREFIX)
		before = {dt: frappe.db.count(dt) for dt in ("Customer", "Item", "Item Price", "Pricing Rule", "POS Coupon")}
		again = build_demo_data(prefix=PREFIX, company=self.company, price_list=self.demo["price_list"])
		after = {dt: frappe.db.count(dt) for dt in before}
		self.assertEqual(before, after)  # re-running creates nothing new
		self.assertEqual(again["customers"], self.demo["customers"])
