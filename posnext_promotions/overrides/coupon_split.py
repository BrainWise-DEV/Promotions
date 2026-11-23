# Copyright (c) 2026, BrainWise and contributors
"""The coupon's split over an invoice's lines is the server's, never the client's (K1 review I-2).

The cart prices each coupon line from ``validate_coupon`` / ``calculate_coupon_discount``.
A modified client could still move a fixed coupon's money onto the lines it kept (F&F
on one line, the whole coupon on another), or keep F&F next to a coupon that won the
whole cart. So the invoice's rows go back to their state before the coupon and before
F&F, the one resolver (:func:`best_discount_coupon.decide_coupon_vs_fnf`) splits the
coupon again in the Settings' mode, and every tagged row must be one of its lines and
take at most its share. Under "Better of the two for the whole cart" a coupon that won
leaves no F&F row on the invoice.

A paid invoice (the POS took the money) is refused; on an offline sync each breach is
logged and the sale kept (``refuse``). An unpaid invoice is re-priced as before: a row
the coupon should not have loses the coupon (F&F then judges it), and F&F next to a
coupon that holds the cart comes off (``HOLDS_CART_FLAG``, read by the F&F step). An
unpaid row above its share follows Settings "On Cap Breach at Submit": "Allow and log"
re-prices it down to its share (the split can move between the cart and the save, as
F&F's caps can), "Block submit" refuses it.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from posnext_promotions.promotions.friends_and_family_apply import _claims_fnf

def _tolerance(qty: float) -> float:
	"""Slack on a line's coupon money: a cent, plus the per-unit rounding of the saved row
	(at most half a cent a unit). Judged on the line, never per unit (re-review R1-M1)."""
	return 0.01 + 0.005 * abs(qty)


# Error Log title of an unpaid coupon line "Allow and log" brought down to its share (R2-M1).
REPRICED_LOG_TITLE = "POS Coupon line re-priced to its share"
# doc.flags: the server's split gave this invoice's coupon the whole cart (no F&F on it).
HOLDS_CART_FLAG = "posnext_coupon_holds_cart"


def _pre_coupon_lines(doc, shares) -> list:
	"""The invoice's rows as cart lines without the coupon (line totals, cart basis).

	A tagged row keeps only the promotion the server found under it (``shares``: per
	unit, from ``coupon_share``); F&F rows keep their markers, so the resolver offers
	them without F&F (or with the discount F&F displaced, from its signed snapshot).
	"""
	lines = []
	for row in doc.get("items") or []:
		line = frappe._dict(row.as_dict() if callable(getattr(row, "as_dict", None)) else dict(row))
		qty = flt(line.get("qty"))
		price = flt(line.get("price_list_rate"))
		if id(row) in shares:
			promotion = shares[id(row)][1]
			line.update(
				{
					"posnext_coupon_code": None,
					"coupon_code": None,
					"discount_source": None,
					"discount_percentage": 0,
					"rate": price - promotion,
					"pricing_rules": line.get("pricing_rules") if promotion else "",
				}
			)
		line.discount_amount = max(price - flt(line.get("rate")), 0.0) * qty
		line.amount = flt(line.get("rate")) * qty
		lines.append(line)
	return lines


def _drop_coupon(row, promotion: float) -> None:
	"""An unpaid row back to its state before the coupon (the promotion under it kept)."""
	price = flt(row.get("price_list_rate"))
	for field, value in (
		("posnext_coupon_code", None),
		("posnext_coupon_discount", 0),
		("coupon_code", None),
		("discount_percentage", 0),
		("discount_amount", promotion),
		("rate", price - promotion),
	):
		if isinstance(row, dict):
			row[field] = value
		else:
			row.set(field, value)


def _precision(row) -> int:
	return row.precision("discount_amount") if callable(getattr(row, "precision", None)) else 2


def _reprice_to_share(row, promotion: float, share_per_unit: float) -> None:
	"""An unpaid row's coupon money brought down to the server's share (per unit)."""
	precision = _precision(row)
	price = flt(row.get("price_list_rate"))
	share_per_unit = flt(share_per_unit, precision)
	discount = flt(promotion + share_per_unit, precision)
	for field, value in (
		("discount_percentage", 0),
		("discount_amount", discount),
		("rate", flt(price - discount, precision)),
		("posnext_coupon_discount", share_per_unit),
		*((("posnext_coupon_share", share_per_unit),) if flt(row.get("posnext_coupon_share")) else ()),
	):
		if isinstance(row, dict):
			row[field] = value
		else:
			row.set(field, value)


def check_coupon_split(doc, tagged, coupons, shares, refuse) -> None:
	"""Hold the invoice's coupon lines to the server's own split (module docstring)."""
	from posnext_promotions.promotions import ff_config, ff_constants, ff_money, ff_paid
	from posnext_promotions.promotions.best_discount_coupon import decide_coupon_vs_fnf

	if cint(doc.get("is_return")) or len(coupons) != 1:
		return
	code, coupon = next(iter(coupons.items()))
	decision = decide_coupon_vs_fnf(
		{
			"items": _pre_coupon_lines(doc, shares),
			"posting_date": doc.get("posting_date"),
			"taxes": doc.get("taxes") or None,
			"doctype": doc.get("doctype"),
			"pos_profile": doc.get("pos_profile"),
		},
		coupon,
		customer=doc.get("customer"),
		pos_profile=doc.get("pos_profile"),
	)
	allowed = {u.get("line_key"): flt(u.get("coupon_only_discount")) for u in decision.kept}
	position = {id(row): key for key, row in enumerate(doc.get("items") or [])}
	paid = ff_paid.is_paid(doc)
	reprice = not paid and (
		ff_config.resolve().on_cap_breach_at_submit.value != ff_constants.CAP_BREACH_POLICIES[1]
	)
	kept = 0
	for row in tagged:
		share, promotion = shares.get(id(row), (flt(row.get("discount_amount")), 0.0))
		limit = allowed.get(position.get(id(row)))
		qty = flt(row.get("qty")) or 1
		if limit is None and not paid:
			_drop_coupon(row, promotion)
			continue
		kept += 1
		if limit is None:
			refuse(
				_("Row {0}: coupon {1} does not give the best discount on item {2}").format(
					row.idx, code, row.item_code
				),
				coupon=code,
				row=row.idx,
				share=share,
			)
		elif share * qty > limit + _tolerance(qty):
			if reprice:
				# "Allow and log": the correction is logged, as the offline-kept checks are.
				details = {
					"invoice": doc.get("name") or doc.get("offline_id") or "new",
					"row": row.idx,
					"item": row.item_code,
					"coupon": code,
					"client_share": flt(share * qty, _precision(row)),
					"server_share": flt(limit, _precision(row)),
				}
				frappe.log_error(
					title=REPRICED_LOG_TITLE,
					message=(
						f"Invoice {details['invoice']}: row {row.idx} ({row.item_code}) had coupon {code} "
						f"giving {details['client_share']}, its share is {details['server_share']}\n"
						+ json.dumps(details, default=str, sort_keys=True)
					),
				)
				_reprice_to_share(row, promotion, limit / qty)
				continue
			refuse(
				_("Row {0}: coupon {1} gives {2} on item {3}, more than its share of {4}").format(
					row.idx, code, ff_money.fmt(share * qty), row.item_code, ff_money.fmt(limit)
				),
				coupon=code,
				row=row.idx,
				share=share,
				limit=limit / qty,
			)
	flags = getattr(doc, "flags", None)
	if flags is not None:
		flags[HOLDS_CART_FLAG] = bool(decision.replaces_fnf_on_cart and kept)
	if decision.replaces_fnf_on_cart and paid:
		for row in doc.get("items") or []:
			if _claims_fnf(row) and not row.get("posnext_coupon_code"):
				refuse(
					_("Row {0}: the Friends & Family discount cannot be combined with coupon {1} on this invoice").format(
						row.idx, code
					),
					coupon=code,
					row=row.idx,
				)
