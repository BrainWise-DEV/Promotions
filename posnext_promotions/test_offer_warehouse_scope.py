# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Warehouse-scoped offers: a Pricing Rule that names a warehouse is offered to, and applied for,
only the POS Profiles whose warehouse is that warehouse or sits below it.

Rule being tested (ERPNext's own): a rule without a warehouse applies everywhere; a rule with one
applies to that warehouse and to every warehouse under it (a rule on a group covers its children).
``get_offers`` (what the cashier is shown) and ``apply_offers`` (what is actually applied) must agree.

Data is created inside the tests and rolled back by the framework; nothing here commits.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from posnext_promotions.api.offers import (
	_get_promotional_scheme_offers,
	_get_standalone_pricing_rule_offers,
	apply_offers,
	get_offers,
	item_has_active_promotion,
)
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_C,
	_cart_payload,
	_ctx,
	_line,
	_make_rule,
)

HEADER_DISCOUNT = 12.5  # unusual on purpose: the transaction rule is recognised by its value


def _warehouse(company: str, name: str, parent: str | None = None, is_group: int = 0) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": name,
			"company": company,
			"parent_warehouse": parent,
			"is_group": is_group,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _profile(ctx, name: str, warehouse: str) -> str:
	profile = frappe.get_doc(
		{
			"doctype": "POS Profile",
			"name": name,
			"company": ctx.company,
			"warehouse": warehouse,
			"selling_price_list": ctx.price_list,
			"currency": ctx.currency,
			"customer": ctx.customer,
			"write_off_account": frappe.get_cached_value("Company", ctx.company, "write_off_account"),
			"write_off_cost_center": frappe.get_cached_value("Company", ctx.company, "cost_center"),
			"ignore_pricing_rule": 0,
			"disable_rounded_total": 1,
			"disabled": 0,
		}
	)
	profile.append("payments", {"mode_of_payment": ctx.mode_of_payment, "default": 1, "amount": 0})
	profile.insert(ignore_permissions=True)
	return profile.name


class TestOfferWarehouseScope(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ctx = ctx = _ctx()
		# group
		#  ├── A   (profiles: P_A and P_A2, two profiles on one warehouse)
		#  └── B   (profile:  P_B)
		# C        (profile:  P_C, outside the group)
		cls.group = _warehouse(ctx.company, "_PNXT WH Group", is_group=1)
		cls.wh_a = _warehouse(ctx.company, "_PNXT WH A", parent=cls.group)
		cls.wh_b = _warehouse(ctx.company, "_PNXT WH B", parent=cls.group)
		cls.wh_c = _warehouse(ctx.company, "_PNXT WH C")
		cls.p_a = _profile(ctx, "_PNXT_WH_PROFILE_A", cls.wh_a)
		cls.p_a2 = _profile(ctx, "_PNXT_WH_PROFILE_A2", cls.wh_a)
		cls.p_b = _profile(ctx, "_PNXT_WH_PROFILE_B", cls.wh_b)
		cls.p_c = _profile(ctx, "_PNXT_WH_PROFILE_C", cls.wh_c)
		cls.profile_warehouse = {cls.p_a: cls.wh_a, cls.p_a2: cls.wh_a, cls.p_b: cls.wh_b, cls.p_c: cls.wh_c}

	def setUp(self):
		super().setUp()
		# rules made by one test must not conflict with the next one's; undo them without committing
		frappe.db.savepoint("wh_scope_test")
		self.addCleanup(frappe.db.rollback, save_point="wh_scope_test")

	# -- helpers ---------------------------------------------------------

	def _rule(self, title: str, warehouse: str | None, discount: float = 10, **fields) -> str:
		apply_on = fields.pop("apply_on", "Item Code")
		# what the rule matches on besides Item Code: an item group / a brand (as ITEM_A / ITEM_C have)
		scope = {
			"Item Group": {
				"item_groups": [{"item_group": frappe.db.get_value("Item", ITEM_A, "item_group")}]
			},
			"Brand": {"brands": [{"brand": frappe.db.get_value("Item", ITEM_C, "brand")}]},
		}.get(apply_on, {})
		fields.setdefault("min_qty", 0)
		return _make_rule(
			f"_PNXT_TEST_WH_{title}",
			apply_on=apply_on,
			items=[{"item_code": ITEM_A}] if apply_on == "Item Code" else [],
			**scope,
			rate_or_discount="Discount Percentage",
			price_or_product_discount="Price",
			discount_percentage=discount,
			warehouse=warehouse,
			**fields,
		)

	def _shown(self, profile: str) -> set[str]:
		return {offer["name"] for offer in get_offers(profile)}

	def _applied_rules(self, profile: str, selected: list[str] | None = None) -> str:
		"""Names of the rules the engine put on the cart line, as one string."""
		cart = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A)])
		cart["pos_profile"] = profile
		cart["items"][0]["warehouse"] = self.profile_warehouse[profile]
		result = apply_offers(json.dumps(cart), json.dumps(selected) if selected else None)
		return str((result.get("items") or [{}])[0].get("pricing_rules") or "")

	def _header_discount(self, profile: str) -> float:
		cart = _cart_payload(self.ctx, [_line(self.ctx, ITEM_A)])
		cart["pos_profile"] = profile
		cart["items"][0]["warehouse"] = self.profile_warehouse[profile]
		return float(apply_offers(json.dumps(cart)).get("additional_discount_percentage") or 0)

	# -- what the cashier is shown --------------------------------------

	def test_rule_for_one_warehouse_is_shown_only_there(self):
		rule = self._rule("OnlyA", self.wh_a)
		self.assertIn(rule, self._shown(self.p_a))
		self.assertNotIn(rule, self._shown(self.p_b))  # sibling warehouse
		self.assertNotIn(rule, self._shown(self.p_c))  # outside the group

	def test_rule_without_warehouse_is_shown_to_every_profile(self):
		rule = self._rule("Everywhere", None)
		for profile in self.profile_warehouse:
			self.assertIn(rule, self._shown(profile), profile)

	def test_group_warehouse_covers_its_children_and_nothing_else(self):
		rule = self._rule("OnGroup", self.group)
		self.assertIn(rule, self._shown(self.p_a))
		self.assertIn(rule, self._shown(self.p_b))
		self.assertNotIn(rule, self._shown(self.p_c))

	def test_a_child_warehouse_rule_does_not_leak_to_its_parent_group_profiles(self):
		# the rule names B; a profile on A (same parent) must not see it
		rule = self._rule("OnlyB", self.wh_b)
		self.assertIn(rule, self._shown(self.p_b))
		self.assertNotIn(rule, self._shown(self.p_a))

	def test_profiles_on_the_same_warehouse_see_the_same_rules(self):
		rule = self._rule("OnlyA2", self.wh_a)
		self.assertIn(rule, self._shown(self.p_a))
		self.assertIn(rule, self._shown(self.p_a2))

	def test_each_profile_sees_its_own_and_unrestricted_rules_only(self):
		rule_a = self._rule("MixA", self.wh_a)
		rule_b = self._rule("MixB", self.wh_b)
		rule_c = self._rule("MixC", self.wh_c)
		rule_any = self._rule("MixAny", None)
		mine = {rule_a, rule_b, rule_c, rule_any}
		self.assertEqual(self._shown(self.p_a) & mine, {rule_a, rule_any})
		self.assertEqual(self._shown(self.p_b) & mine, {rule_b, rule_any})
		self.assertEqual(self._shown(self.p_c) & mine, {rule_c, rule_any})

	def test_a_profile_without_a_warehouse_gets_only_unrestricted_rules(self):
		# POS Profile.warehouse is mandatory in ERPNext; this guards the helper's fallback anyway
		restricted = self._rule("NoWhRestricted", self.wh_a)
		open_rule = self._rule("NoWhOpen", None)
		names = {
			o.name for o in _get_standalone_pricing_rule_offers(self.ctx.company, nowdate(), warehouse=None)
		}
		self.assertIn(open_rule, names)
		self.assertNotIn(restricted, names)

	def test_expired_and_disabled_rules_are_still_left_out(self):
		expired = self._rule(
			"Expired", self.wh_a, valid_from=add_days(nowdate(), -10), valid_upto=add_days(nowdate(), -1)
		)
		disabled = self._rule("Disabled", self.wh_a, disable=1)
		live = self._rule("Live", self.wh_a)
		shown = self._shown(self.p_a)
		self.assertIn(live, shown)
		self.assertNotIn(expired, shown)
		self.assertNotIn(disabled, shown)

	def test_offers_keep_priority_order(self):
		low = self._rule("PrioLow", self.wh_a, priority="1")
		high = self._rule("PrioHigh", self.wh_a, priority="9")
		order = [o["name"] for o in get_offers(self.p_a) if o["name"] in (low, high)]
		self.assertEqual(order, [high, low])

	def test_promotional_scheme_rules_respect_the_warehouse_too(self):
		scheme = frappe.get_doc(
			{
				"doctype": "Promotional Scheme",
				"name": "_PNXT_TEST_WH_SCHEME",
				"company": self.ctx.company,
				"apply_on": "Item Code",
				"selling": 1,
				"items": [{"item_code": ITEM_A}],
				"price_discount_slabs": [
					{
						"rule_description": "warehouse scoped",
						"rate_or_discount": "Discount Percentage",
						"discount_percentage": 10,
						"warehouse": self.wh_a,
					}
				],
			}
		).insert(ignore_permissions=True)
		rule = frappe.db.get_value("Pricing Rule", {"promotional_scheme": scheme.name}, "name")
		self.assertTrue(rule, "the scheme should have generated a Pricing Rule")

		in_scope = {o.name for o in _get_promotional_scheme_offers(self.ctx.company, nowdate(), self.wh_a)}
		other = {o.name for o in _get_promotional_scheme_offers(self.ctx.company, nowdate(), self.wh_b)}
		self.assertIn(rule, in_scope)
		self.assertNotIn(rule, other)
		self.assertIn(rule, self._shown(self.p_a))
		self.assertNotIn(rule, self._shown(self.p_b))

	def test_transaction_rules_ignore_a_stale_warehouse(self):
		# ERPNext hides the warehouse field on Transaction rules and never evaluates it, so a stored
		# value is not a restriction: the rule is offered, and applied, everywhere.
		rule = self._rule(
			"Txn",
			self.wh_a,
			discount=HEADER_DISCOUNT,
			apply_on="Transaction",
			min_amt=1,
			apply_discount_on="Grand Total",
		)
		for profile in (self.p_a, self.p_b, self.p_c):
			self.assertIn(rule, self._shown(profile), profile)
			self.assertAlmostEqual(self._header_discount(profile), HEADER_DISCOUNT, msg=profile)

	# -- what is actually applied --------------------------------------

	def test_applied_offers_follow_the_same_warehouse_rule(self):
		rule = self._rule("ApplyA", self.wh_a)
		self.assertIn(rule, self._applied_rules(self.p_a))
		self.assertNotIn(rule, self._applied_rules(self.p_b))
		self.assertNotIn(rule, self._applied_rules(self.p_c))

	def test_unrestricted_rule_is_applied_in_every_warehouse(self):
		rule = self._rule("ApplyAny", None)
		for profile in self.profile_warehouse:
			self.assertIn(rule, self._applied_rules(profile), profile)

	def test_group_rule_is_applied_to_children_only(self):
		rule = self._rule("ApplyGroup", self.group)
		self.assertIn(rule, self._applied_rules(self.p_a))
		self.assertIn(rule, self._applied_rules(self.p_b))
		self.assertNotIn(rule, self._applied_rules(self.p_c))

	def test_naming_a_foreign_warehouse_rule_cannot_force_it_onto_the_cart(self):
		# apply_offers takes rule names from the client; a rule the engine refused for this
		# warehouse must not come back just because its name was sent.
		rule = self._rule("Forced", self.wh_a)
		self.assertNotIn(rule, self._applied_rules(self.p_b, selected=[rule]))
		self.assertNotIn(rule, self._applied_rules(self.p_c, selected=[rule]))
		self.assertIn(rule, self._applied_rules(self.p_a, selected=[rule]))

	def test_what_is_shown_is_what_is_applied(self):
		# One rule at a time: with several matching rules ERPNext applies only the best-priority one,
		# which would hide whether a given rule was refused for its warehouse or just outranked.
		specs = {"A": self.wh_a, "B": self.wh_b, "Group": self.group, "C": self.wh_c, "Any": None}
		for label, warehouse in specs.items():
			frappe.db.savepoint("wh_scope_agree")
			rule = self._rule(f"Agree{label}", warehouse)
			for profile in self.profile_warehouse:
				self.assertEqual(
					rule in self._shown(profile),
					rule in self._applied_rules(profile),
					f"{label} rule, profile {profile}",
				)
			frappe.db.rollback(save_point="wh_scope_agree")

	# -- the manual-discount lock (item_has_active_promotion) ---------------

	QTY = 7  # a narrow window, so no other rule on the site can satisfy the lookup

	def _locked_by(self, item: str, warehouse: str | None, **kwargs) -> str | None:
		"""The rule that locks the item for ``warehouse`` (or None)."""
		result = item_has_active_promotion(item, self.ctx.company, self.QTY, warehouse, **kwargs)
		return result.get("pricing_rule") if result.get("has_promotion") else None

	def _qty_rule(self, title: str, warehouse: str | None, **fields) -> str:
		return self._rule(title, warehouse, min_qty=self.QTY, max_qty=self.QTY, **fields)

	def test_a_promotion_for_another_warehouse_does_not_lock_the_discount(self):
		rule = self._qty_rule("LockA", self.wh_a)
		self.assertEqual(self._locked_by(ITEM_A, self.wh_a), rule)
		self.assertIsNone(self._locked_by(ITEM_A, self.wh_b))  # sibling warehouse
		self.assertIsNone(self._locked_by(ITEM_A, self.wh_c))  # outside the group

	def test_an_unrestricted_promotion_locks_in_every_warehouse(self):
		rule = self._qty_rule("LockAny", None)
		for warehouse in self.profile_warehouse.values():
			self.assertEqual(self._locked_by(ITEM_A, warehouse), rule, warehouse)

	def test_a_group_warehouse_promotion_locks_its_children_only(self):
		rule = self._qty_rule("LockGroup", self.group)
		self.assertEqual(self._locked_by(ITEM_A, self.wh_a), rule)
		self.assertEqual(self._locked_by(ITEM_A, self.wh_b), rule)
		self.assertIsNone(self._locked_by(ITEM_A, self.wh_c))

	def test_item_group_and_brand_promotions_respect_the_warehouse(self):
		by_group = self._qty_rule("LockItemGroup", self.wh_a, apply_on="Item Group")
		self.assertEqual(self._locked_by(ITEM_A, self.wh_a), by_group)
		self.assertIsNone(self._locked_by(ITEM_A, self.wh_b))
		# ITEM_C shares ITEM_A's item group, so put the brand rule on another warehouse to isolate it
		by_brand = self._qty_rule("LockBrand", self.wh_b, apply_on="Brand")
		self.assertEqual(self._locked_by(ITEM_C, self.wh_b), by_brand)
		self.assertIsNone(self._locked_by(ITEM_C, self.wh_c))

	def test_callers_that_send_no_warehouse_keep_the_old_behaviour(self):
		# an older cached screen does not send a warehouse: every warehouse's rules still count
		rule = self._qty_rule("LockNoWarehouse", self.wh_a)
		self.assertEqual(self._locked_by(ITEM_A, None), rule)

	def test_the_quantity_window_and_disabled_rules_still_decide(self):
		rule = self._qty_rule("LockWindow", self.wh_a)
		result = item_has_active_promotion(ITEM_A, self.ctx.company, self.QTY + 1, self.wh_a)
		self.assertNotEqual(result.get("pricing_rule"), rule)  # outside min_qty..max_qty
		frappe.db.set_value("Pricing Rule", rule, "disable", 1)
		self.assertIsNone(self._locked_by(ITEM_A, self.wh_a))
