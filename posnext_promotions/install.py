# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Install helpers.

POS Coupon extras (scope fields and child tables) are Custom Fields declared in
``posnext_promotions/custom/pos_coupon.json``; Frappe syncs them on install and
on every migrate (``sync_on_migrate``).
"""

from __future__ import annotations

import frappe

# Module Def names this app owns. They may already exist on a site that ran
# staging pos_next (promotions lived there before the split).
#
# "POS Next Auth Gate" is deliberately absent: the authorization gate belongs to
# pos_next (its modules.txt, DocTypes and rehome patch). Deleting that Module Def
# here left sites without it when pos_next's migrate ran before this install
# (the Frappe Cloud order: update site, then install from the dashboard).
OWNED_MODULE_DEFS = ("POSNext Promotions",)


def before_install():
	"""Reclaim Module Defs left behind by pos_next so install can insert them."""
	for module in OWNED_MODULE_DEFS:
		if not frappe.db.exists("Module Def", module):
			continue
		frappe.delete_doc("Module Def", module, force=1, ignore_permissions=True)


def after_migrate():
	"""Runs after sync_customizations, so the custom/*.json fields exist by now."""
	_backfill_coupon_promotion_conflict()


def _backfill_coupon_promotion_conflict():
	"""Give coupons saved before `promotion_conflict` existed their old behaviour:
	checked exclude_already_discounted_items skips, unchecked stacks (from #13). Idempotent."""
	if not frappe.db.exists("DocType", "POS Coupon"):
		return
	if not frappe.get_meta("POS Coupon").has_field("promotion_conflict"):
		return
	for excluded, mode in ((1, "Skip the line"), (0, "Add on top")):
		frappe.db.set_value(
			"POS Coupon",
			{"promotion_conflict": ["is", "not set"], "exclude_already_discounted_items": excluded},
			"promotion_conflict",
			mode,
			update_modified=False,
		)
