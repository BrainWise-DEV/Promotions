# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""F&F money follows the invoice currency: precision, rounding and formatting."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import fmt_money

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_money
from posnext_promotions.promotions.friends_and_family_apply import _capped_line_values
from posnext_promotions.promotions.friends_and_family_caps import (
	CAP_MONTHLY,
	CAP_ORDER,
	CapContext,
	allocate,
	build_cap_payload,
)

# Generic test currencies, created per test and rolled back.
TWO = "_T2D"
THREE = "_T3D"
ZERO = "_T0D"
_FORMATS = {TWO: ("#,###.##", 100), THREE: ("#,###.###", 1000), ZERO: ("#,###", 1)}


def _reset_settings_cache():
	frappe.clear_document_cache("System Settings", "System Settings")
	frappe.local.system_settings = None
	frappe.cache.hdel("defaults", "__default")
	ff_money.clear_cache()


def _set_system(**values):
	"""Like saving System Settings: the field, plus the global default ERPNext reads."""
	for key, value in values.items():
		frappe.db.set_single_value("System Settings", key, value)
		frappe.db.set_default(key, value)
	_reset_settings_cache()


class TestFFMoney(FrappeTestCase):
	def setUp(self):
		frappe.db.savepoint("ff_money_case")
		for name, (number_format, fraction_units) in _FORMATS.items():
			if not frappe.db.exists("Currency", name):
				frappe.get_doc(
					{
						"doctype": "Currency",
						"currency_name": name,
						"enabled": 1,
						"number_format": number_format,
						"fraction_units": fraction_units,
					}
				).insert()
		# A currency's own format counts only with this setting on and no global precision.
		_set_system(use_number_format_from_currency=1, currency_precision="")

	def tearDown(self):
		frappe.db.rollback(save_point="ff_money_case")
		_reset_settings_cache()

	def test_precision_comes_from_the_currency(self):
		self.assertEqual(ff_money.precision_for(TWO), 2)
		self.assertEqual(ff_money.precision_for(THREE), 3)
		self.assertEqual(ff_money.precision_for(ZERO), 0)

	def test_two_decimal_currency(self):
		_set_system(rounding_method="Commercial Rounding")
		self.assertEqual(ff_money.money(1.005, TWO), 1.01)
		self.assertEqual(ff_money.floor_money(1.239, TWO), 1.23)
		self.assertEqual(ff_money.ceil_money(1.231, TWO), 1.24)

	def test_three_decimal_currency(self):
		self.assertEqual(ff_money.floor_money(1.2349, THREE), 1.234)
		self.assertEqual(ff_money.ceil_money(1.2341, THREE), 1.235)
		self.assertEqual(ff_money.money(1.2346, THREE), 1.235)

	def test_zero_decimal_currency(self):
		self.assertEqual(ff_money.money(1234.6, ZERO), 1235)
		self.assertEqual(ff_money.floor_money(1234.6, ZERO), 1234)
		self.assertEqual(ff_money.ceil_money(1234.2, ZERO), 1235)

	def test_global_currency_precision_wins(self):
		# ERPNext rounds invoices to the System Settings precision when one is set.
		_set_system(currency_precision=4)
		self.assertEqual(ff_money.precision_for(TWO), 4)
		self.assertEqual(ff_money.precision_for(ZERO), 4)

	def test_engine_rounds_in_the_current_currency(self):
		with ff_money.currency_context(THREE):
			self.assertEqual(bd.money(1.2346), 1.235)
			with ff_money.currency_context(ZERO):
				self.assertEqual(bd.money(1234.6), 1235)
			self.assertEqual(ff_money.current_currency(), THREE)
			# 10 over 3 lines of 3.333...: floored to the currency's unit, never above the allowance.
			alloc = allocate([(0, 10, 0), (1, 10, 0), (2, 10, 0)], 10)
			self.assertEqual(alloc, {0: 3.333, 1: 3.333, 2: 3.333})
		self.assertIsNone(ff_money.current_currency())

	def test_no_currency_means_the_context_currency(self):
		with ff_money.currency_context(THREE):
			self.assertEqual(ff_money.money(1.2346), 1.235)
			self.assertEqual(ff_money.floor_money(1.2349), 1.234)
			self.assertEqual(ff_money.ceil_money(1.2341), 1.235)
			self.assertEqual(ff_money.fmt(1), fmt_money(1, precision=3, currency=THREE))
			self.assertEqual(ff_money.money(1234.6, ZERO), 1235)  # an explicit currency still wins

	def test_unresolved_currency_is_logged_once_per_request(self):
		if hasattr(frappe.local, ff_money._FALLBACK_LOGGED):
			delattr(frappe.local, ff_money._FALLBACK_LOGGED)
		before = frappe.db.count("Error Log", {"method": ff_money.FALLBACK_LOG_TITLE})
		with ff_money.currency_context(THREE):
			ff_money.money(1)
		self.assertEqual(frappe.db.count("Error Log", {"method": ff_money.FALLBACK_LOG_TITLE}), before)
		self.assertEqual(ff_money.precision_for(), ff_money.precision_for(ff_money.default_currency()))
		ff_money.money(1)
		self.assertEqual(frappe.db.count("Error Log", {"method": ff_money.FALLBACK_LOG_TITLE}), before + 1)

	def test_capped_invoice_row_rounds_rate_up_in_currency(self):
		with ff_money.currency_context(THREE):
			actual, rate, _pct, stored = _capped_line_values(10.0, 3, 1.0, bd.AMOUNT_BASIS_UNIT)
		self.assertEqual(rate, 9.667)
		self.assertEqual(stored, 0.333)
		self.assertEqual(actual, 0.999)
		self.assertLessEqual(actual, 1.0)

	def test_entry_points_set_the_currency(self):
		@ff_money.in_cart_currency
		def cart(items=None, pos_profile=None, company=None):
			return ff_money.current_currency()

		@ff_money.in_cart_currency
		def whitelisted(invoice_data, selected_offers=None):
			return ff_money.current_currency()

		company = frappe.db.get_value("Company", {}, "name")
		self.assertEqual(cart(company=company), ff_money.company_currency(company))
		with ff_money.currency_context(THREE):  # an invoice already chose
			self.assertEqual(cart(company=company), THREE)
			# ...but a cart that names its currency prices in it.
			self.assertEqual(whitelisted(json.dumps({"currency": ZERO, "company": company})), ZERO)
		# The POS payload: its own currency first, else its company's (a JSON string or a dict).
		self.assertEqual(whitelisted(json.dumps({"currency": ZERO, "company": company})), ZERO)
		self.assertEqual(whitelisted({"company": company}), ff_money.company_currency(company))
		profile = frappe.db.get_value("POS Profile", {}, ["name", "currency", "company"], as_dict=True)
		if profile:
			expected = profile.currency or ff_money.company_currency(profile.company)
			self.assertEqual(whitelisted({"pos_profile": profile.name}), expected)

		@ff_money.in_document_currency
		def hook(doc, method=None):
			return ff_money.current_currency()

		self.assertEqual(hook(frappe._dict(currency=ZERO, company=company)), ZERO)
		self.assertEqual(hook(frappe._dict(company=company)), ff_money.company_currency(company))
		self.assertIsNone(ff_money.current_currency())

	def test_messages_use_currency_format(self):
		ctx = CapContext(per_order_cap=10, currency=THREE, company_currency=THREE)
		payload = build_cap_payload(ctx, 12.3456, 10)
		self.assertEqual(payload["cap_hit"], CAP_ORDER)
		expected = fmt_money(10, precision=3, currency=THREE)
		self.assertIn(expected, payload["message_en"])
		self.assertIn("10.000", expected)
		self.assertIn(fmt_money(12.346, precision=3, currency=THREE), payload["message_en"])
		self.assertIn(expected, payload["message_ar"])
		self.assertEqual(payload["fnf_uncapped_total"], 12.346)

	def test_cap_message_shows_each_amount_in_its_currency(self):
		# Caps are company currency; the reduced F&F is invoice currency.
		ctx = CapContext(monthly_cap=100, monthly_used=40, conversion_rate=0.5, currency=ZERO, company_currency=THREE)
		self.assertEqual(ctx.allowance, 120)  # 60 left / 0.5, floored to whole units
		payload = build_cap_payload(ctx, 300, 120)
		self.assertEqual(payload["cap_hit"], CAP_MONTHLY)
		self.assertIn(ff_money.fmt(120, ZERO), payload["message_en"])
		self.assertIn(ff_money.fmt(100, THREE), payload["message_en"])
		self.assertIn(ff_money.fmt(60, THREE), payload["message_en"])

	def test_line_message_uses_currency_format(self):
		fnf = bd.Candidate(source=bd.SOURCE_FRIENDS_AND_FAMILY, amount=1.2346)
		other = bd.Candidate(source=bd.SOURCE_MANUAL, amount=1)
		with ff_money.currency_context(THREE):
			messages = bd.decide([fnf, other]).messages()
		self.assertIn(fmt_money(1.235, precision=3, currency=THREE), messages["en"])
		self.assertIn(fmt_money(1, precision=3, currency=THREE), messages["en"])

	def test_existing_two_decimal_behaviour_unchanged(self):
		# The caps allocation fixtures of test_friends_and_family_caps, on a 2-decimal currency.
		with ff_money.currency_context(TWO):
			lines = [(0, 33.33, 0), (1, 33.33, 0), (2, 33.34, 0)]
			self.assertEqual(allocate(lines, 10), {0: 3.33, 1: 3.33, 2: 3.33})
			self.assertEqual(allocate(lines, 0), {})
			self.assertEqual(allocate(lines, 1000), {0: 33.33, 1: 33.33, 2: 33.34})
			self.assertEqual(allocate([(0, 30, 0), (1, 60, 0)], 50), {0: 16.66, 1: 33.33})
			ctx = CapContext(per_order_cap=50, monthly_cap=100, monthly_used=80)
			self.assertEqual(ctx.allowance, 20)
			payload = build_cap_payload(ctx, 90, 49.99)
			self.assertEqual(payload["fnf_applied_total"], 49.99)
			self.assertEqual(payload["monthly_remaining_after"], 0)
			self.assertIn(fmt_money(20, precision=2, currency=TWO), payload["message_en"])
			self.assertEqual(CapContext(per_order_cap=50, conversion_rate=3).allowance, 16.66)
