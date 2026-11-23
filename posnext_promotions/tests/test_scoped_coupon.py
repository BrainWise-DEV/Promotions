# Copyright (c) 2026, BrainWise and contributors

import unittest
from contextlib import nullcontext
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
		"applicable_item_group": None, "promotion_conflict": ""}
	d.update(kw)
	return SimpleNamespace(get=lambda k, default=None, d=d: d.get(k, default), **d)


def _doc(rows, coupon_code="ANAS"):
	return frappe._dict(items=rows, coupon_code=coupon_code, get=lambda k, d=None: {"items": rows, "coupon_code": coupon_code}.get(k, d))


def _priced(price=100, qty=1, rate=90, **kw):
	"""A row as ERPNext leaves it on validate: discount_amount per unit, rate after all discounts."""
	unit = price - rate
	d = {"price_list_rate": price, "qty": qty, "rate": rate, "discount_amount": unit,
		"discount_percentage": round(unit * 100.0 / price, 6)}
	d.update(kw)
	return _row(**d)


class TestScopedCoupon(unittest.TestCase):
	def _run(self, rows, coupon, coupon_code="ANAS", promotion=None):
		"""``promotion``: the per-unit promotion the server finds under each "Add on top" line
		(``coupon_share._promotions_under``, never a client field). None leaves it unpatched."""
		db = MagicMock()
		db.get_value.return_value = "anas"
		def throw(msg, *a, **k):
			raise frappe.ValidationError(msg)

		with (
			patch(MOD + ".db", new=db),
			patch(MOD + ".get_cached_doc", return_value=coupon),
			patch(MOD + ".throw", new=throw),
			patch("posnext_promotions.overrides.coupon_split.check_coupon_split"),  # real data: test_coupon_line_level_fnf
			patch("posnext_promotions.overrides.scoped_coupon._", new=lambda s: s),
			patch(
				"posnext_promotions.overrides.coupon_share._promotions_under",
				side_effect=lambda doc, rows: {id(r): promotion for r in rows},
			) if promotion is not None else nullcontext(),
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
		# Priced line: the coupon's own share (50 of 100) is above its 10%
		with self.assertRaises(frappe.ValidationError):
			self._run([_priced(rate=50)], _coupon(promotion_conflict="Add on top"), promotion=0)

	def test_tag_must_match_invoice_coupon(self):
		with self.assertRaises(frappe.ValidationError):
			self._run([_row()], _coupon(), coupon_code="OTHER")

	def test_amount_coupon_total_is_capped(self):
		coupon = _coupon(discount_type="Amount", discount_amount=20)
		self._run([_row(discount_percentage=0, discount_amount=20)], coupon)
		with self.assertRaises(frappe.ValidationError):
			self._run([_row(discount_percentage=0, discount_amount=30)], coupon)

	# "Add on top": the coupon takes its % of what the promotion left (coupon_engine.apply_coupon_to_items).
	# The promotion under the line is the server's (``promotion``), never a client field.
	def test_add_on_top_promo_plus_coupon_passes(self):
		# 10% promo + 10% coupon on the remaining 90 = 19% line discount, coupon share 9 per unit
		self._run([_priced(qty=2, rate=81)], _coupon(promotion_conflict="Add on top"), promotion=10)

	def test_add_on_top_inflated_coupon_share_is_rejected(self):
		# 10% promo, then the coupon share pushed to 15% of the remaining 90 (13.5)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run([_priced(rate=76.5)], _coupon(promotion_conflict="Add on top"), promotion=10)
		self.assertIn("15.0%", str(ctx.exception))

	def test_no_promo_line_at_coupon_percentage_passes(self):
		for mode in ("", "Add on top", "Better of the two"):
			with self.subTest(mode=mode):
				self._run([_priced(rate=90)], _coupon(promotion_conflict=mode), promotion=0)

	def test_no_promo_line_above_coupon_percentage_is_rejected(self):
		for mode in ("", "Add on top", "Better of the two"):
			with self.subTest(mode=mode), self.assertRaises(frappe.ValidationError):
				self._run([_priced(rate=85)], _coupon(promotion_conflict=mode), promotion=0)

	def test_better_of_the_two_passes(self):
		coupon = _coupon(promotion_conflict="Better of the two")
		# The coupon (10) beat a 5% promotion and replaced it: the whole 10 is the coupon's
		coupon_line = _priced(rate=90)
		# The promotion (20%) was bigger, so this line kept it and carries no coupon tag
		promo_line = _priced(idx=2, item_code="A", rate=80, posnext_coupon_code=None)
		self._run([coupon_line, promo_line], coupon)

	def test_better_of_the_two_never_stacks(self):
		# Only "Add on top" subtracts a promotion: the whole 19% is the coupon's, even if one is found
		with self.assertRaises(frappe.ValidationError):
			self._run([_priced(rate=81)], _coupon(promotion_conflict="Better of the two"), promotion=10)

	def test_skip_the_line_never_stacks(self):
		with self.assertRaises(frappe.ValidationError):
			self._run([_priced(rate=81)], _coupon(promotion_conflict="Skip the line"), promotion=10)

	def test_add_on_top_without_a_server_promotion_checks_the_whole_line(self):
		# A Pricing Rule named on the row proves nothing: with no promotion found, all 19% is the coupon's
		coupon = _coupon(promotion_conflict="Add on top")
		with self.assertRaises(frappe.ValidationError):
			self._run([_priced(rate=81, pricing_rules="PRLE-1")], coupon, promotion=0)

	def test_forged_client_coupon_discount_is_ignored(self):
		# PR #26's posnext_coupon_discount is stored, never trusted: a client claiming a 9 share
		# on a 19% line with no promotion under it is still refused, for % and for the cap.
		with self.assertRaises(frappe.ValidationError):
			self._run([_priced(rate=81, posnext_coupon_discount=9)], _coupon(promotion_conflict="Add on top"), promotion=0)
		coupon = _coupon(max_amount=10, promotion_conflict="Add on top")
		with self.assertRaises(frappe.ValidationError):
			self._run([_priced(qty=2, rate=90, posnext_coupon_discount=1)], coupon, promotion=0)
		# And a client claiming a bigger share cannot refuse a valid stacked line either.
		self._run([_priced(rate=81, posnext_coupon_discount=81)], _coupon(promotion_conflict="Add on top"), promotion=10)

	def test_add_on_top_amount_coupon_caps_only_the_coupon_share(self):
		coupon = _coupon(discount_type="Amount", discount_percentage=0, discount_amount=20,
			promotion_conflict="Add on top")
		# 10 promo + 20 coupon on the line: only the 20 counts against the coupon
		self._run([_priced(rate=70)], coupon, promotion=10)
		with self.assertRaises(frappe.ValidationError):
			self._run([_priced(rate=65)], coupon, promotion=10)

	def test_percentage_cap_counts_only_the_coupon_share(self):
		coupon = _coupon(max_amount=10, promotion_conflict="Add on top")
		self._run([_priced(qty=1, rate=81)], coupon, promotion=10)
		with self.assertRaises(frappe.ValidationError):
			self._run([_priced(qty=2, rate=81)], coupon, promotion=10)


class TestStackedCouponShare(unittest.TestCase):
	"""G5: on an "Add on top" line only the coupon's share counts, against its % and its cap."""

	_run = TestScopedCoupon._run

	def _stacked(self, promotion):
		row = _row(discount_percentage=28, discount_amount=14, qty=2, price_list_rate=50)
		coupon = _coupon(apply_scope="All Eligible Items", max_amount=10, promotion_conflict="Add on top")
		with patch(
			"posnext_promotions.overrides.coupon_share._promotions_under",
			side_effect=lambda doc, rows: {id(r): promotion for r in rows},
		):
			self._run([row], coupon)

	def test_share_within_the_coupon_passes(self):
		# 50 - 10 promo = 40; the coupon's 4 a unit is 10% of that, 8 in all (cap 10).
		self._stacked(10)

	def test_whole_line_counts_without_a_promotion(self):
		with self.assertRaises(frappe.ValidationError):
			self._stacked(0)


class TestOfflineSyncKeepsTheSale(unittest.TestCase):
	"""G5 review I-1: an offline sync (server flag) is never refused; each failed check is logged."""

	def _doc(self, offline):
		row = _row(discount_percentage=0, discount_amount=14, qty=2, price_list_rate=50)
		doc = _doc([row])
		doc.flags = frappe._dict(pos_offline_sync=1) if offline else frappe._dict()
		return doc

	def _check(self, doc):
		coupon = _coupon(apply_scope="All Eligible Items", max_amount=10, promotion_conflict="Add on top")
		db = MagicMock()
		db.get_value.return_value = "anas"

		def throw(msg, *a, **k):
			raise frappe.ValidationError(msg)

		with (
			patch(MOD + ".db", new=db),
			patch(MOD + ".get_cached_doc", return_value=coupon),
			patch(MOD + ".throw", new=throw),
			patch("posnext_promotions.overrides.coupon_split.check_coupon_split"),  # real data: test_coupon_line_level_fnf
			patch(MOD + ".log_error") as log_error,
			patch("posnext_promotions.overrides.scoped_coupon._", new=lambda s: s),
			patch(
				"posnext_promotions.overrides.coupon_share._promotions_under",
				side_effect=lambda doc, rows: {id(r): 0.0 for r in rows},
			),
		):
			validate_scoped_coupon_lines(doc)
		return log_error

	def test_online_refuses(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(self._doc(offline=False))

	def test_offline_sync_logs_and_keeps(self):
		log_error = self._check(self._doc(offline=True))
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs["title"], "POS Coupon check kept on offline sync")
		message = log_error.call_args.kwargs["message"]
		for text in ("ANAS", "28.0", '"limit": 10', '"share": 14', '"promotion": 0.0', '"row": 1'):
			self.assertIn(text, message)

	def test_client_offline_id_alone_is_not_offline(self):
		doc = self._doc(offline=False)
		doc["offline_id"] = "client-says-offline"
		with self.assertRaises(frappe.ValidationError):
			self._check(doc)


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
