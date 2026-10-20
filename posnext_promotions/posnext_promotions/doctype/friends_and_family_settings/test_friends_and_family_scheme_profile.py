# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""F&F follow-ups.

1. Promotional Scheme ``apply_with_ff`` ("Apply with F&F"):
   checked   → the scheme's discount competes with F&F (larger wins, tie → F&F);
   unchecked → where F&F covers the line only F&F applies, the scheme is skipped.
2. POS Profile ``allow_friends_family`` ("Allow F&F"):
   unchecked → F&F is off for the whole profile (no discount, stamp stripped,
   API answers "not applicable", nothing logged).
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows, give_verified_email
from posnext_promotions.api.friends_and_family import (
	apply_friends_and_family_discount,
	check_friends_and_family_eligibility,
	get_friends_and_family_program,
)
from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions.friends_and_family import profile_allows_ff
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
	REASON_APPLIED,
	REASON_OTHER_BETTER,
	apply_friends_and_family_on_invoice,
	apply_friends_and_family_to_items,
	compute_friends_and_family_discounts,
)
from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
	REASON_PROFILE_NOT_ALLOWED,
	ensure_customer_flag_field,
	get_customer_eligibility,
)
from posnext_promotions.promotions.friends_and_family_log import LOG_DOCTYPE, log_invoice_submit

DATE = nowdate()
FF_GROUP = "_Test FF Scheme Customer Group"
ELIGIBLE = "_Test FF Scheme Eligible Customer"
ITEM = "_Test FF Scheme Item"
SCHEME = "_Test FF Scheme Apply With FF"


def _first_company():
	return frappe.defaults.get_global_default("company") or frappe.db.get_value("Company", {})


class TestFriendsAndFamilySchemeAndProfile(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()
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
		if not frappe.db.exists("Customer", ELIGIBLE):
			frappe.get_doc(
				{"doctype": "Customer", "customer_name": ELIGIBLE, "customer_group": FF_GROUP}
			).insert()
		frappe.db.set_value("Customer", ELIGIBLE, {FLAG_FIELD: 0, "disabled": 0})
		give_verified_email(ELIGIBLE)
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or "Products"
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": ITEM,
					"item_name": ITEM,
					"item_group": item_group,
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()

	def setUp(self):
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
			}
		)
		doc.save()

	# ---- helpers ------------------------------------------------------------

	def _scheme(self, apply_with_ff):
		"""A real Promotional Scheme (so a generated Pricing Rule points at it)."""
		if frappe.db.exists("Promotional Scheme", SCHEME):
			frappe.delete_doc("Promotional Scheme", SCHEME, force=True, ignore_permissions=True)
		scheme = frappe.get_doc(
			{
				"doctype": "Promotional Scheme",
				"name": SCHEME,
				"company": _first_company(),
				"apply_on": "Item Code",
				"selling": 1,
				"buying": 0,
				"valid_from": nowdate(),
				"apply_with_ff": apply_with_ff,
				"items": [{"item_code": ITEM}],
				"price_discount_slabs": [
					{
						"rule_description": "ff test",
						"min_qty": 0,
						"rate_or_discount": "Discount Percentage",
						"discount_percentage": 10,
					}
				],
			}
		)
		scheme.insert(ignore_permissions=True)
		self.addCleanup(
			lambda: frappe.db.exists("Promotional Scheme", SCHEME)
			and frappe.delete_doc("Promotional Scheme", SCHEME, force=True, ignore_permissions=True)
		)
		rules = frappe.get_all("Pricing Rule", filters={"promotional_scheme": SCHEME}, pluck="name")
		self.assertTrue(rules, "scheme generated no pricing rule")
		return rules[0]

	def _line(self, rule, pct):
		return frappe._dict(
			{
				"item_code": ITEM,
				"qty": 1,
				"price_list_rate": 100,
				"rate": 100 - pct,
				"discount_percentage": pct,
				"discount_amount": pct,
				"pricing_rules": rule,
				"discount_source": "item_level_promotion",
			}
		)

	def _profile(self, allow):
		name = frappe.db.get_value("POS Profile", {})
		if not name:
			self.skipTest("no POS Profile on the site")
		old = frappe.db.get_value("POS Profile", name, "allow_friends_family")
		frappe.db.set_value("POS Profile", name, "allow_friends_family", allow)
		self.addCleanup(lambda: frappe.db.set_value("POS Profile", name, "allow_friends_family", old))
		return name

	# ---- fields exist -------------------------------------------------------

	def test_fields_installed_with_defaults(self):
		scheme_field = frappe.get_meta("Promotional Scheme").get_field("apply_with_ff")
		self.assertIsNotNone(scheme_field)
		self.assertEqual(scheme_field.fieldtype, "Check")
		self.assertEqual(str(scheme_field.default or "0"), "0")
		profile_field = frappe.get_meta("POS Profile").get_field("allow_friends_family")
		self.assertIsNotNone(profile_field)
		self.assertEqual(profile_field.fieldtype, "Check")
		self.assertEqual(str(profile_field.default), "1")

	# ---- Promotional Scheme: Apply with F&F ----------------------------------

	def test_unchecked_scheme_is_skipped_even_when_bigger(self):
		rule = self._scheme(apply_with_ff=0)
		line = self._line(rule, 40)  # 40% promo > 30% F&F
		payload = apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		result = payload["items"][0]
		self.assertEqual(result["reason"], REASON_APPLIED)
		self.assertEqual(flt(line.discount_percentage), 30)
		self.assertEqual(line.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertFalse(line.pricing_rules)  # scheme detached
		self.assertEqual(payload["suppressed_pricing_rules"], [rule])
		decision = result["best_discount"]
		self.assertEqual(decision["winner"], bd.SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(decision["outcome"], bd.OUTCOME_FNF_EXCLUSIVE)
		self.assertEqual(decision["suppressed"], [bd.SOURCE_PROMOTION])
		self.assertIn("Friends & Family", decision["message_en"])

	def test_unchecked_scheme_not_covered_by_ff_keeps_scheme(self):
		rule = self._scheme(apply_with_ff=0)
		frappe.get_single("Friends and Family Settings").update(
			{"excluded_items": [{"item_code": ITEM}]}
		).save()
		line = self._line(rule, 10)
		apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(flt(line.discount_percentage), 10)
		self.assertEqual(line.pricing_rules, rule)

	def test_checked_scheme_larger_promo_wins(self):
		rule = self._scheme(apply_with_ff=1)
		line = self._line(rule, 40)
		payload = apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(payload["items"][0]["reason"], REASON_OTHER_BETTER)
		self.assertEqual(flt(line.discount_percentage), 40)
		self.assertEqual(line.pricing_rules, rule)

	def test_checked_scheme_smaller_promo_loses(self):
		rule = self._scheme(apply_with_ff=1)
		line = self._line(rule, 10)
		payload = apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(payload["items"][0]["best_discount"]["outcome"], bd.OUTCOME_FNF_BETTER)
		self.assertEqual(flt(line.discount_percentage), 30)
		self.assertFalse(line.pricing_rules)

	def test_checked_scheme_tie_goes_to_ff(self):
		rule = self._scheme(apply_with_ff=1)
		line = self._line(rule, 30)
		payload = apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(payload["items"][0]["best_discount"]["outcome"], bd.OUTCOME_TIE_FNF)
		self.assertEqual(line.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)

	def test_pricing_rule_without_scheme_still_competes(self):
		line = self._line("_Test FF Rule Without Scheme", 40)
		apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(flt(line.discount_percentage), 40)

	def test_compute_is_read_only_and_reports_exclusive(self):
		rule = self._scheme(apply_with_ff=0)
		line = self._line(rule, 40)
		payload = compute_friends_and_family_discounts(customer=ELIGIBLE, items=[line], posting_date=DATE)
		self.assertTrue(payload["items"][0]["applies"])
		self.assertEqual(flt(line.discount_percentage), 40)  # untouched

	# ---- POS Profile: Allow F&F ------------------------------------------------

	def test_profile_helper_defaults_to_allowed(self):
		self.assertTrue(profile_allows_ff(None))
		self.assertTrue(profile_allows_ff(""))
		self.assertTrue(profile_allows_ff("_No Such POS Profile_"))
		name = self._profile(1)
		self.assertTrue(profile_allows_ff(name))
		frappe.db.set_value("POS Profile", name, "allow_friends_family", 0)
		self.assertFalse(profile_allows_ff(name))

	def test_profile_allowed_applies_normally(self):
		name = self._profile(1)
		line = self._line("_Test FF Rule Without Scheme", 0)
		line.pricing_rules = None
		line.discount_source = None
		payload = apply_friends_and_family_discount(
			customer=ELIGIBLE, items=[line], posting_date=DATE, pos_profile=name
		)
		self.assertTrue(payload["eligible"])
		self.assertEqual(payload["applied_count"], 1)

	def test_profile_disabled_turns_everything_off(self):
		name = self._profile(0)

		eligibility = get_customer_eligibility(ELIGIBLE, DATE, name)
		self.assertFalse(eligibility.eligible)
		self.assertEqual(eligibility.reason, REASON_PROFILE_NOT_ALLOWED)
		self.assertIsNone(eligibility.program)

		check = check_friends_and_family_eligibility(ELIGIBLE, DATE, name)
		self.assertFalse(check["eligible"])
		self.assertEqual(check["reason"], REASON_PROFILE_NOT_ALLOWED)
		self.assertEqual(check["discount_percentage"], 0)
		self.assertFalse(check["program_active"])

		self.assertEqual(get_friends_and_family_program(DATE, name), {"active": False})
		# Same call without the profile still sees the live program.
		self.assertTrue(get_friends_and_family_program(DATE)["active"])

		line = frappe._dict(
			{"item_code": ITEM, "qty": 1, "price_list_rate": 100, "rate": 100, "discount_percentage": 0}
		)
		payload = apply_friends_and_family_discount(
			customer=ELIGIBLE, items=[line], posting_date=DATE, pos_profile=name
		)
		self.assertFalse(payload["eligible"])
		self.assertEqual(payload["reason"], REASON_PROFILE_NOT_ALLOWED)
		self.assertEqual(payload["applied_count"], 0)
		self.assertEqual(flt(line.discount_percentage), 0)
		self.assertFalse(payload["items"][0]["applies"])

		read_only = apply_friends_and_family_discount(
			customer=ELIGIBLE, items=[line], posting_date=DATE, mutate=0, pos_profile=name
		)
		self.assertEqual(read_only["applied_count"], 0)

	def test_profile_disabled_strips_stale_stamp_and_restores_promo(self):
		name = self._profile(1)
		line = self._line("_Test FF Rule Without Scheme", 10)
		apply_friends_and_family_to_items([line], customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(line.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)

		frappe.db.set_value("POS Profile", name, "allow_friends_family", 0)
		payload = apply_friends_and_family_to_items(
			[line], customer=ELIGIBLE, posting_date=DATE, pos_profile=name
		)
		self.assertNotEqual(line.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(payload["applied_count"], 0)
		self.assertEqual(flt(line.discount_percentage), 10)  # the replaced promotion is back

	def test_profile_disabled_invoice_hook_and_log(self):
		name = self._profile(0)
		row = frappe._dict(
			{"item_code": ITEM, "qty": 1, "price_list_rate": 100, "rate": 100, "discount_percentage": 0, "discount_amount": 0}
		)
		doc = frappe._dict(
			{
				"doctype": "POS Invoice",
				"customer": ELIGIBLE,
				"posting_date": DATE,
				"pos_profile": name,
				"items": [row],
				"flags": frappe._dict(),
			}
		)
		before = frappe.db.count(LOG_DOCTYPE)
		payload = apply_friends_and_family_on_invoice(doc)
		self.assertEqual(payload["reason"], REASON_PROFILE_NOT_ALLOWED)
		self.assertEqual(flt(row.discount_percentage), 0)
		log_invoice_submit(doc)
		self.assertEqual(frappe.db.count(LOG_DOCTYPE), before)

		# Same invoice on an allowed profile gets F&F.
		frappe.db.set_value("POS Profile", name, "allow_friends_family", 1)
		doc.flags = frappe._dict()
		row.discount_percentage = 0
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(row.discount_percentage), 30)
