# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Add GWP free items child table and extend scheme-level threshold visibility."""

import frappe

_GWP_THRESHOLD_DEPENDS = "eval:doc.pos_is_accumulative || doc.promotion_type == 'GWP'"

_THRESHOLD_FIELDS = (
	"Promotional Scheme-min_qty",
	"Promotional Scheme-max_qty",
	"Promotional Scheme-min_amount",
	"Promotional Scheme-max_amount",
	"Promotional Scheme-pos_accumulative_section",
)


def execute():
	for name in _THRESHOLD_FIELDS:
		if frappe.db.exists("Custom Field", name):
			frappe.db.set_value("Custom Field", name, "depends_on", _GWP_THRESHOLD_DEPENDS)

	frappe.clear_cache(doctype="Promotional Scheme")
	frappe.clear_cache(doctype="POS GWP Free Item")
