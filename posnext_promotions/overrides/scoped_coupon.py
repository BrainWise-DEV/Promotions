# Copyright (c) 2026, BrainWise and contributors
"""Server-side re-check of lines the POS discounted with a scoped POS Coupon.

The POS client tags each such line with ``posnext_coupon_code``. The client
prices the lines from ``validate_coupon``, but nothing stops a stale or altered
client (or an offline invoice synced later) from tagging lines the coupon does
not cover, so the invoice is checked here before it is saved.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from posnext_promotions.api.coupon_engine import (
	PROMOTION_CONFLICT_ADD,
	_item_matches_scope,
	get_promotion_conflict_mode,
)

SCOPED = ("Item Code", "Item Group", "Brand")
# Currency rounding on the client can leave a line a few cents above its allocation.
TOLERANCE = 0.05
# Per-line currency rounding allowance for the coupon share (amounts, not percent).
AMOUNT_TOLERANCE = 0.01


def validate_scoped_coupon_lines(doc, method=None):
	tagged = [
		row
		for row in doc.get("items") or []
		if row.get("posnext_coupon_code") and not cint(row.get("is_free_item"))
	]
	if not tagged:
		return

	coupons = {}
	for code in {row.posnext_coupon_code.upper() for row in tagged}:
		name = frappe.db.get_value("POS Coupon", {"coupon_code": code})
		if not name:
			frappe.throw(_("POS Coupon {0} does not exist").format(code))
		coupons[code] = frappe.get_cached_doc("POS Coupon", name)

	spent = {code: 0.0 for code in coupons}
	for row in tagged:
		code = row.posnext_coupon_code.upper()
		coupon = coupons[code]
		if cint(coupon.get("disabled")):
			frappe.throw(_("Row {0}: POS Coupon {1} is disabled").format(row.idx, code))
		if (doc.get("coupon_code") or "").upper() != code:
			frappe.throw(_("Row {0}: POS Coupon {1} is not the coupon applied to this invoice").format(row.idx, code))

		scope = coupon.get("apply_scope") or "All Eligible Items"
		if scope in SCOPED:
			item = {
				"item_code": row.item_code,
				"brand": row.get("brand")
				or frappe.db.get_value("Item", row.item_code, "brand"),
				"item_group": row.get("item_group")
				or frappe.db.get_value("Item", row.item_code, "item_group"),
			}
			if not _item_matches_scope(coupon, item):
				frappe.throw(
					_("Row {0}: item {1} is not covered by POS Coupon {2}").format(row.idx, row.item_code, code)
				)

		share, room = coupon_share(row, coupon)
		if coupon.discount_type == "Percentage":
			pct = flt(coupon.discount_percentage)
			if room > 0:
				# Only the coupon's own share counts; a promotion under it is not the coupon's.
				over = share > room * pct / 100.0 + max(room * TOLERANCE / 100.0, AMOUNT_TOLERANCE)
				shown = flt(share * 100.0 / room, 2)
			else:
				# No price on the row to split the discount: check its percentage as before.
				shown = flt(row.discount_percentage)
				over = shown > pct + TOLERANCE
			if over:
				frappe.throw(
					_("Row {0}: discount {1}% exceeds POS Coupon {2} ({3}%)").format(row.idx, shown, code, pct)
				)
		spent[code] += share

	for code, total in spent.items():
		coupon = coupons[code]
		cap = flt(coupon.max_amount) if flt(coupon.max_amount) else None
		if coupon.discount_type == "Amount":
			cap = min(cap, flt(coupon.discount_amount)) if cap else flt(coupon.discount_amount)
		# Percentage coupons without a cap are priced as a percentage, not an amount.
		if cap and total > cap + TOLERANCE:
			frappe.throw(_("POS Coupon {0} discount {1} exceeds its limit of {2}").format(code, total, cap))


def _line_discount(row):
	"""(line value before any discount, whole line discount) from the saved row fields.

	``discount_amount`` on a Sales Invoice Item is per unit; ERPNext recomputes it as
	``price_list_rate - rate``. Returns base 0 when the row has no price to measure against.
	"""
	qty = flt(row.get("qty"))
	price = flt(row.get("price_list_rate"))
	unit_discount = flt(row.get("discount_amount"))
	if price > 0 and row.get("rate") is not None and flt(row.get("rate")) < price:
		unit_discount = max(unit_discount, price - flt(row.get("rate")))
	if price > 0:
		unit_discount = max(unit_discount, price * flt(row.get("discount_percentage")) / 100.0)
		return price * qty, min(unit_discount, price) * qty
	return 0.0, unit_discount * qty


def coupon_share(row, coupon):
	"""(coupon's own discount on the line, line value the coupon was taken from).

	Only "Add on top" leaves a promotion under the coupon (the coupon takes its % of
	what the promotion left, see ``coupon_engine.apply_coupon_to_items``). "Skip the
	line" never prices discounted lines and "Better of the two" replaces the promotion,
	so on those lines the whole discount is the coupon's.

	The share is ``posnext_coupon_discount`` when the POS stored it. When that field is
	empty, the fallback subtracts only the percentage of Pricing Rules still named on
	the row (see ``_promotion_percentage``). A cashier's discount has no Pricing Rule,
	so the fallback cannot see it and the whole line discount is checked against the
	coupon. Invoices saved before this field existed, and offline invoices from an
	older POS build, have no stored share: a cashier discount plus an "Add on top"
	coupon on those is still refused. That strict default is intentional.
	"""
	base, total = _line_discount(row)
	if base <= 0:
		return total, 0.0

	promo = 0.0
	if get_promotion_conflict_mode(coupon) == PROMOTION_CONFLICT_ADD:
		sent = flt(row.get("posnext_coupon_discount"))
		if sent > 0:
			# The POS sends the coupon's share of the line (whole line, not per unit).
			promo = total - min(sent, total)
		else:
			promo = min(base * _promotion_percentage(row) / 100.0, total)
	share = max(total - promo, 0.0)
	return share, max(base - promo, 0.0)


def _promotion_percentage(row) -> float:
	"""Percentage of the Pricing Rules still named on the row, when it can be read (0 otherwise).

	Only rules whose ``rate_or_discount`` is "Discount Percentage" are read. An
	amount-type offer under the coupon is invisible to the ``coupon_share`` fallback,
	the same as a cashier's discount, and that part of the line is checked as the coupon's.
	"""
	from posnext_promotions.api.promotion_exclusions import _parse_pricing_rules

	names = _parse_pricing_rules(row.get("pricing_rules"))
	if not names:
		return 0.0
	rules = frappe.get_all(
		"Pricing Rule",
		filters={"name": ["in", names], "rate_or_discount": "Discount Percentage"},
		pluck="discount_percentage",
	)
	return max((flt(p) for p in rules), default=0.0)


# Scope -> (child table, legacy single field or None, label for the message)
SCOPE_TABLES = {
	"Item Code": ("applicable_items", None, _("Applicable Items")),
	"Item Group": ("applicable_item_groups", "applicable_item_group", _("Applicable Item Groups")),
	"Brand": ("applicable_brands", "applicable_brand", _("Applicable Brands")),
}


def validate_coupon_scope(doc, method=None):
	"""POS Coupon save: a restricted scope needs at least one Item / Item Group / Brand.

	A coupon saved with an empty list matches no cart line, so every cart would be
	rejected. Only checked on save; coupons already stored empty keep loading.
	"""
	scope = doc.get("apply_scope") or "All Eligible Items"
	spec = SCOPE_TABLES.get(scope)
	if not spec:
		return
	table, legacy, label = spec
	if any(row.get(_ROW_FIELD[table]) for row in (doc.get(table) or [])):
		return
	if legacy and doc.get(legacy):
		return
	frappe.throw(
		_("Scope is {0}: add at least one row to {1} or change the scope to All Eligible Items").format(
			frappe.bold(_(scope)), frappe.bold(label)
		),
		title=_("Coupon scope is empty"),
	)


_ROW_FIELD = {
	"applicable_items": "item_code",
	"applicable_item_groups": "item_group",
	"applicable_brands": "brand",
}
