# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Expose install flags on frappe.boot for optional POS UI (no Python imports of pos_next)."""


def extend(bootinfo):
	bootinfo["posnext_promotions"] = 1
	bootinfo["posnext_promotions_auth"] = 1
