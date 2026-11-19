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

from posnext_promotions.api.coupon_engine import _item_matches_scope, with_item_master_scope

SCOPED = ("Item Code", "Item Group", "Brand")
# Currency rounding on the client can leave a line a few cents above its allocation.
TOLERANCE = 0.05


def validate_scoped_coupon_lines(doc, method=None):
	tagged = [
		row
		for row in doc.get("items") or []
		if row.get("posnext_coupon_code") and not cint(row.get("is_free_item"))
	]
	if not tagged:
		return

	_restore_offline_header_coupon(doc, tagged)

	coupons = {}
	for code in {row.posnext_coupon_code.upper() for row in tagged}:
		name = frappe.db.get_value("POS Coupon", {"coupon_code": code})
		if not name:
			frappe.throw(_("POS Coupon {0} does not exist").format(code))
		coupons[code] = frappe.get_cached_doc("POS Coupon", name)

	spent = {code: 0.0 for code in coupons}
	# Scope is judged on the Item master's brand / group, never the row's (one query).
	scope_lines = with_item_master_scope({"item_code": row.item_code} for row in tagged)
	for row, scope_line in zip(tagged, scope_lines, strict=True):
		code = row.posnext_coupon_code.upper()
		coupon = coupons[code]
		if cint(coupon.get("disabled")):
			frappe.throw(_("Row {0}: POS Coupon {1} is disabled").format(row.idx, code))
		if (doc.get("coupon_code") or "").upper() != code:
			frappe.throw(_("Row {0}: POS Coupon {1} is not the coupon applied to this invoice").format(row.idx, code))

		scope = coupon.get("apply_scope") or "All Eligible Items"
		if scope in SCOPED:
			if not _item_matches_scope(coupon, scope_line):
				frappe.throw(
					_("Row {0}: item {1} is not covered by POS Coupon {2}").format(row.idx, row.item_code, code)
				)

		if coupon.discount_type == "Percentage" and flt(row.discount_percentage) > flt(coupon.discount_percentage) + TOLERANCE:
			frappe.throw(
				_("Row {0}: discount {1}% exceeds POS Coupon {2} ({3}%)").format(
					row.idx, flt(row.discount_percentage), code, flt(coupon.discount_percentage)
				)
			)
		spent[code] += flt(row.discount_amount) * flt(row.qty)

	for code, total in spent.items():
		coupon = coupons[code]
		cap = flt(coupon.max_amount) if flt(coupon.max_amount) else None
		if coupon.discount_type == "Amount":
			cap = min(cap, flt(coupon.discount_amount)) if cap else flt(coupon.discount_amount)
		# Percentage coupons without a cap are priced as a percentage, not an amount.
		if cap and total > cap + TOLERANCE:
			frappe.throw(_("POS Coupon {0} discount {1} exceeds its limit of {2}").format(code, total, cap))


def _restore_offline_header_coupon(doc, tagged) -> None:
	"""An offline sale being synced with tagged lines but no header coupon takes that coupon (D-18).

	The POS offline checkout queues the sale without its header ``coupon_code``
	while the lines keep ``posnext_coupon_code``. Restoring the header (only when
	every tagged line names the same existing coupon) lets the coupon be counted
	on submit, where POS Settings ``offline_coupon_overshoot`` decides an over-limit
	sale: "Allow and log" keeps and logs it, "Reject" refuses it. Online sales and
	mixed codes are left to the checks below.

	``flags.pos_offline_sync`` only means the caller sent an ``offline_id``; it is
	not proof the sale happened offline. The restore is safe on that basis alone
	because it grants nothing a header ``coupon_code`` would not (the coupon is
	still checked per line here and re-checked and counted on submit).
	"""
	flags = getattr(doc, "flags", None) or {}
	if not flags.get("pos_offline_sync") or (doc.get("coupon_code") or "").strip():
		return
	codes = {row.posnext_coupon_code.strip().upper() for row in tagged}
	if len(codes) != 1:
		return
	from pos_next.pos_next.doctype.pos_coupon.pos_coupon import resolve_coupon_name

	name = resolve_coupon_name(codes.pop())  # case-safe whatever the DB collation
	if name:
		doc.coupon_code = frappe.db.get_value("POS Coupon", name, "coupon_code")


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
