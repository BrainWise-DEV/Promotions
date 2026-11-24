# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

from __future__ import annotations

import ast
import pathlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.api import friends_and_family, offers, promotions

GUARD_NAMES = {"ensure_desk_user", "ensure_pos_user", "ensure_pos_supervisor"}


class TestPromotionsSecurityGuards(FrappeTestCase):
	def test_friends_and_family_enforces_desk_and_profile(self):
		with patch(
			"posnext_promotions.api.friends_and_family.ensure_desk_user",
			side_effect=frappe.PermissionError("Desk user access required"),
		):
			with self.assertRaises(frappe.PermissionError):
				friends_and_family.get_friends_and_family_program()
			with self.assertRaises(frappe.PermissionError):
				friends_and_family.check_friends_and_family_eligibility("CUST-1")
			with self.assertRaises(frappe.PermissionError):
				friends_and_family.apply_friends_and_family_discount("CUST-1", [])

		with (
			patch("posnext_promotions.api.friends_and_family.ensure_desk_user", return_value="cashier@example.com"),
			patch(
				"posnext_promotions.api.friends_and_family.ensure_pos_user",
				side_effect=frappe.PermissionError("Denied"),
			),
		):
			with self.assertRaises(frappe.PermissionError):
				friends_and_family.get_friends_and_family_program(pos_profile="Other POS")
			with self.assertRaises(frappe.PermissionError):
				friends_and_family.check_friends_and_family_eligibility("CUST-1", pos_profile="Other POS")
			with self.assertRaises(frappe.PermissionError):
				friends_and_family.apply_friends_and_family_discount("CUST-1", [], pos_profile="Other POS")

	def test_offers_and_coupons_enforce_profile_and_customer_permission(self):
		with patch(
			"posnext_promotions.api.offers.ensure_pos_user",
			side_effect=frappe.PermissionError("Denied"),
		):
			with self.assertRaises(frappe.PermissionError):
				offers.get_offers("Other POS")

		with (
			patch("posnext_promotions.api.offers.ensure_desk_user", return_value="cashier@example.com"),
			patch("posnext_promotions.api.offers.frappe.db.table_exists", return_value=True),
			patch(
				"posnext_promotions.api.offers.frappe.has_permission",
				side_effect=frappe.PermissionError("No Customer read"),
			),
		):
			with self.assertRaises(frappe.PermissionError):
				offers.get_customer_one_time_redemptions("CUST-1")
			with self.assertRaises(frappe.PermissionError):
				offers.get_active_coupons("CUST-1", "Test Co")
			with self.assertRaises(frappe.PermissionError):
				offers.validate_coupon("SAVE10", customer="CUST-1", company="Test Co")
			with self.assertRaises(frappe.PermissionError):
				offers.calculate_coupon_discount("SAVE10", {"customer": "CUST-1", "company": "Test Co", "items": []})

	def test_promotions_and_referrals_enforce_profile_and_permissions(self):
		with (
			patch("posnext_promotions.api.promotions.ensure_desk_user", return_value="cashier@example.com"),
			patch(
				"posnext_promotions.api.promotions.ensure_pos_user",
				side_effect=frappe.PermissionError("Denied"),
			),
		):
			with self.assertRaises(frappe.PermissionError):
				promotions.get_promotions(pos_profile="Other POS")
			with self.assertRaises(frappe.PermissionError):
				promotions.search_items("shirt", pos_profile="Other POS")

		with (
			patch("posnext_promotions.api.promotions.ensure_desk_user", return_value="cashier@example.com"),
			patch(
				"posnext_promotions.api.promotions.frappe.has_permission",
				side_effect=frappe.PermissionError("No Customer write"),
			),
		):
			with self.assertRaises(frappe.PermissionError):
				promotions.apply_referral_code("REF-1", "CUST-1")

		with (
			patch("posnext_promotions.api.promotions.ensure_desk_user", return_value="cashier@example.com"),
			patch(
				"posnext_promotions.api.promotions.check_promotion_permissions",
				side_effect=frappe.PermissionError("Denied"),
			),
		):
			with self.assertRaises(frappe.PermissionError):
				promotions.get_referral_codes()

	def test_all_whitelisted_endpoints_in_posnext_promotions_have_guards(self):
		app_root = pathlib.Path(frappe.get_app_path("posnext_promotions"))
		missing = []

		for path in sorted(app_root.rglob("*.py")):
			if "test_" in path.name or "__pycache__" in str(path) or path.name == "security.py":
				continue
			src = path.read_text()
			if "whitelist" not in src:
				continue
			tree = ast.parse(src)
			for node in ast.walk(tree):
				if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
					continue
				is_whitelisted = any(
					(isinstance(d, ast.Call) and ast.unparse(d.func).endswith("whitelist"))
					or (isinstance(d, ast.Attribute) and d.attr == "whitelist")
					for d in node.decorator_list
				)
				if not is_whitelisted:
					continue
				body = node.body
				idx = (
					1
					if (
						body
						and isinstance(body[0], ast.Expr)
						and isinstance(body[0].value, ast.Constant)
						and isinstance(body[0].value.value, str)
					)
					else 0
				)
				found = False
				for stmt in body[idx : idx + 2]:
					if isinstance(stmt, ast.Try):
						break
					if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
						fn = stmt.value.func
						name = (
							fn.id
							if isinstance(fn, ast.Name)
							else (fn.attr if isinstance(fn, ast.Attribute) else None)
						)
						if name in GUARD_NAMES:
							found = True
							break
				if not found:
					missing.append(f"{path.relative_to(app_root)}:{node.lineno} {node.name}")

		self.assertEqual(missing, [], f"Unguarded posnext_promotions endpoints: {missing}")
