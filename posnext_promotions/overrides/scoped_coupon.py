# Copyright (c) 2026, BrainWise and contributors
"""Server-side re-check of lines the POS discounted with a scoped POS Coupon.

The POS client tags each such line with ``posnext_coupon_code``. The client
prices the lines from ``validate_coupon``, but nothing stops a stale or altered
client (or an offline invoice synced later) from tagging lines the coupon does
not cover, so the invoice is checked here before it is saved.
"""

import json

import frappe
from frappe import _
from frappe.query_builder.functions import Upper
from frappe.utils import cint, flt

from posnext_promotions.api.coupon_engine import _item_matches_scope, with_item_master_scope

SCOPED = ("Item Code", "Item Group", "Brand")
# Currency rounding on the client can leave a line a few cents above its allocation.
TOLERANCE = 0.05


# Error Log title of a coupon check an offline sync keeps instead of refusing (G5 review I-1).
OFFLINE_KEPT_LOG_TITLE = "POS Coupon check kept on offline sync"


def validate_scoped_coupon_lines(doc, method=None):
	"""Each tagged line is checked against its coupon; only the coupon's own share of a
	line's discount (``coupon_share``) counts against the coupon's limits (G5).

	Online a coupon over its limit (its % or its cap / fixed amount) refuses the save. On
	an offline sync (the server-set ``flags.pos_offline_sync``, never a client field) the
	sale already happened and the customer has left, and the server may no longer see the
	promotion under a stacked line: the limit is not enforced, the conservative share is
	kept and each breach is written to an Error Log (``OFFLINE_KEPT_LOG_TITLE``) with the
	coupon, the row, the share, the promotion under it and the limit, as pos_next keeps and
	logs an offline sale's unexplained discount (G5 review I-1). The coupon's identity and
	scope (it exists, is enabled, is the invoice's, covers the item) stay strict on every
	route (D-18).
	"""
	from posnext_promotions.overrides.coupon_share import set_coupon_shares

	tagged = [
		row
		for row in doc.get("items") or []
		if row.get("posnext_coupon_code") and not cint(row.get("is_free_item"))
	]
	if not tagged:
		set_coupon_shares(doc, [], {})
		return

	refuse = _refuser(doc)
	_restore_offline_header_coupon(doc, tagged)

	coupons = {}
	for code in {row.posnext_coupon_code.upper() for row in tagged}:
		name = frappe.db.get_value("POS Coupon", {"coupon_code": code})
		if not name:
			frappe.throw(_("POS Coupon {0} does not exist").format(code))
		coupons[code] = frappe.get_cached_doc("POS Coupon", name)
	shares = set_coupon_shares(doc, tagged, coupons)

	spent = {code: 0.0 for code in coupons}
	rows_of = {code: [] for code in coupons}
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

		share, promotion = shares.get(id(row), (flt(row.discount_amount), 0.0))
		details = {"coupon": code, "row": row.idx, "share": share, "promotion": promotion}
		if coupon.discount_type == "Percentage":
			# A stacked line: the coupon's % of what the promotion leaves.
			percentage = (
				flt(share / (flt(row.price_list_rate) - promotion) * 100, 2)
				if promotion > 0 and flt(row.price_list_rate) > promotion
				else flt(row.discount_percentage)
			)
			if percentage > flt(coupon.discount_percentage) + TOLERANCE:
				refuse(
					_("Row {0}: discount {1}% exceeds POS Coupon {2} ({3}%)").format(
						row.idx, percentage, code, flt(coupon.discount_percentage)
					),
					**details,
					limit=f"{flt(coupon.discount_percentage)}%",
				)
		spent[code] += share * flt(row.qty)
		rows_of[code].append(
			{"row": row.idx, "item": row.item_code, "qty": flt(row.qty), "share": share, "promotion": promotion}
		)

	for code, total in spent.items():
		coupon = coupons[code]
		cap = flt(coupon.max_amount) if flt(coupon.max_amount) else None
		if coupon.discount_type == "Amount":
			cap = min(cap, flt(coupon.discount_amount)) if cap else flt(coupon.discount_amount)
		# Percentage coupons without a cap are priced as a percentage, not an amount.
		if cap and total > cap + TOLERANCE:
			refuse(
				_("POS Coupon {0} discount {1} exceeds its limit of {2}").format(code, total, cap),
				coupon=code,
				rows=rows_of[code],
				limit=cap,
			)


def _refuser(doc):
	"""``refuse(message, **details)`` for a coupon over its limit: throws online; on an
	offline sync logs and keeps."""
	from posnext_promotions.promotions.ff_paid import OFFLINE_SYNC_FLAG

	flags = getattr(doc, "flags", None) or {}
	if not flags.get(OFFLINE_SYNC_FLAG):

		def refuse(message, **details):
			frappe.throw(message)

		return refuse

	reference = doc.get("name") or doc.get("offline_id") or "new"

	def keep(message, **details):
		frappe.log_error(
			title=OFFLINE_KEPT_LOG_TITLE,
			message=f"Invoice {reference}: {message}\n{json.dumps(details, default=str, sort_keys=True)}",
		)

	return keep


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
	# Matched case-insensitively whatever the DB collation (this app never imports pos_next).
	coupon = frappe.qb.DocType("POS Coupon")
	rows = (
		frappe.qb.from_(coupon)
		.select(coupon.coupon_code)
		.where(Upper(coupon.coupon_code) == codes.pop())
		.limit(1)
	).run()
	if rows:
		doc.coupon_code = rows[0][0]


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
