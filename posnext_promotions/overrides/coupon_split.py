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

def _tolerance(qty: float, stacked: bool = False) -> float:
	"""Slack on a line's coupon money: a cent, plus the per-unit rounding of the saved row
	(at most half a cent a unit). Judged on the line, never per unit (re-review R1-M1).

	A stacked line's share is its rate to the cent less the promotion, which the cart also
	holds to the cent: half a cent a unit more (re-review R2-m1, 8 of 101 real carts at qty
	5-24 were refused without it)."""
	return 0.01 + (0.01 if stacked else 0.005) * abs(qty)


# Error Log title of an unpaid coupon line "Allow and log" brought down to its share (R2-M1).
REPRICED_LOG_TITLE = "POS Coupon line re-priced to its share"
# Error Log title of a paid whole-cart sale that left a line of the coupon without it (review M4).
UNDER_APPLIED_LOG_TITLE = "POS Coupon line sold without its share"
# doc.flags: what that log will say, found at validate and written once, at submit (R1-M3).
UNDER_APPLIED_FLAG = "posnext_coupon_under_applied"
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


def _check_split_total(doc, code, held, reprice, refuse) -> None:
	"""The coupon money on the rows it holds is at most the server's split of it (review I1).

	Each row may carry the cents the cut loses (the cart may have put them on another line),
	so the rows together are held to the split before the cut, with the rows' own rounding:
	the cart's cut only floors and hands back what it lost, so its rows never add up to more
	than its own uncut split, which differs from this one by far less than a cent (R1-M1).
	Over it: "Allow and log" brings an unpaid invoice's rows back to the server's split and
	logs it; otherwise it is refused.
	"""
	from posnext_promotions.promotions import ff_money

	units = sum(qty for *_rest, qty in held)
	stacked_units = sum(qty for _row, _share, promotion, _limit, _exact, qty in held if promotion > 0)
	claimed = sum(share * qty for _row, share, _promotion, _limit, _exact, qty in held)
	limited = sum(exact for *_rest, exact, _qty in held)
	if not held or claimed <= limited + _tolerance(units) + 0.005 * stacked_units:
		return
	if reprice:
		frappe.log_error(
			title=REPRICED_LOG_TITLE,
			message=(
				f"Invoice {doc.get('name') or doc.get('offline_id') or 'new'}: coupon {code} gave "
				f"{flt(claimed, 2)} on its rows, the server's split is {flt(limited, 2)}\n"
				+ json.dumps({"coupon": code, "client_total": claimed, "server_total": limited}, default=str)
			),
		)
		for row, _share, promotion, limit, _exact, qty in held:
			_reprice_to_share(row, promotion, limit / qty)
		return
	refuse(
		_("Coupon {0} gives {1} on this invoice, more than its split of {2}").format(
			code, ff_money.fmt(claimed), ff_money.fmt(limited)
		),
		coupon=code,
		share=claimed,
		limit=limited,
	)


def _under_applied(doc, code, tagged, allowed, position) -> dict | None:
	"""A paid sale where the coupon held the whole cart but some of its lines went out
	without it (added after the coupon, or offline): kept as paid, logged (review M4).

	The customer is owed no F&F on this invoice, so the F&F audit says nothing here.
	Returns the log's details, for :func:`log_under_applied` at submit; None when none.
	"""
	held = {position.get(id(row)) for row in tagged}
	items = doc.get("items") or []
	missing = [
		{"row": key + 1, "item": items[key].get("item_code"), "server_share": flt(allowed[key], 2)}
		for key in sorted(k for k in allowed if k not in held and k is not None and k < len(items))
	]
	if not missing:
		return None
	return {"invoice": doc.get("name") or doc.get("offline_id") or "new", "coupon": code, "rows": missing}


def log_under_applied(doc, method=None) -> None:
	"""Sales Invoice ``on_submit``: the under-applied coupon found at validate, logged once a
	sale. Validate runs on every save of a paid draft and again on submit (review R1-M3)."""
	flags = getattr(doc, "flags", None) or {}
	details = flags.get(UNDER_APPLIED_FLAG)
	if not details:
		return
	frappe.log_error(
		title=UNDER_APPLIED_LOG_TITLE,
		message=(
			f"Invoice {details['invoice']}: coupon {details['coupon']} holds the cart but "
			+ ", ".join(f"row {m['row']} ({m['item']})" for m in details["rows"])
			+ " went out without its share\n"
			+ json.dumps(details, default=str, sort_keys=True)
		),
	)


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
	exact = {
		u.get("line_key"): flt(u.get("coupon_exact_share", u.get("coupon_only_discount"))) for u in decision.kept
	}
	# The cents the whole-cents cut loses land on a line chosen by sub-cent figures, which the
	# cart and this re-split see differently (the cart's promotion is to the cent): any one
	# line may carry them, and the rows together are held to the split (review I1).
	slack = max((flt(u.get("whole_cents_slack")) for u in decision.kept), default=0.0)
	position = {id(row): key for key, row in enumerate(doc.get("items") or [])}
	paid = ff_paid.is_paid(doc)
	reprice = not paid and (
		ff_config.resolve().on_cap_breach_at_submit.value != ff_constants.CAP_BREACH_POLICIES[1]
	)
	kept = 0
	held = []  # (row, share, promotion, limit, exact share, qty) of the rows the split gives the coupon
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
		elif share * qty <= limit + slack + _tolerance(qty, stacked=promotion > 0):
			held.append((row, share, promotion, limit, exact[position.get(id(row))], qty))
		else:
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
	_check_split_total(doc, code, held, reprice, refuse)
	flags = getattr(doc, "flags", None)
	if flags is not None:
		flags[HOLDS_CART_FLAG] = bool(decision.replaces_fnf_on_cart and kept)
		flags[UNDER_APPLIED_FLAG] = (
			_under_applied(doc, code, tagged, allowed, position)
			if paid and decision.replaces_fnf_on_cart
			else None
		)
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
