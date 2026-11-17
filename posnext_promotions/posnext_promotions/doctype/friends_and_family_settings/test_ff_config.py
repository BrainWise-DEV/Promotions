# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.promotions import ff_config, ff_constants
from posnext_promotions.promotions.ff_config import Value
from posnext_promotions.promotions.friends_and_family_testing import FF_SETTINGS, ff_settings_guard


def _save(**values):
	doc = frappe.get_doc(FF_SETTINGS)
	doc.update(values)
	doc.flags.ignore_validate = True
	doc.save(ignore_permissions=True)


class TestFFConfig(FrappeTestCase):
	def test_defaults_when_unset(self):
		with ff_settings_guard():
			_save(on_cap_breach_at_submit=None, unclassified_promotion_policy=None)
			cfg = ff_config.resolve()
			self.assertEqual(cfg.on_cap_breach_at_submit, Value("Allow and log", "default"))
			self.assertEqual(cfg.unclassified_promotion_policy.value, "Compete")

	def test_global_value_wins_over_default(self):
		with ff_settings_guard():
			_save(on_cap_breach_at_submit="Block submit")
			cfg = ff_config.resolve()
			self.assertEqual(cfg.on_cap_breach_at_submit, Value("Block submit", "global"))

	def test_existing_allow_free_gift_preserved(self):
		with ff_settings_guard():
			_save(allow_free_gift_with_ff=1)
			self.assertIs(ff_config.resolve().allow_free_gift_with_ff.value, True)
			_save(allow_free_gift_with_ff=0)
			self.assertIs(ff_config.resolve().allow_free_gift_with_ff.value, False)

	def test_doctype_options_match_constants(self):
		meta = frappe.get_meta(FF_SETTINGS)
		for fieldname, expected in (
			("on_cap_breach_at_submit", ff_constants.CAP_BREACH_POLICIES),
			("unclassified_promotion_policy", ff_constants.UNCLASSIFIED_POLICIES),
		):
			self.assertEqual(tuple(meta.get_field(fieldname).options.split("\n")), expected)

	def test_cache_cleared_on_save(self):
		with ff_settings_guard():
			_save(on_cap_breach_at_submit="Allow and log")
			self.assertEqual(ff_config.resolve().on_cap_breach_at_submit.value, "Allow and log")
			_save(on_cap_breach_at_submit="Block submit")
			self.assertEqual(ff_config.resolve().on_cap_breach_at_submit.value, "Block submit")
