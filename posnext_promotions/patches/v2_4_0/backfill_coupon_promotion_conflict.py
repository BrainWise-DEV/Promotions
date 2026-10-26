# Copyright (c) 2026, BrainWise and contributors
"""Give coupons saved before `promotion_conflict` existed their old behaviour:
exclude_already_discounted_items checked -> "Skip the line", unchecked -> "Add on top".
(Ported from #13's install.after_migrate; main declares fields in custom/*.json.)"""

import frappe


def execute():
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
