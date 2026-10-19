# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.api.friends_and_family import get_friends_and_family_program
from posnext_promotions.promotions.friends_and_family import get_active_program

BRAND = "_Test FF Brand"
OTHER_BRAND = "_Test FF Other Brand"
PARENT_GROUP = "_Test FF Parent Group"
CHILD_GROUP = "_Test FF Child Group"
OTHER_GROUP = "_Test FF Other Group"


def _line(**values):
	return frappe._dict({"item_code": "_Test FF Item", "qty": 1, "price_list_rate": 100, **values})


class TestFriendsandFamilySettings(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for brand in (BRAND, OTHER_BRAND):
			if not frappe.db.exists("Brand", brand):
				frappe.get_doc({"doctype": "Brand", "brand": brand}).insert()
		root = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""}) or "All Item Groups"
		for name, parent, is_group in (
			(PARENT_GROUP, root, 1),
			(CHILD_GROUP, PARENT_GROUP, 0),
			(OTHER_GROUP, root, 0),
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
			}
		)
		doc.update(values)
		doc.save()
		return doc

	# ---- validation -------------------------------------------------------

	def test_settings_persist(self):
		self._configure(discount_percentage=25, start_date="2026-10-01", end_date="2026-12-31")
		doc = frappe.get_single("Friends and Family Settings")
		self.assertEqual(doc.discount_percentage, 25)
		self.assertEqual(doc.discount_scope, "Entire Cart")
		self.assertEqual(str(doc.end_date), "2026-12-31")

	def test_end_date_before_start_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(start_date="2026-12-01", end_date="2026-11-01")

	def test_percentage_out_of_range_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(discount_percentage=150)

	def test_enabled_with_zero_percentage_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(discount_percentage=0)

	def test_enabled_brand_scope_without_brands_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(discount_scope="Brand")

	def test_enabled_item_group_scope_without_groups_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(discount_scope="Item Group")

	def test_scope_change_clears_unused_tables(self):
		self._configure(discount_scope="Brand", applicable_brands=[{"brand": BRAND}])
		doc = self._configure(applicable_brands=[{"brand": BRAND}])
		self.assertEqual(doc.discount_scope, "Entire Cart")
		self.assertEqual(doc.applicable_brands, [])

	# ---- engine exposure --------------------------------------------------

	def test_disabled_program_does_not_apply(self):
		self._configure(enabled=0)
		self.assertIsNone(get_active_program("2026-10-05"))
		self.assertEqual(get_friends_and_family_program("2026-10-05"), {"active": False})

	def test_outside_date_range_does_not_apply(self):
		self._configure(start_date="2026-10-10", end_date="2026-10-20")
		self.assertIsNone(get_active_program("2026-10-09"))
		self.assertIsNone(get_active_program("2026-10-21"))
		self.assertEqual(get_friends_and_family_program("2026-10-21"), {"active": False})

	def test_date_bounds_are_inclusive(self):
		self._configure(start_date="2026-10-10", end_date="2026-10-20")
		self.assertIsNotNone(get_active_program("2026-10-10"))
		self.assertIsNotNone(get_active_program("2026-10-20"))

	def test_saved_changes_apply_on_next_evaluation(self):
		self._configure(discount_percentage=30)
		self.assertEqual(get_active_program("2026-10-05").line_percentage(_line()), 30)

		self._configure(discount_percentage=15, discount_scope="Brand", applicable_brands=[{"brand": BRAND}])
		program = get_active_program("2026-10-05")
		self.assertEqual(program.line_percentage(_line(brand=BRAND)), 15)
		self.assertEqual(program.line_percentage(_line(brand=OTHER_BRAND)), 0)

		self._configure(end_date="2026-10-01")
		self.assertIsNone(get_active_program("2026-10-05"))

	def test_entire_cart_covers_every_paid_line(self):
		self._configure(excluded_brands=[{"brand": OTHER_BRAND}])
		program = get_active_program("2026-10-05")
		self.assertTrue(program.covers(_line(brand=BRAND)))
		self.assertTrue(program.covers(_line()))
		self.assertFalse(program.covers(_line(brand=OTHER_BRAND)))
		self.assertFalse(program.covers(_line(is_free_item=1)))
		self.assertEqual(program.to_client_payload()["excluded_brands"], [OTHER_BRAND])

	def test_item_group_scope_includes_child_groups(self):
		self._configure(discount_scope="Item Group", applicable_item_groups=[{"item_group": PARENT_GROUP}])
		program = get_active_program("2026-10-05")
		self.assertTrue(program.covers(_line(item_group=PARENT_GROUP)))
		self.assertTrue(program.covers(_line(item_group=CHILD_GROUP)))
		self.assertFalse(program.covers(_line(item_group=OTHER_GROUP)))

	def test_client_payload_reflects_settings(self):
		self._configure(
			discount_percentage=20,
			discount_scope="Brand",
			applicable_brands=[{"brand": BRAND}],
			start_date="2026-10-01",
			end_date="2026-10-31",
		)
		payload = get_friends_and_family_program("2026-10-05")
		self.assertTrue(payload["active"])
		self.assertEqual(payload["discount_percentage"], 20)
		self.assertEqual(payload["discount_scope"], "Brand")
		self.assertEqual(payload["brands"], [BRAND])
		self.assertEqual(payload["end_date"], "2026-10-31")


	def test_excluded_item_never_covered(self):
		item_code = "_Test FF Gift Card"
		if not frappe.db.exists("Item", item_code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"item_group": OTHER_GROUP,
					"brand": BRAND,
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()
		self._configure(excluded_items=[{"item_code": item_code}])
		program = get_active_program("2026-10-05")
		self.assertFalse(program.covers(_line(item_code=item_code, brand=BRAND, item_group=OTHER_GROUP)))
		self.assertTrue(program.covers(_line(brand=BRAND, item_group=CHILD_GROUP)))
		self.assertEqual(program.to_client_payload()["excluded_items"], [item_code])

	def test_excluded_item_group_never_covered(self):
		self._configure(excluded_item_groups=[{"item_group": OTHER_GROUP}])
		program = get_active_program("2026-10-05")
		self.assertFalse(program.covers(_line(item_group=OTHER_GROUP, brand=BRAND)))
		self.assertTrue(program.covers(_line(item_group=CHILD_GROUP, brand=BRAND)))

	def test_item_code_scope_only_listed_items(self):
		item_code = "_Test FF Scoped Item"
		if not frappe.db.exists("Item", item_code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"item_group": CHILD_GROUP,
					"brand": BRAND,
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()
		self._configure(discount_scope="Item Code", applicable_items=[{"item_code": item_code}])
		program = get_active_program("2026-10-05")
		self.assertTrue(program.covers(_line(item_code=item_code)))
		self.assertFalse(program.covers(_line(item_code="_Test FF Item")))

	def test_enabled_item_code_scope_without_items_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self._configure(discount_scope="Item Code")

	def test_exclusion_change_recalculates_on_next_evaluation(self):
		self._configure()
		program = get_active_program("2026-10-05")
		self.assertTrue(program.covers(_line(brand=OTHER_BRAND)))
		self._configure(excluded_brands=[{"brand": OTHER_BRAND}])
		program = get_active_program("2026-10-05")
		self.assertFalse(program.covers(_line(brand=OTHER_BRAND)))

	def test_brand_placeholder_uses_default_company(self):
		"""``{brand}`` in Friends and Family Message texts = the default Company (no Settings field)."""
		from unittest.mock import patch

		from frappe.utils import cstr

		from posnext_promotions.promotions import friends_and_family_messages as ffm

		company = cstr(
			frappe.defaults.get_global_default("company")
			or frappe.db.get_single_value("Global Defaults", "default_company")
		)
		self.assertEqual(ffm._default_brand(), company)
		records = {ffm.EVENT_FF_ACTIVATED: {"ar": "", "en": "Enjoy shopping at {brand}!"}}
		with patch.object(ffm, "_load_records", return_value=records):
			msg = ffm.get_customer_message(ffm.EVENT_FF_ACTIVATED)
			self.assertEqual(msg["en"], f"Enjoy shopping at {company}!")
			self.assertEqual(ffm.get_customer_message(ffm.EVENT_FF_ACTIVATED, brand="X")["en"], "Enjoy shopping at X!")
