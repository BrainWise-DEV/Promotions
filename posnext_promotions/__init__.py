# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

try:
	import frappe
except ModuleNotFoundError:  # pragma: no cover
	frappe = None

__version__ = "0.1.0"


# ERPNext copies only a fixed list of slab fields onto generated Pricing Rules.
try:
	from erpnext.accounts.doctype.promotional_scheme import promotional_scheme as _promotional_scheme

	for _slab_field in (
		"apply_discount_on_price",
		"min_or_max_discount_qty_limit",
		"max_accumulated_discount_percentage",
		"min_scopes_required",
	):
		if _slab_field not in _promotional_scheme.price_discount_fields:
			_promotional_scheme.price_discount_fields.append(_slab_field)
except Exception:
	if frappe:
		frappe.log_error(frappe.get_traceback(), "Promotional Scheme Field Patch Error")

try:
	from erpnext.accounts.doctype.pricing_rule import utils as pr_utils

	from posnext_promotions.overrides.pricing_rule import patch_get_other_conditions

	patch_get_other_conditions(pr_utils)
except Exception:
	pass

try:
	from erpnext.accounts.doctype.pricing_rule import pricing_rule as _erpnext_pricing_rule

	from posnext_promotions.overrides.pricing_rule import (
		apply_price_discount_rule as _promo_apply_price_discount_rule,
	)

	_erpnext_pricing_rule.apply_price_discount_rule = _promo_apply_price_discount_rule
except Exception:
	if frappe:
		frappe.log_error(frappe.get_traceback(), "Pricing Rule Override Error")
