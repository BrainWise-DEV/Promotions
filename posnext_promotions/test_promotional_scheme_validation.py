# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.test_promotions import ITEM_A, _resolve_company


def _make_scheme(name, item_code, promotion_type):
	company = _resolve_company()
	scheme = frappe.get_doc(
		{
			"doctype": "Promotional Scheme",
			"name": name,
			"company": company,
			"apply_on": "Item Code",
			"selling": 1,
			"items": [{"item_code": item_code}],
			"price_discount_slabs": [
				{
					"rule_description": "Test discount",
					"rate_or_discount": "Discount Percentage",
					"discount_percentage": 10,
				}
			],
		}
	)
	if frappe.db.has_column("Promotional Scheme", "promotion_type"):
		scheme.promotion_type = promotion_type
	return scheme


class TestPromotionalSchemeValidation(FrappeTestCase):
	def tearDown(self):
		super().tearDown()
		for name in ("_PNXT_TEST_SCHEME_A", "_PNXT_TEST_SCHEME_B"):
			if frappe.db.exists("Promotional Scheme", name):
				frappe.delete_doc("Promotional Scheme", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_duplicate_promotion_type_for_same_item_is_blocked(self):
		if not frappe.db.has_column("Promotional Scheme", "promotion_type"):
			self.skipTest("promotion_type custom field is not installed")

		first = _make_scheme("_PNXT_TEST_SCHEME_A", ITEM_A, "Item Level Discount")
		first.insert(ignore_permissions=True)

		second = _make_scheme("_PNXT_TEST_SCHEME_B", ITEM_A, "Item Level Discount")
		with self.assertRaises(frappe.ValidationError):
			second.insert(ignore_permissions=True)

	def test_different_promotion_type_for_same_item_is_allowed(self):
		if not frappe.db.has_column("Promotional Scheme", "promotion_type"):
			self.skipTest("promotion_type custom field is not installed")

		first = _make_scheme("_PNXT_TEST_SCHEME_A", ITEM_A, "Item Level Discount")
		first.insert(ignore_permissions=True)

		second = _make_scheme("_PNXT_TEST_SCHEME_B", ITEM_A, "GWP")
		second.price_discount_slabs = []
		second.append(
			"product_discount_slabs",
			{
				"rule_description": "GWP",
				"min_qty": 2,
				"free_item": ITEM_A,
				"free_qty": 1,
				"same_item": 1,
			},
		)
		second.insert(ignore_permissions=True)

	def _gwp_scheme(self, name="_PNXT_TEST_SCHEME_A", **slab_fields):
		scheme = _make_scheme(name, ITEM_A, "GWP")
		scheme.price_discount_slabs = []
		row = {
			"rule_description": "GWP",
			"min_qty": 2,
			"max_qty": 0,
			"free_qty": 1,
			"same_item": 1,
		}
		row.update(slab_fields)
		scheme.append("product_discount_slabs", row)
		return scheme

	def test_gwp_rejects_zero_or_negative_min_qty(self):
		for bad_min in (0, -2):
			with self.subTest(min_qty=bad_min):
				scheme = self._gwp_scheme(min_qty=bad_min)
				with self.assertRaises(frappe.ValidationError):
					scheme.insert(ignore_permissions=True)

	def test_gwp_rejects_zero_or_negative_free_qty(self):
		for bad_free in (0, -1):
			with self.subTest(free_qty=bad_free):
				scheme = self._gwp_scheme(free_qty=bad_free)
				with self.assertRaises(frappe.ValidationError):
					scheme.insert(ignore_permissions=True)

	def test_gwp_rejects_invalid_max_qty(self):
		for bad_max in (-1, 1):
			with self.subTest(max_qty=bad_max):
				scheme = self._gwp_scheme(min_qty=2, max_qty=bad_max)
				with self.assertRaises(frappe.ValidationError):
					scheme.insert(ignore_permissions=True)

	def test_gwp_accepts_valid_quantities(self):
		scheme = self._gwp_scheme(min_qty=2, max_qty=0, free_qty=1)
		scheme.insert(ignore_permissions=True)

		scheme2 = self._gwp_scheme(name="_PNXT_TEST_SCHEME_B", min_qty=2, max_qty=4, free_qty=1)
		# Use disable=1 on scheme first so unique promotion_type per item doesn't conflict
		scheme.disable = 1
		scheme.save(ignore_permissions=True)
		scheme2.insert(ignore_permissions=True)

	def test_gwp_disabled_scheme_skips_quantity_guard(self):
		scheme = self._gwp_scheme(min_qty=0, free_qty=1)
		scheme.disable = 1
		scheme.insert(ignore_permissions=True)
