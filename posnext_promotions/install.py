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
	_ensure_friends_and_family_customer_flag()
	_ensure_friends_and_family_invoice_fields()
	_ensure_friends_and_family_message_events()


def _ensure_friends_and_family_message_events():
	"""Structural: standard F&F message event definitions the engine fires (no message text)."""
	from posnext_promotions.promotions.friends_and_family_messages import ensure_standard_events

	ensure_standard_events()


def _ensure_friends_and_family_invoice_fields():
	"""PN-129: per-line F&F source + amount on Sales / POS Invoice Item (monthly cap ledger)."""
	from posnext_promotions.promotions.friends_and_family_caps import ensure_invoice_item_fields

	ensure_invoice_item_fields()


def _ensure_friends_and_family_customer_flag():
	from posnext_promotions.promotions.friends_and_family_eligibility import ensure_customer_flag_field

	ensure_customer_flag_field()
