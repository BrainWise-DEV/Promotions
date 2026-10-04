# Copyright (c) 2026, BrainWise and contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.api.coupon_engine import (
	CouponScope,
	apply_coupon_to_items,
	validate_invoice_coupon_lines,
)

PREFIX = "_CPNSCOPE"
PARENT_GROUP = f"{PREFIX} Footwear"
CHILD_GROUP = f"{PREFIX} Running Shoes"
OTHER_GROUP = f"{PREFIX} Bags"
BRAND = f"{PREFIX} Brand"
OTHER_BRAND = f"{PREFIX} Other Brand"
TEMPLATE = f"{PREFIX}-TEMPLATE"
VARIANT = f"{PREFIX}-TEMPLATE-RED"
SHOE = f"{PREFIX}-SHOE"
BAG = f"{PREFIX}-BAG"


def _ensure(doctype, name, **fields):
	if frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name)
	doc = frappe.get_doc({"doctype": doctype, **fields})
	doc.insert(ignore_permissions=True)
	return doc


def _cart_line(item_code, qty=1, rate=100, **extra):
	return {"item_code": item_code, "qty": qty, "price_list_rate": rate, "rate": rate, **extra}


class TestCouponScope(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		root_group = frappe.db.get_value(
			"Item Group", {"is_group": 1, "parent_item_group": ["in", ["", None]]}
		)
		cls.company = frappe.db.get_value("Company", {}, "name")
		_ensure(
			"Item Group", PARENT_GROUP, item_group_name=PARENT_GROUP, parent_item_group=root_group, is_group=1
		)
		_ensure("Item Group", CHILD_GROUP, item_group_name=CHILD_GROUP, parent_item_group=PARENT_GROUP)
		_ensure("Item Group", OTHER_GROUP, item_group_name=OTHER_GROUP, parent_item_group=root_group)
		_ensure("Brand", BRAND, brand=BRAND)
		_ensure("Brand", OTHER_BRAND, brand=OTHER_BRAND)

		uom = frappe.db.get_value("UOM", {}, "name")
		for code, group, brand in (
			(TEMPLATE, CHILD_GROUP, BRAND),
			(VARIANT, CHILD_GROUP, BRAND),
			(SHOE, CHILD_GROUP, BRAND),
			(BAG, OTHER_GROUP, OTHER_BRAND),
		):
			_ensure(
				"Item",
				code,
				item_code=code,
				item_name=code,
				item_group=group,
				brand=brand,
				stock_uom=uom,
				is_stock_item=0,
			)
		frappe.db.set_value("Item", VARIANT, "variant_of", TEMPLATE)

	def _coupon(self, scope, rows=None, code=None, **fields):
		code = code or f"{PREFIX}{frappe.generate_hash(length=6)}".upper()
		table = {
			"Item Code": ("applicable_items", "item_code"),
			"Item Group": ("applicable_item_groups", "item_group"),
			"Brand": ("applicable_brands", "brand"),
		}.get(scope)
		doc = frappe.get_doc(
			{
				"doctype": "POS Coupon",
				"coupon_name": code,
				"coupon_code": code,
				"coupon_type": "Promotional",
				"company": self.company,
				"discount_type": "Percentage",
				"discount_percentage": 10,
				"apply_on": "Net Total",
				"apply_scope": scope,
				**fields,
			}
		)
		if table:
			for value in rows or []:
				doc.append(table[0], {table[1]: value})
		doc.insert(ignore_permissions=True)
		return doc

	def test_item_code_scope_matches_listed_items_and_variants_of_listed_templates(self):
		coupon = self._coupon("Item Code", [TEMPLATE, BAG])
		cart = [_cart_line(VARIANT), _cart_line(SHOE), _cart_line(BAG)]

		result = apply_coupon_to_items(coupon, cart)

		self.assertTrue(result["valid"])
		self.assertEqual(result["eligible_item_codes"], [VARIANT, BAG])
		self.assertEqual(result["total_discount"], 20)

	def test_item_group_scope_includes_descendant_groups(self):
		coupon = self._coupon("Item Group", [PARENT_GROUP])
		cart = [_cart_line(SHOE), _cart_line(BAG)]

		self.assertEqual(apply_coupon_to_items(coupon, cart)["eligible_item_codes"], [SHOE])

	def test_brand_scope_with_multiple_rows(self):
		coupon = self._coupon("Brand", [BRAND, OTHER_BRAND])
		cart = [_cart_line(SHOE), _cart_line(BAG)]

		self.assertEqual(apply_coupon_to_items(coupon, cart)["eligible_item_codes"], [SHOE, BAG])

	def test_scope_uses_item_master_not_client_item_group(self):
		coupon = self._coupon("Item Group", [CHILD_GROUP])
		cart = [_cart_line(BAG, item_group=CHILD_GROUP, brand=BRAND)]

		self.assertFalse(CouponScope(coupon, cart).matches(cart[0]))

	def test_cart_entirely_out_of_scope_is_rejected_with_scope_message(self):
		coupon = self._coupon("Brand", [OTHER_BRAND])

		result = apply_coupon_to_items(coupon, [_cart_line(SHOE)])

		self.assertFalse(result["valid"])
		self.assertIn("scope", result["message"])

	def test_restricted_scope_requires_rows(self):
		with self.assertRaises(frappe.ValidationError):
			self._coupon("Brand", [])

	def test_duplicate_scope_rows_are_collapsed(self):
		coupon = self._coupon("Brand", [BRAND, BRAND])
		self.assertEqual([row.brand for row in coupon.applicable_brands], [BRAND])

	def test_legacy_single_field_is_moved_into_table_on_save(self):
		coupon = self._coupon("Brand", [], applicable_brand=BRAND)
		self.assertEqual([row.brand for row in coupon.applicable_brands], [BRAND])
		self.assertFalse(coupon.applicable_brand)

	def test_patch_migrates_legacy_values_once(self):
		from posnext_promotions.patches.v2_5_0 import migrate_coupon_scope_to_tables

		coupon = self._coupon("Brand", [BRAND])
		frappe.db.delete("POS Coupon Brand", {"parent": coupon.name})
		frappe.db.set_value("POS Coupon", coupon.name, "applicable_brand", OTHER_BRAND)

		migrate_coupon_scope_to_tables.execute()
		migrate_coupon_scope_to_tables.execute()

		rows = frappe.get_all("POS Coupon Brand", filters={"parent": coupon.name}, pluck="brand")
		self.assertEqual(rows, [OTHER_BRAND])

	def test_api_create_and_update_coupon_scope_tables(self):
		from posnext_promotions.api.promotions import create_coupon, update_coupon

		code = f"{PREFIX}API".upper()
		created = create_coupon(
			{
				"coupon_name": code,
				"coupon_code": code,
				"coupon_type": "Promotional",
				"company": self.company,
				"discount_type": "Percentage",
				"discount_percentage": 10,
				"apply_scope": "Item Code",
				"applicable_items": [SHOE, {"item_code": BAG}],
			}
		)
		coupon = frappe.get_doc("POS Coupon", created["coupon_name"])
		self.assertEqual([row.item_code for row in coupon.applicable_items], [SHOE, BAG])

		update_coupon(
			coupon.name,
			{"apply_scope": "Brand", "applicable_items": [], "applicable_brands": [OTHER_BRAND]},
		)
		coupon.reload()
		self.assertEqual(coupon.apply_scope, "Brand")
		self.assertEqual(coupon.applicable_items, [])
		self.assertEqual([row.brand for row in coupon.applicable_brands], [OTHER_BRAND])

	def test_validate_coupon_endpoint_returns_line_updates_for_scoped_lines_only(self):
		from posnext_promotions.api.offers import validate_coupon

		coupon = self._coupon("Item Group", [PARENT_GROUP])
		customer = frappe.db.get_value("Customer", {}, "name")

		result = validate_coupon(
			coupon.coupon_code,
			customer=customer,
			company=self.company,
			items=[_cart_line(BAG), _cart_line(SHOE, qty=2)],
		)

		self.assertTrue(result["valid"])
		self.assertEqual([(u["line_key"], u["item_code"]) for u in result["line_updates"]], [(1, SHOE)])
		self.assertEqual(result["total_discount"], 20)

	def _invoice(self, coupon, lines):
		return frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.company,
				"coupon_code": coupon.coupon_code,
				"items": [{"idx": index + 1, **line} for index, line in enumerate(lines)],
			}
		)

	def test_invoice_accepts_coupon_discount_on_in_scope_line(self):
		coupon = self._coupon("Brand", [BRAND])
		invoice = self._invoice(
			coupon,
			[
				{
					"item_code": SHOE,
					"qty": 2,
					"price_list_rate": 100,
					"rate": 90,
					"amount": 180,
					"discount_percentage": 10,
					"posnext_coupon_code": coupon.coupon_code,
				},
				{"item_code": BAG, "qty": 1, "price_list_rate": 50, "rate": 50, "amount": 50},
			],
		)

		validate_invoice_coupon_lines(invoice)

	def test_invoice_rejects_coupon_tag_on_out_of_scope_line(self):
		coupon = self._coupon("Brand", [BRAND])
		invoice = self._invoice(
			coupon,
			[
				{
					"item_code": BAG,
					"qty": 1,
					"price_list_rate": 50,
					"rate": 45,
					"amount": 45,
					"discount_percentage": 10,
					"posnext_coupon_code": coupon.coupon_code,
				},
			],
		)

		with self.assertRaises(frappe.ValidationError):
			validate_invoice_coupon_lines(invoice)

	def test_invoice_rejects_coupon_discount_above_allowed(self):
		coupon = self._coupon("Brand", [BRAND])
		invoice = self._invoice(
			coupon,
			[
				{
					"item_code": SHOE,
					"qty": 1,
					"price_list_rate": 100,
					"rate": 50,
					"amount": 50,
					"discount_percentage": 50,
					"posnext_coupon_code": coupon.coupon_code,
				},
			],
		)

		with self.assertRaises(frappe.ValidationError):
			validate_invoice_coupon_lines(invoice)

	def test_switching_scope_clears_inactive_tables(self):
		coupon = self._coupon("Brand", [BRAND])
		coupon.apply_scope = "Item Group"
		coupon.append("applicable_item_groups", {"item_group": PARENT_GROUP})
		coupon.save(ignore_permissions=True)
		self.assertEqual(coupon.applicable_brands, [])

		coupon.apply_scope = "All Eligible Items"
		coupon.save(ignore_permissions=True)
		self.assertEqual(coupon.applicable_item_groups, [])

	def test_coupon_list_summary_reads_active_scope_only(self):
		from posnext_promotions.api.promotions import _coupon_scope_summary

		coupon = self._coupon("Brand", [BRAND])
		frappe.get_doc(
			{
				"doctype": "POS Coupon Item Group",
				"parent": coupon.name,
				"parenttype": "POS Coupon",
				"parentfield": "applicable_item_groups",
				"idx": 1,
				"item_group": OTHER_GROUP,
			}
		).db_insert()

		summary = _coupon_scope_summary([frappe._dict(name=coupon.name, apply_scope="Brand")])
		self.assertEqual(summary[coupon.name], [BRAND])

	def test_invoice_rejects_scoped_coupon_without_tagged_lines(self):
		coupon = self._coupon("Brand", [BRAND])
		invoice = self._invoice(
			coupon,
			[
				{
					"item_code": BAG,
					"qty": 1,
					"price_list_rate": 50,
					"rate": 25,
					"amount": 25,
					"discount_percentage": 50,
				}
			],
		)

		with self.assertRaises(frappe.ValidationError):
			validate_invoice_coupon_lines(invoice)

	def test_invoice_allows_scoped_coupon_as_header_discount(self):
		coupon = self._coupon("Brand", [BRAND])
		invoice = self._invoice(coupon, [_cart_line(SHOE)])
		invoice.discount_amount = 10

		validate_invoice_coupon_lines(invoice)

	def test_invoice_ignores_untagged_lines_for_unscoped_coupon(self):
		coupon = self._coupon("All Eligible Items")
		invoice = self._invoice(coupon, [_cart_line(SHOE)])

		validate_invoice_coupon_lines(invoice)

	def test_invoice_tolerance_does_not_grow_a_full_unit_per_qty(self):
		coupon = self._coupon(
			"Brand", [BRAND], discount_type="Amount", discount_amount=50, discount_percentage=0
		)
		invoice = self._invoice(
			coupon,
			[
				{
					"item_code": SHOE,
					"qty": 100,
					"price_list_rate": 100,
					"rate": 99.49,
					"amount": 9949,
					"posnext_coupon_code": coupon.coupon_code,
				},
			],
		)

		with self.assertRaises(frappe.ValidationError):
			validate_invoice_coupon_lines(invoice)

	def test_invoice_rejects_line_tag_for_a_different_coupon(self):
		coupon = self._coupon("Brand", [BRAND])
		invoice = self._invoice(
			coupon,
			[
				{
					"item_code": SHOE,
					"qty": 1,
					"price_list_rate": 100,
					"rate": 90,
					"amount": 90,
					"discount_percentage": 10,
					"posnext_coupon_code": "SOMETHING-ELSE",
				},
			],
		)

		with self.assertRaises(frappe.ValidationError):
			validate_invoice_coupon_lines(invoice)
