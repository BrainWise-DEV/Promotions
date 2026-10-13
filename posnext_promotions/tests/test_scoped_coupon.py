# Copyright (c) 2026, BrainWise and contributors

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from posnext_promotions.overrides.scoped_coupon import validate_scoped_coupon_lines

MOD = "posnext_promotions.overrides.scoped_coupon.frappe"


def _row(**kw):
	d = {"idx": 1, "item_code": "A", "qty": 1, "posnext_coupon_code": "ANAS", "is_free_item": 0,
		"discount_percentage": 10, "discount_amount": 0, "brand": "Nike", "item_group": "Shoes"}
	d.update(kw)
	return frappe._dict(d)


def _coupon(**kw):
	d = {"disabled": 0, "apply_scope": "Item Code", "discount_type": "Percentage", "discount_percentage": 10,
		"discount_amount": 0, "max_amount": 0, "applicable_items": [{"item_code": "A"}],
		"applicable_item_groups": [], "applicable_brands": [], "applicable_brand": None,
		"applicable_item_group": None}
	d.update(kw)
	return SimpleNamespace(get=lambda k, default=None, d=d: d.get(k, default), **d)


def _doc(rows, coupon_code="ANAS"):
	return frappe._dict(items=rows, coupon_code=coupon_code, get=lambda k, d=None: {"items": rows, "coupon_code": coupon_code}.get(k, d))


class TestScopedCoupon(unittest.TestCase):
	def _run(self, rows, coupon, coupon_code="ANAS"):
		db = MagicMock()
		db.get_value.return_value = "anas"
		def throw(msg, *a, **k):
			raise frappe.ValidationError(msg)

		with (
			patch(MOD + ".db", new=db),
			patch(MOD + ".get_cached_doc", return_value=coupon),
			patch(MOD + ".throw", new=throw),
			patch("posnext_promotions.overrides.scoped_coupon._", new=lambda s: s),
		):
			validate_scoped_coupon_lines(_doc(rows, coupon_code))

	def test_untagged_invoice_is_ignored(self):
		validate_scoped_coupon_lines(_doc([_row(posnext_coupon_code=None)]))

	def test_covered_line_passes(self):
		self._run([_row()], _coupon())

	def test_line_outside_scope_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._run([_row(item_code="B")], _coupon())

	def test_percentage_above_coupon_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._run([_row(discount_percentage=50)], _coupon())

	def test_tag_must_match_invoice_coupon(self):
		with self.assertRaises(frappe.ValidationError):
			self._run([_row()], _coupon(), coupon_code="OTHER")

	def test_amount_coupon_total_is_capped(self):
		coupon = _coupon(discount_type="Amount", discount_amount=20)
		self._run([_row(discount_percentage=0, discount_amount=20)], coupon)
		with self.assertRaises(frappe.ValidationError):
			self._run([_row(discount_percentage=0, discount_amount=30)], coupon)


class TestCouponScopeValidation(unittest.TestCase):
	"""POS Coupon save refuses a restricted scope with nothing selected."""

	def _doc(self, **kw):
		d = {"apply_scope": "Item Code", "applicable_items": [], "applicable_item_groups": [],
			"applicable_brands": [], "applicable_brand": None, "applicable_item_group": None}
		d.update(kw)
		return frappe._dict(d)

	def _check(self, doc):
		from posnext_promotions.overrides.scoped_coupon import validate_coupon_scope

		def throw(msg, *a, **k):
			raise frappe.ValidationError(msg)

		with patch("posnext_promotions.overrides.scoped_coupon.frappe.throw", side_effect=throw), patch(
			"posnext_promotions.overrides.scoped_coupon.frappe.bold", side_effect=lambda v: v
		):
			validate_coupon_scope(doc)

	def test_empty_scope_tables_rejected(self):
		for scope in ("Item Code", "Item Group", "Brand"):
			with self.subTest(scope=scope), self.assertRaises(frappe.ValidationError) as ctx:
				self._check(self._doc(apply_scope=scope))
			self.assertIn(scope, str(ctx.exception))

	def test_blank_rows_count_as_empty(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(self._doc(applicable_items=[frappe._dict(item_code=None)]))

	def test_filled_scope_accepted(self):
		self._check(self._doc(applicable_items=[frappe._dict(item_code="A")]))
		self._check(self._doc(apply_scope="Item Group", applicable_item_groups=[frappe._dict(item_group="G")]))
		self._check(self._doc(apply_scope="Brand", applicable_brands=[frappe._dict(brand="B")]))

	def test_legacy_single_field_accepted(self):
		self._check(self._doc(apply_scope="Brand", applicable_brand="Nike"))
		self._check(self._doc(apply_scope="Item Group", applicable_item_group="Shoes"))

	def test_unscoped_coupon_unaffected(self):
		self._check(self._doc(apply_scope="All Eligible Items"))
		self._check(self._doc(apply_scope=None))
