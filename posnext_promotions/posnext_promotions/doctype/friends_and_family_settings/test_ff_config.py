# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.promotions import ff_config, ff_constants
from posnext_promotions.promotions import friends_and_family_gifts as gifts
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

	# ---- J1: "Allow Free Gift with F&F" takes effect at once, and every reader agrees ----

	def _plant_stale_effective_config(self, allow: bool):
		"""What a request that ran between the Settings save and its commit left in Redis.

		on_update cleared the (since retired) ``ff:effective_config``; a POS request then read the old,
		still-committed row and cached it with no expiry (seen on pos-dev with two
		processes: the saved box stayed off for the BXGY judge, on for the gift policy).
		"""
		cfg = ff_config._build()
		stale = ff_config.EffectiveConfig(
			allow_free_gift_with_ff=Value(allow, "global"),
			on_cap_breach_at_submit=cfg.on_cap_breach_at_submit,
			unclassified_promotion_policy=cfg.unclassified_promotion_policy,
		)
		# Removed whatever the test does (the key is no longer read, but leave no trace).
		self.addCleanup(frappe.cache().delete_value, "ff:effective_config")
		frappe.cache().set_value("ff:effective_config", stale)

	def test_checkbox_read_from_the_saved_settings_not_a_stale_entry(self):
		with ff_settings_guard():
			for saved in (1, 0, 1, 0):
				_save(allow_free_gift_with_ff=saved)
				self._plant_stale_effective_config(not saved)
				with self.subTest(saved=saved):
					self.assertIs(ff_config.resolve().allow_free_gift_with_ff.value, bool(saved))

	def test_every_reader_of_the_checkbox_agrees(self):
		bxgy = frappe._dict(price_or_product_discount="Product", same_item=1, apply_on="Item Code", min_qty=2, free_qty=1)
		with ff_settings_guard():
			for saved in (0, 1, 0):
				_save(allow_free_gift_with_ff=saved)
				self._plant_stale_effective_config(not saved)
				with self.subTest(saved=saved):
					self.assertIs(gifts.allow_free_gift_with_ff(), bool(saved), "gift policy")
					self.assertIs(ff_config.resolve().allow_free_gift_with_ff.value, bool(saved), "ff_config")
					self.assertEqual(
						gifts.scheme_blocking_kind(bxgy), None if saved else gifts.SCHEME_KIND_BXGY, "BXGY judge"
					)

	def test_checkbox_changed_without_on_update_is_seen(self):
		"""db.set_single_value (a patch, a data fix, a restore) skips on_update: no stale value either."""
		with ff_settings_guard():
			_save(allow_free_gift_with_ff=0)
			self.assertIs(ff_config.resolve().allow_free_gift_with_ff.value, False)
			frappe.db.set_single_value(FF_SETTINGS, "allow_free_gift_with_ff", 1)
			self.assertIs(ff_config.resolve().allow_free_gift_with_ff.value, True)
			self.assertIs(gifts.allow_free_gift_with_ff(), True)
