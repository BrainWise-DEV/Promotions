# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.promotions.friends_and_family_testing import approved_domain_rows, give_verified_email
from posnext_promotions.api.friends_and_family import apply_friends_and_family_discount
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
	REASON_APPLIED,
	REASON_INELIGIBLE,
	REASON_OTHER_BETTER,
	REASON_EXCLUDED,
	REASON_NO_COVERED_ITEMS,
	REASON_OUT_OF_SCOPE,
	REASON_PROGRAM_INACTIVE,
	REASON_PROTECTED,
	apply_friends_and_family_on_invoice,
	apply_friends_and_family_to_items,
	compute_friends_and_family_discounts,
)
from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
	ensure_customer_flag_field,
)

DATE = "2026-10-05"
FF_GROUP = "_Test FF Apply Customer Group"
OTHER_GROUP = "_Test FF Apply Other Customer Group"
ELIGIBLE = "_Test FF Apply Eligible Customer"
INELIGIBLE = "_Test FF Apply Plain Customer"
BRAND = "_Test FF Apply Brand"
EXCLUDED_BRAND = "_Test FF Apply Excluded Brand"
ITEM = "_Test FF Apply Item"
EXCLUDED_ITEM = "_Test FF Apply Excluded Item"
GROUP_ITEM = "_Test FF Apply Group Item"
PARENT_GROUP = "_Test FF Apply Parent Group"
CHILD_GROUP = "_Test FF Apply Child Group"
OTHER_ITEM_GROUP = "_Test FF Apply Other Item Group"


class TestFriendsAndFamilyApply(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()

		root_cg = frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ""}) or (
			"All Customer Groups"
		)
		for name in (FF_GROUP, OTHER_GROUP):
			if not frappe.db.exists("Customer Group", name):
				frappe.get_doc(
					{
						"doctype": "Customer Group",
						"customer_group_name": name,
						"parent_customer_group": root_cg,
						"is_group": 0,
					}
				).insert()

		for name, group in ((ELIGIBLE, FF_GROUP), (INELIGIBLE, OTHER_GROUP)):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": group}).insert()
			frappe.db.set_value("Customer", name, {FLAG_FIELD: 0, "disabled": 0})
			give_verified_email(name)

		for brand in (BRAND, EXCLUDED_BRAND):
			if not frappe.db.exists("Brand", brand):
				frappe.get_doc({"doctype": "Brand", "brand": brand}).insert()

		root_ig = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""}) or "All Item Groups"
		for name, parent, is_group in (
			(PARENT_GROUP, root_ig, 1),
			(CHILD_GROUP, PARENT_GROUP, 0),
			(OTHER_ITEM_GROUP, root_ig, 0),
		):
			if not frappe.db.exists("Item Group", name):
				frappe.get_doc(
					{
						"doctype": "Item Group",
						"item_group_name": name,
						"parent_item_group": parent,
						"is_group": is_group,
					}
				).insert()
		frappe.cache().delete_value(f"item_group_descendants:{PARENT_GROUP}")

		for code, brand, group in (
			(ITEM, BRAND, CHILD_GROUP),
			(EXCLUDED_ITEM, EXCLUDED_BRAND, OTHER_ITEM_GROUP),
			(GROUP_ITEM, BRAND, CHILD_GROUP),
		):
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

	# ---- coverage --------------------------------------------------------

	def test_excluded_brand_not_covered_on_entire_cart(self):
		self._configure(excluded_brands=[{"brand": EXCLUDED_BRAND}])
		lines = [self._line(ITEM, brand=BRAND), self._line(EXCLUDED_ITEM, brand=EXCLUDED_BRAND)]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 1)
		self.assertEqual(result["items"][0]["reason"], REASON_APPLIED)
		self.assertEqual(result["items"][1]["reason"], REASON_EXCLUDED)
		self.assertEqual(lines[0].discount_percentage, 30)
		self.assertEqual(lines[0].discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(lines[1].discount_percentage, 0)

	def test_brand_scope_only_listed_brands(self):
		self._configure(discount_scope="Brand", applicable_brands=[{"brand": BRAND}])
		lines = [self._line(ITEM, brand=BRAND), self._line(EXCLUDED_ITEM, brand=EXCLUDED_BRAND)]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 1)
		self.assertTrue(result["items"][0]["applies"])
		self.assertEqual(result["items"][1]["reason"], REASON_OUT_OF_SCOPE)

	def test_item_group_scope_includes_children(self):
		self._configure(
			discount_scope="Item Group", applicable_item_groups=[{"item_group": PARENT_GROUP}]
		)
		lines = [
			self._line(GROUP_ITEM, item_group=CHILD_GROUP, brand=BRAND),
			self._line(EXCLUDED_ITEM, item_group=OTHER_ITEM_GROUP, brand=EXCLUDED_BRAND),
		]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 1)
		self.assertTrue(result["items"][0]["applies"])
		self.assertEqual(result["items"][1]["reason"], REASON_OUT_OF_SCOPE)

	# ---- eligibility / dates ---------------------------------------------

	def test_ineligible_customer_gets_no_discount(self):
		self._configure()
		lines = [self._line()]
		result = apply_friends_and_family_to_items(lines, customer=INELIGIBLE, posting_date=DATE)
		self.assertFalse(result["eligible"])
		self.assertEqual(result["applied_count"], 0)
		self.assertEqual(result["items"][0]["reason"], REASON_INELIGIBLE)
		self.assertEqual(lines[0].discount_percentage, 0)

	def test_after_end_date_auto_apply_stops(self):
		self._configure(end_date="2026-10-01")
		lines = [self._line()]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["reason"], "program_inactive")
		self.assertEqual(result["items"][0]["reason"], REASON_PROGRAM_INACTIVE)
		self.assertEqual(result["discount_percentage"], 0)
		self.assertEqual(lines[0].discount_percentage, 0)

	def test_strips_stale_fnf_when_program_ends(self):
		self._configure()
		lines = [self._line()]
		apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(lines[0].discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)

		self._configure(end_date="2026-10-01")
		apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertNotEqual(lines[0].discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(flt(lines[0].discount_percentage), 0)
		self.assertEqual(flt(lines[0].rate), 100)

	# ---- best-of / no stacking -------------------------------------------

	def test_fnf_replaces_smaller_existing_percentage(self):
		self._configure(discount_percentage=30)
		lines = [self._line(discount_percentage=10, discount_source="auto_discount")]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(result["items"][0]["applies"])
		self.assertTrue(result["items"][0]["replaced_existing"])
		self.assertEqual(lines[0].discount_percentage, 30)
		self.assertEqual(lines[0].discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(flt(lines[0].rate), 70)
		self.assertEqual(flt(lines[0].discount_amount), 30)

	def test_fnf_wins_tie(self):
		self._configure(discount_percentage=25)
		lines = [self._line(discount_percentage=25, discount_source="auto_discount")]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(result["items"][0]["applies"])
		self.assertEqual(lines[0].discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)

	def test_larger_existing_discount_wins(self):
		self._configure(discount_percentage=20)
		lines = [self._line(discount_percentage=40, discount_source="auto_discount", rate=60)]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertFalse(result["items"][0]["applies"])
		self.assertEqual(result["items"][0]["reason"], REASON_OTHER_BETTER)
		self.assertEqual(lines[0].discount_percentage, 40)
		self.assertEqual(lines[0].discount_source, "auto_discount")

	def test_does_not_stack_on_top_of_existing(self):
		"""F&F replaces; it must not become existing + F&F."""
		self._configure(discount_percentage=30)
		lines = [self._line(discount_percentage=10)]
		apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(lines[0].discount_percentage, 30)
		self.assertNotEqual(lines[0].discount_percentage, 40)

	def test_protected_free_item_skipped(self):
		self._configure()
		lines = [self._line(is_free_item=1)]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["items"][0]["reason"], REASON_PROTECTED)
		self.assertEqual(lines[0].discount_percentage, 0)

	# ---- rate is central / cashier cannot override -----------------------

	def test_client_supplied_percentage_is_ignored(self):
		self._configure(discount_percentage=30)
		# Client tries to force 90% while claiming F&F (list price stays 100).
		lines = [
			self._line(
				discount_percentage=90,
				discount_source=DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
			)
		]
		lines[0].rate = 10  # forged net rate; list price remains 100
		apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(lines[0].discount_percentage, 30)
		self.assertEqual(flt(lines[0].rate), 70)

	def test_api_mutate_false_does_not_change_lines(self):
		self._configure(discount_percentage=30)
		lines = [self._line()]
		payload = apply_friends_and_family_discount(
			customer=ELIGIBLE, items=lines, posting_date=DATE, mutate=0
		)
		self.assertTrue(payload["items"][0]["applies"])
		self.assertEqual(payload["discount_percentage"], 30)
		# Original line untouched when mutate=0 (compute path).
		self.assertEqual(flt(lines[0].discount_percentage), 0)

	def test_api_mutate_stamps_lines(self):
		self._configure(discount_percentage=15)
		lines = [dict(self._line())]
		payload = apply_friends_and_family_discount(
			customer=ELIGIBLE, items=lines, posting_date=DATE, mutate=1
		)
		self.assertEqual(payload["applied_count"], 1)
		self.assertEqual(payload["discount_percentage"], 15)
		# API rebuilds _dict copies; check payload amounts.
		self.assertEqual(payload["items"][0]["discount_percentage"], 15)
		self.assertEqual(payload["items"][0]["discount_amount"], 15)

	def test_compute_enriches_brand_from_item(self):
		self._configure(discount_scope="Brand", applicable_brands=[{"brand": BRAND}])
		# No brand on the payload — must be backfilled from Item.
		payload = compute_friends_and_family_discounts(
			customer=ELIGIBLE,
			items=[{"item_code": ITEM, "qty": 2, "price_list_rate": 50}],
			posting_date=DATE,
		)
		self.assertTrue(payload["items"][0]["applies"])
		self.assertEqual(payload["items"][0]["discount_amount"], 30)  # 30% of 100

	# ---- invoice validate hook ------------------------------------------

	def test_invoice_hook_enforces_settings_rate(self):
		self._configure(discount_percentage=30)
		item = self._line(
			discount_percentage=90,
			discount_source=DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
			brand=BRAND,
		)
		item.rate = 10
		doc = frappe._dict(
			{
				"doctype": "POS Invoice",
				"customer": ELIGIBLE,
				"posting_date": DATE,
				"is_pos": 1,
				"items": [item],
			}
		)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(item.discount_percentage, 30)
		self.assertEqual(flt(item.rate), 70)

	def test_invoice_hook_skips_non_pos(self):
		self._configure()
		item = self._line()
		doc = frappe._dict(
			{
				"doctype": "Sales Invoice",
				"customer": ELIGIBLE,
				"posting_date": DATE,
				"is_pos": 0,
				"items": [item],
			}
		)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(item.discount_percentage), 0)


	# ---- PN-127 inclusions / exclusions ---------------------------------

	def test_mixed_cart_excluded_item_stays_full_price(self):
		self._configure(excluded_items=[{"item_code": EXCLUDED_ITEM}])
		lines = [self._line(ITEM, brand=BRAND), self._line(EXCLUDED_ITEM, brand=BRAND)]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 1)
		self.assertEqual(result["items"][0]["reason"], REASON_APPLIED)
		self.assertEqual(result["items"][1]["reason"], REASON_EXCLUDED)
		self.assertEqual(lines[0].discount_percentage, 30)
		self.assertEqual(lines[1].discount_percentage, 0)

	def test_excluded_item_wins_over_brand_scope(self):
		self._configure(
			discount_scope="Brand",
			applicable_brands=[{"brand": BRAND}],
			excluded_items=[{"item_code": ITEM}],
		)
		lines = [self._line(ITEM, brand=BRAND)]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 0)
		self.assertEqual(result["items"][0]["reason"], REASON_EXCLUDED)

	def test_excluded_item_group_wins_over_entire_cart(self):
		self._configure(excluded_item_groups=[{"item_group": OTHER_ITEM_GROUP}])
		lines = [
			self._line(ITEM, brand=BRAND, item_group=CHILD_GROUP),
			self._line(EXCLUDED_ITEM, brand=EXCLUDED_BRAND, item_group=OTHER_ITEM_GROUP),
		]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 1)
		self.assertEqual(result["items"][1]["reason"], REASON_EXCLUDED)

	def test_all_brands_excluded_clear_message(self):
		self._configure(excluded_brands=[{"brand": BRAND}, {"brand": EXCLUDED_BRAND}])
		lines = [
			self._line(ITEM, brand=BRAND),
			self._line(EXCLUDED_ITEM, brand=EXCLUDED_BRAND),
		]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertTrue(result["eligible"])
		self.assertEqual(result["applied_count"], 0)
		self.assertEqual(result["reason"], REASON_NO_COVERED_ITEMS)
		self.assertIn("excluded", result["message"].lower())
		self.assertEqual(result["items"][0]["reason"], REASON_EXCLUDED)
		self.assertEqual(result["items"][1]["reason"], REASON_EXCLUDED)

	def test_item_code_scope_applies_only_listed(self):
		self._configure(discount_scope="Item Code", applicable_items=[{"item_code": ITEM}])
		lines = [self._line(ITEM, brand=BRAND), self._line(EXCLUDED_ITEM, brand=EXCLUDED_BRAND)]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 1)
		self.assertTrue(result["items"][0]["applies"])
		self.assertEqual(result["items"][1]["reason"], REASON_OUT_OF_SCOPE)

	def test_settings_exclusion_change_recalculates_next_apply(self):
		self._configure()
		lines = [self._line(EXCLUDED_ITEM, brand=EXCLUDED_BRAND)]
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 1)
		self._configure(excluded_items=[{"item_code": EXCLUDED_ITEM}])
		result = apply_friends_and_family_to_items(lines, customer=ELIGIBLE, posting_date=DATE)
		self.assertEqual(result["applied_count"], 0)
		self.assertEqual(result["items"][0]["reason"], REASON_EXCLUDED)
		self.assertEqual(flt(lines[0].discount_percentage), 0)
