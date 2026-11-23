# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""The replaced-discount snapshot is signed by the server (ff_snapshot).

``posnext_fnf_replaced_discount`` is a row field the client can write. Only a
snapshot signed for this line's item, qty, list price and program, not
expired, and whose Pricing Rules are still enabled and valid on the posting
date is put back on the line.
"""

from __future__ import annotations

import contextlib
import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.promotions import ff_snapshot
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY as FNF,
	apply_friends_and_family_on_invoice,
	apply_friends_and_family_to_items,
	restore_context,
)
from posnext_promotions.promotions.friends_and_family_caps import FIELD_FNF_REPLACED
from posnext_promotions.promotions.friends_and_family_eligibility import FLAG_FIELD, ensure_customer_flag_field
from posnext_promotions.test_promotions import _ctx
from posnext_promotions.promotions.friends_and_family_testing import (
	approved_domain_rows,
	ensure_named_pricing_rule,
	ff_settings_guard,
	give_verified_email,
)

DATE = "2026-10-05"
CUSTOMER = "_Test FF Snapshot Customer"
ITEM = "_Test FF Snapshot Item"
RULE = "_Test FF Snapshot Rule 40"
RULE_2 = "_Test FF Snapshot Rule 40 B"
OTHER_CUSTOMER = "_Test FF Snapshot Other Customer"


def _promo_data(rules=RULE, pct=40):
	return {
		"discount_source": "item_level_promotion",
		"discount_percentage": pct,
		"discount_amount": pct,
		"rate": 100 - pct,
		"pricing_rules": rules,
		"posnext_coupon_code": None,
		"coupon_code": None,
		"is_already_discounted": 0,
	}


class TestFFSnapshot(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()
		group = frappe.db.get_value("Customer Group", {"is_group": 0})
		for name in (CUSTOMER, OTHER_CUSTOMER):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": group}).insert()
			frappe.db.set_value("Customer", name, {FLAG_FIELD: 0, "disabled": 0})
			give_verified_email(name)
		ctx = _ctx()
		cls.pos_profile, cls.company = ctx.pos_profile, ctx.company
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": ITEM,
					"item_name": ITEM,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}) or "Products",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()

	def setUp(self):
		self._stack = contextlib.ExitStack()
		self._stack.enter_context(ff_settings_guard())
		frappe.db.savepoint("ff_snapshot")
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
				"max_discount_per_order": 0,
				"max_discount_per_customer_month": 0,
			}
		)
		doc.save()
		# The test POS profile's company, so the profile tests compare like with like.
		ensure_named_pricing_rule(RULE, company=self.company)
		ensure_named_pricing_rule(RULE_2, company=self.company)

	def tearDown(self):
		frappe.db.rollback(save_point="ff_snapshot")
		for name in (RULE, RULE_2):
			frappe.clear_document_cache("Pricing Rule", name)
		frappe.clear_document_cache("POS Profile", self.pos_profile)
		self._stack.close()

	# ---- helpers -------------------------------------------------------------

	def _fnf_line(self, qty=1, **extra):
		"""A line F&F already won (30 %), as a saved row or a cart line carries it."""
		return frappe._dict(
			{
				"item_code": ITEM,
				"qty": qty,
				"price_list_rate": 100,
				"rate": 70,
				"discount_percentage": 30,
				"discount_amount": 30 * qty,
				"discount_source": FNF,
				"pricing_rules": "",
				**extra,
			}
		)

	def _cart(self, customer=CUSTOMER, pos_profile=None):
		return restore_context(customer, DATE, pos_profile)

	def _signed_line(self, data=None, customer=CUSTOMER, pos_profile=None, **extra):
		"""F&F line carrying a snapshot signed for ``customer`` / ``pos_profile``."""
		line = self._fnf_line(**extra)
		line[FIELD_FNF_REPLACED] = ff_snapshot.sign(line, data or _promo_data(), doc=self._cart(customer, pos_profile))
		return line

	def _apply(self, line, posting_date=DATE, customer=CUSTOMER, pos_profile=None):
		return apply_friends_and_family_to_items(
			[line], customer=customer, posting_date=posting_date, pos_profile=pos_profile
		)

	def _invoice(self, line, posting_date, posting_time=None, coupon_code=None):
		doc = frappe._dict(
			{
				"doctype": "POS Invoice",
				"customer": CUSTOMER,
				"posting_date": posting_date,
				"posting_time": posting_time,
				"coupon_code": coupon_code,
				"is_pos": 1,
				"items": [line],
			}
		)
		apply_friends_and_family_on_invoice(doc)
		return line

	# ---- signature -----------------------------------------------------------

	def test_signed_snapshot_restores(self):
		line = self._signed_line()
		payload = self._apply(line)
		# The 40 % promotion is back and beats F&F 30 %.
		self.assertFalse(payload["items"][0]["applies"])
		self.assertEqual(flt(line.discount_percentage), 40)
		self.assertEqual(line.pricing_rules, RULE)
		self.assertNotEqual(line.discount_source, FNF)

	def test_bad_signature_ignored(self):
		line = self._fnf_line()
		envelope = json.loads(ff_snapshot.sign(line, _promo_data(pct=90)))
		envelope["sig"] = "0" * 64
		line[FIELD_FNF_REPLACED] = json.dumps(envelope)
		self.assertIsNone(ff_snapshot.verify(line, line[FIELD_FNF_REPLACED]))
		self._apply(line)
		self.assertNotEqual(flt(line.discount_percentage), 90)
		self.assertEqual(flt(line.discount_percentage), 30)
		self.assertEqual(line.discount_source, FNF)

	def test_tamper_each_bound_field(self):
		line = self._fnf_line()
		raw = ff_snapshot.sign(line, _promo_data(), program="P1")
		self.assertIsNotNone(ff_snapshot.verify(line, raw, program="P1"))
		for key, value in (("item_code", "_Other Item"), ("qty", 2), ("price_list_rate", 1000)):
			with self.subTest(field=key):
				self.assertIsNone(ff_snapshot.verify(frappe._dict({**line, key: value}), raw, program="P1"))
		self.assertIsNone(ff_snapshot.verify(line, raw, program="P2"))
		self.assertIsNone(ff_snapshot.verify(line, raw))
		envelope = json.loads(raw)
		envelope["program"] = "P2"
		self.assertIsNone(ff_snapshot.verify(line, json.dumps(envelope), program="P2"))

	def test_canonical_order(self):
		line = self._fnf_line()
		data = _promo_data()
		reordered = dict(reversed(list(data.items())))
		with mock.patch.object(ff_snapshot.time, "time", return_value=1_900_000_000):
			first = json.loads(ff_snapshot.sign(line, data))
			second = json.loads(ff_snapshot.sign(line, reordered))
		self.assertEqual(first["sig"], second["sig"])

	def test_numbers_survive_db_round_trip(self):
		line = self._fnf_line()
		raw = ff_snapshot.sign(line, _promo_data())
		saved = frappe._dict({**line, "qty": 1.0, "price_list_rate": "100.000000000"})
		self.assertIsNotNone(ff_snapshot.verify(saved, raw))

	def test_key_error_returns_none(self):
		line = self._fnf_line()
		raw = ff_snapshot.sign(line, _promo_data())
		before = frappe.db.count("Error Log")
		with mock.patch.object(ff_snapshot, "get_encryption_key", side_effect=Exception("no key")):
			self.assertIsNone(ff_snapshot.verify(line, raw))
			self.assertIsNone(ff_snapshot.sign(line, _promo_data()))
		self.assertGreater(frappe.db.count("Error Log"), before)

	def test_tampered_qty_discards(self):
		line = self._signed_line(qty=1)
		raw = line[FIELD_FNF_REPLACED]
		line.qty = 5
		line.discount_amount = 150
		self.assertIsNone(ff_snapshot.verify(line, raw))
		self._apply(line)
		self.assertEqual(flt(line.discount_percentage), 30)
		self.assertEqual(line.discount_source, FNF)

	def test_legacy_unsigned_snapshot_ignored(self):
		line = self._fnf_line()
		line[FIELD_FNF_REPLACED] = json.dumps(_promo_data(pct=90))
		self.assertIsNone(ff_snapshot.verify(line, line[FIELD_FNF_REPLACED]))
		self._apply(line)
		self.assertEqual(flt(line.discount_percentage), 30)
		self.assertEqual(line.discount_source, FNF)
		self.assertFalse(line.pricing_rules)

	def test_expired_snapshot_ignored(self):
		line = self._fnf_line()
		line[FIELD_FNF_REPLACED] = ff_snapshot.sign(line, _promo_data(), ttl_seconds=-1)
		self.assertIsNone(ff_snapshot.verify(line, line[FIELD_FNF_REPLACED]))
		self._apply(line)
		self.assertEqual(flt(line.discount_percentage), 30)

	def test_new_snapshot_is_signed(self):
		line = frappe._dict(
			{
				"item_code": ITEM,
				"qty": 1,
				"price_list_rate": 100,
				"rate": 90,
				"discount_percentage": 10,
				"discount_amount": 10,
				"pricing_rules": RULE,
				"discount_source": "item_level_promotion",
			}
		)
		self._apply(line)
		self.assertEqual(line.discount_source, FNF)
		self.assertIsNone(ff_snapshot.verify(line, line[FIELD_FNF_REPLACED]))  # bound to the cart
		data = ff_snapshot.verify(line, line[FIELD_FNF_REPLACED], doc=self._cart())
		self.assertEqual(data["pricing_rules"], RULE)
		self.assertEqual(flt(data["discount_percentage"]), 10)

	# ---- rule gate ------------------------------------------------------------

	def test_snapshot_dropped_when_rule_disabled(self):
		line = self._signed_line()
		data = ff_snapshot.verify(line, line[FIELD_FNF_REPLACED], doc=self._cart())
		self.assertIsNotNone(data)
		self.assertTrue(ff_snapshot.rules_apply_to(data, frappe._dict(customer=CUSTOMER, posting_date=DATE)))
		frappe.db.set_value("Pricing Rule", RULE, "disable", 1)
		frappe.clear_document_cache("Pricing Rule", RULE)
		self.assertFalse(ff_snapshot.rules_apply_to(data, frappe._dict(customer=CUSTOMER, posting_date=DATE)))
		self._apply(line)
		self.assertNotEqual(flt(line.discount_percentage), 40)
		self.assertEqual(line.discount_source, FNF)
		self.assertFalse(line.pricing_rules)

	def test_snapshot_dropped_when_rule_expired_on_posting_date(self):
		frappe.db.set_value("Pricing Rule", RULE, "valid_upto", "2026-11-30")
		frappe.clear_document_cache("Pricing Rule", RULE)
		# Held on 30 Nov (rule still valid) …
		held = self._invoice(self._signed_line(), "2026-11-30")
		self.assertEqual(flt(held.discount_percentage), 40)
		# … but the invoice is posted 1 Dec: the promotion is gone.
		line = self._invoice(self._signed_line(), "2026-12-01")
		self.assertNotEqual(flt(line.discount_percentage), 40)
		self.assertEqual(flt(line.discount_percentage), 30)

	def test_snapshot_kept_when_all_rules_valid(self):
		line = self._signed_line(data=_promo_data(rules=f"{RULE},{RULE_2}"))
		self._apply(line)
		self.assertEqual(flt(line.discount_percentage), 40)
		self.assertEqual(line.pricing_rules, f"{RULE},{RULE_2}")

	def test_snapshot_dropped_when_one_rule_missing(self):
		line = self._signed_line(data=_promo_data(rules=f"{RULE},_Test FF Snapshot No Such Rule"))
		self._apply(line)
		self.assertEqual(flt(line.discount_percentage), 30)

	def test_manual_discount_snapshot_passes_gate(self):
		data = _promo_data(rules="")
		data["discount_source"] = None
		self.assertTrue(ff_snapshot.rules_apply_to(data, frappe._dict(customer=CUSTOMER, posting_date=DATE)))

	# ---- Ruling 24: full applicability --------------------------------------

	def test_snapshot_dropped_when_customer_changes(self):
		frappe.db.set_value("Pricing Rule", RULE, {"applicable_for": "Customer", "customer": CUSTOMER})
		frappe.clear_document_cache("Pricing Rule", RULE)
		kept = self._signed_line()
		self._apply(kept, customer=CUSTOMER)
		self.assertEqual(flt(kept.discount_percentage), 40)
		# Held cart resumed for another customer: the rule is not theirs (snapshot
		# signed for that customer, so only the applicability gate can drop it).
		line = self._signed_line(customer=OTHER_CUSTOMER)
		self._apply(line, customer=OTHER_CUSTOMER)
		self.assertNotEqual(flt(line.discount_percentage), 40)
		self.assertEqual(line.discount_source, FNF)
		self.assertFalse(line.pricing_rules)

	def test_snapshot_dropped_for_profile_of_other_company(self):
		frappe.db.set_value("Pricing Rule", RULE, "company", "_Test FF Snapshot No Such Company")
		frappe.clear_document_cache("Pricing Rule", RULE)
		line = self._signed_line(pos_profile=self.pos_profile)
		self._apply(line, pos_profile=self.pos_profile)
		self.assertNotEqual(flt(line.discount_percentage), 40)

	def test_snapshot_dropped_for_profile_ignoring_pricing_rules(self):
		kept = self._signed_line(pos_profile=self.pos_profile)
		self._apply(kept, pos_profile=self.pos_profile)
		self.assertEqual(flt(kept.discount_percentage), 40)
		frappe.db.set_value("POS Profile", self.pos_profile, "ignore_pricing_rule", 1)
		frappe.clear_document_cache("POS Profile", self.pos_profile)
		line = self._signed_line(pos_profile=self.pos_profile)
		self._apply(line, pos_profile=self.pos_profile)
		self.assertNotEqual(flt(line.discount_percentage), 40)
		self.assertFalse(line.pricing_rules)

	def test_snapshot_dropped_for_buying_only_rule(self):
		frappe.db.set_value("Pricing Rule", RULE, {"selling": 0, "buying": 1})
		frappe.clear_document_cache("Pricing Rule", RULE)
		line = self._signed_line()
		self._apply(line)
		self.assertNotEqual(flt(line.discount_percentage), 40)

	def test_snapshot_dropped_outside_time_window(self):
		from posnext_promotions.promotions import schedule

		if not schedule.schedule_fields_available():
			self.skipTest("Pricing Rule Active From / To Time fields missing")
		# "Happy hour": the rule runs 10:00-12:00 on the posting day only.
		frappe.db.set_value(
			"Pricing Rule",
			RULE,
			{
				"valid_from": DATE,
				"valid_upto": DATE,
				schedule.FROM_TIME_FIELD: "10:00:00",
				schedule.TO_TIME_FIELD: "12:00:00",
			},
		)
		frappe.clear_document_cache("Pricing Rule", RULE)
		held = self._invoice(self._signed_line(), DATE, "11:00:00")
		self.assertEqual(flt(held.discount_percentage), 40)
		late = self._invoice(self._signed_line(), DATE, "14:00:00")
		self.assertNotEqual(flt(late.discount_percentage), 40)
		self.assertEqual(flt(late.discount_percentage), 30)

	# ---- fix round 1: signature hardening, Ruling 25 binding ------------------

	def test_non_ascii_sig_returns_none(self):
		line = self._fnf_line()
		envelope = json.loads(ff_snapshot.sign(line, _promo_data()))
		envelope["sig"] = "é" * 64
		self.assertIsNone(ff_snapshot.verify(line, json.dumps(envelope)))
		envelope["sig"] = envelope["sig"].upper()
		self.assertIsNone(ff_snapshot.verify(line, json.dumps(envelope)))
		# Validate does not crash on it either.
		line[FIELD_FNF_REPLACED] = json.dumps({**envelope, "sig": "é"})
		self._invoice(line, DATE)
		self.assertEqual(flt(line.discount_percentage), 30)

	def test_v1_envelope_invalid(self):
		line = self._fnf_line()
		envelope = json.loads(ff_snapshot.sign(line, _promo_data()))
		self.assertEqual(envelope["v"], 2)
		envelope["v"] = 1
		self.assertIsNone(ff_snapshot.verify(line, json.dumps(envelope)))

	def test_snapshot_transplanted_to_other_customer_dropped(self):
		# Manual 40 % (no Pricing Rule): only the context binding can stop it.
		manual = _promo_data(rules="")
		manual["discount_source"] = None
		kept = self._signed_line(data=manual)
		self._apply(kept, customer=CUSTOMER)
		self.assertEqual(flt(kept.discount_percentage), 40)
		moved = self._signed_line(data=manual)
		self._apply(moved, customer=OTHER_CUSTOMER)
		self.assertEqual(flt(moved.discount_percentage), 30)
		self.assertEqual(moved.discount_source, FNF)

	def test_binding_covers_company_and_profile(self):
		line = self._fnf_line()
		raw = ff_snapshot.sign(line, _promo_data(), doc=self._cart(pos_profile=self.pos_profile))
		self.assertIsNotNone(ff_snapshot.verify(line, raw, doc=self._cart(pos_profile=self.pos_profile)))
		# Invoice from that cart: explicit company equal to the profile's.
		invoice = frappe._dict(customer=CUSTOMER, company=self.company, pos_profile=self.pos_profile)
		self.assertIsNotNone(ff_snapshot.verify(line, raw, doc=invoice))
		self.assertIsNone(ff_snapshot.verify(line, raw, doc=self._cart()))
		self.assertIsNone(ff_snapshot.verify(line, raw, doc={**invoice, "company": "_Other Company"}))

	def test_settings_snapshot_hours_at_least_one(self):
		for value in (0, None, -5):
			doc = frappe.get_single("Friends and Family Settings")
			doc.replaced_discount_snapshot_hours = value
			doc.save()
			self.assertEqual(doc.replaced_discount_snapshot_hours, 24)
		doc.replaced_discount_snapshot_hours = 2
		doc.save()
		line = self._fnf_line()
		with mock.patch.object(ff_snapshot.time, "time", return_value=1_900_000_000):
			from posnext_promotions.promotions.friends_and_family_apply import _replaced_snapshot

			raw = _replaced_snapshot(line, self._cart())
		self.assertEqual(json.loads(raw)["exp"], 1_900_000_000 + 2 * 3600)

	# ---- Ruling 26: coupon-based rules ------------------------------------------

	def _coupon_rule(self):
		frappe.db.set_value("Pricing Rule", RULE, "coupon_code_based", 1)
		frappe.clear_document_cache("Pricing Rule", RULE)
		name = "_Test FF Snapshot Coupon"
		if not frappe.db.exists("Coupon Code", name):
			frappe.get_doc(
				{
					"doctype": "Coupon Code",
					"coupon_name": name,
					"coupon_code": "FFSNAPCPN",
					"coupon_type": "Promotional",
					"pricing_rule": RULE,
				}
			).insert(ignore_permissions=True)
		return name

	def test_coupon_rule_snapshot_restored_while_coupon_on_doc(self):
		coupon = self._coupon_rule()
		line = self._invoice(self._signed_line(), DATE, coupon_code=coupon)
		self.assertEqual(flt(line.discount_percentage), 40)
		self.assertEqual(line.pricing_rules, RULE)

	def test_coupon_rule_snapshot_dropped_when_coupon_removed(self):
		self._coupon_rule()
		line = self._invoice(self._signed_line(), DATE)
		self.assertEqual(flt(line.discount_percentage), 30)
		self.assertEqual(line.discount_source, FNF)

	def test_coupon_rule_snapshot_dropped_for_other_coupon(self):
		self._coupon_rule()
		frappe.db.set_value("Pricing Rule", RULE_2, "coupon_code_based", 1)
		other = frappe.get_doc(
			{
				"doctype": "Coupon Code",
				"coupon_name": "_Test FF Snapshot Other Coupon",
				"coupon_code": "FFSNAPOTH",
				"coupon_type": "Promotional",
				"pricing_rule": RULE_2,
			}
		).insert(ignore_permissions=True)
		line = self._invoice(self._signed_line(), DATE, coupon_code=other.name)
		self.assertEqual(flt(line.discount_percentage), 30)
