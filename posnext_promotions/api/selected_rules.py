# Copyright (c) 2026, BrainWise and contributors
"""Selected offers ERPNext's own rule selection picks for a cart (G5, ledger 531).

``apply_offers`` takes the cart's applied offers as ``selected_offers``. A selected
name the per-line pricing call did not return is applied only if
:func:`selected_rules_that_win` confirms it, the same way pos_next's free-row check
confirms a client-named rule (``_cart_gift_winners``, Ruling 148): ERPNext's
``get_pricing_rules`` must return it for one of the cart's paid lines, with the
cart's customer, groups, territory, company, price list, date and POS flag, the
rule's condition, quantity and amount thresholds, priority and every competing
rule. A GWP or Gift Pool rule is judged on the cart's summed quantity and amount
over the rule's items (a Gift Pool's own pool items do not count), as the cart's
evaluator grants it. The one-time rules this customer already redeemed are left out
of the lookup, and the price list is the POS Profile's, never the client's (Ruling 154). A lookup error or an unresolved conflict picks nothing.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt

AGGREGATED_PROMOTION_TYPES = ("GWP", "Gift Pool")
AGGREGATED_RULES_FLAG = "posnext_promotions_aggregated_rule_totals"
EXCLUDED_RULES_FLAG = "posnext_promotions_excluded_pricing_rules"
# A pricing line's own document keys: the lookup is made for the cart, not the row.
_ROW_ONLY = ("doctype", "name", "parenttype")


class _Cart(frappe._dict):
	"""The cart as ERPNext's rule selection reads a document (``doc.as_dict()``)."""

	def as_dict(self, *args, **kwargs):
		return self


def _install_aggregated_qty() -> None:
	"""While ``frappe.flags[AGGREGATED_RULES_FLAG]`` holds a callable, ERPNext's quantity
	and amount filter judges each rule it gives totals for on those totals; every other
	rule keeps the line's own. Installed once per process; inert while the flag is unset."""
	from erpnext.accounts.doctype.pricing_rule import utils

	if getattr(utils.filter_pricing_rules_for_qty_amount, "posnext_promotions_aggregates", False):
		return
	original = utils.filter_pricing_rules_for_qty_amount

	def filter_pricing_rules_for_qty_amount(qty, rate, pricing_rules, args=None):
		totals = frappe.flags.get(AGGREGATED_RULES_FLAG)
		if not totals:
			return original(qty, rate, pricing_rules, args)
		kept = []
		for rule in pricing_rules:
			rule_qty, rule_amount = totals(rule) or (qty, rate)
			if original(rule_qty, rule_amount, [rule], args):
				kept.append(rule)
		return kept

	filter_pricing_rules_for_qty_amount.posnext_promotions_aggregates = True
	utils.filter_pricing_rules_for_qty_amount = filter_pricing_rules_for_qty_amount


def _install_rule_exclusion() -> None:
	"""While ``frappe.flags[EXCLUDED_RULES_FLAG]`` names rules, ERPNext's rule queries leave
	them out (pos_next ``_install_rule_exclusion``). Installed once; inert while unset."""
	from erpnext.accounts.doctype.pricing_rule import utils

	if getattr(utils.get_other_conditions, "posnext_promotions_excludes_rules", False):
		return
	original = utils.get_other_conditions

	def get_other_conditions(*args, **kwargs):
		conditions = original(*args, **kwargs)
		excluded = frappe.flags.get(EXCLUDED_RULES_FLAG)
		if excluded:
			values = kwargs["values"] if "values" in kwargs else args[1]
			conditions += f" and `tabPricing Rule`.name not in %({EXCLUDED_RULES_FLAG})s"
			values[EXCLUDED_RULES_FLAG] = tuple(sorted(excluded))
		return conditions

	get_other_conditions.posnext_promotions_excludes_rules = True
	utils.get_other_conditions = get_other_conditions


def _rule_totals(items):
	"""rule → (stock qty, amount) of ``items`` in a GWP / Gift Pool rule's scope (its Gift
	Pool items left out); None for any other rule. pos_next's ``_rule_totals`` exactly."""
	from erpnext.accounts.doctype.pricing_rule.utils import get_pricing_rule_items

	from posnext_promotions.api.gift_pool import get_scheme_gift_pools

	cache = {}

	def totals(rule):
		name = rule.get("name")
		if cstr(rule.get("promotion_type")) not in AGGREGATED_PROMOTION_TYPES or not name:
			return None
		if name not in cache:
			pr_doc = frappe.get_cached_doc("Pricing Rule", name)
			scope = set(get_pricing_rule_items(pr_doc) or [])
			field = frappe.scrub(pr_doc.apply_on or "")
			pool = set()
			if pr_doc.get("promotion_type") == "Gift Pool" and pr_doc.get("promotional_scheme"):
				for codes in get_scheme_gift_pools(pr_doc.promotional_scheme).values():
					pool.update(codes)
			rows = [item for item in items if item.get(field) in scope and item.get("item_code") not in pool]
			cache[name] = (
				sum(flt(item.get("stock_qty")) for item in rows),
				sum(flt(item.get("price_list_rate")) * flt(item.get("qty")) for item in rows),
			)
		return cache[name]

	return totals


def selected_rules_that_win(names, pricing_args, pricing_items, price_list=None, redeemed=()) -> set[str]:
	"""The ``names`` ERPNext's ``get_pricing_rules`` picks for one of ``pricing_items``
	(the cart's paid lines, as priced) on ``pricing_args`` (the cart's pricing args), on
	``price_list`` (the POS Profile's), with the ``redeemed`` one-time rules left out of
	the lookup (and never picked)."""
	names = {cstr(name) for name in names or () if cstr(name)}
	if not names or not pricing_items:
		return set()
	try:
		from erpnext.accounts.doctype.pricing_rule.utils import get_pricing_rules
	except ImportError:
		return set()

	base = frappe._dict(
		{key: value for key, value in pricing_args.items() if key != "items"},
		transaction_type="selling",
	)
	if price_list:
		base.price_list = price_list
	redeemed = set(redeemed or ())
	cart = _Cart(base, items=list(pricing_items))
	won = set()
	_install_aggregated_qty()
	_install_rule_exclusion()
	previous = frappe.flags.get(AGGREGATED_RULES_FLAG)
	previous_excluded = frappe.flags.get(EXCLUDED_RULES_FLAG)
	frappe.flags[AGGREGATED_RULES_FLAG] = _rule_totals(pricing_items)
	frappe.flags[EXCLUDED_RULES_FLAG] = set(previous_excluded or ()) | redeemed
	try:
		for item in pricing_items:
			args = frappe._dict(base, **{k: v for k, v in item.items() if k not in _ROW_ONLY})
			message_count = len(frappe.local.message_log or [])
			try:
				rules = get_pricing_rules(args, cart) or []
			except Exception:
				# An unresolved conflict (or any lookup error) picks nothing here.
				del frappe.local.message_log[message_count:]
				continue
			won.update(rule.get("name") for rule in rules if isinstance(rule, dict) and rule.get("name"))
			if names <= won:
				break
	finally:
		frappe.flags[AGGREGATED_RULES_FLAG] = previous
		frappe.flags[EXCLUDED_RULES_FLAG] = previous_excluded
	return (won & names) - redeemed
