# Copyright (c) 2026, BrainWise and contributors
"""The coupon-only part of a coupon line's discount (``posnext_coupon_share``, G5).

An "Add on top" POS Coupon stacks on the promotion a line already has, so the row's
``discount_amount`` holds promotion + coupon. Only the coupon's part may count against
the coupon's ``max_amount`` / fixed amount. The server works that part out on every
validate from its own data, never from the client:

- a plain coupon line (any other ``promotion_conflict``, or nothing under it): the whole
  line discount, as before;
- a stacked line: the line discount less the promotion under it, read from the row's
  server-signed ``posnext_fnf_replaced_discount`` (the promotion F&F displaced on the
  cart) when it verifies, else re-derived by evaluating the offers on the server's list
  prices. A snapshot that does not verify (forged, altered, expired, or signed for
  other line values) is dropped, never used. A restored discount that is a coupon
  (this one or another) is no promotion (re-review M-A, G5 review m-4);
- a return row: its original row's share, per unit, so the return counts the same
  coupon money back (the field is copied, but a client-sent value is replaced).

Every other row gets 0. The share is per unit, as ``discount_amount`` is on a saved row.
"""

import json

import frappe
from frappe.utils import cint, cstr, flt

from posnext_promotions.api.coupon_engine import (
	PROMOTION_CONFLICT_ADD,
	_line_existing_discount,
	get_promotion_conflict_mode,
)
from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions.best_discount_coupon import _holds_coupon
from posnext_promotions.promotions import ff_snapshot
from posnext_promotions.promotions.friends_and_family_caps import FIELD_FNF_REPLACED

FIELD_SHARE = "posnext_coupon_share"
ITEM_DOCTYPE = {"Sales Invoice": "Sales Invoice Item"}
RETURN_LINK = {"Sales Invoice": "sales_invoice_item"}
# Fields of a replaced-discount snapshot that describe the discount it holds.
_SNAPSHOT_FIELDS = (
	"discount_source",
	"discount_percentage",
	"discount_amount",
	"rate",
	"pricing_rules",
	"posnext_coupon_code",
	"coupon_code",
	"is_already_discounted",
)


def stores_share(doc) -> bool:
	item_doctype = ITEM_DOCTYPE.get(cstr(doc.get("doctype")))
	return bool(item_doctype) and frappe.get_meta(item_doctype).has_field(FIELD_SHARE)


def set_coupon_shares(doc, tagged, coupons) -> dict:
	"""Write the server's share on every row; ``{id(row): (share, promotion)}`` per unit
	for the ``tagged`` (coupon-discounted, paid) rows. ``coupons``: code → POS Coupon."""
	stored = stores_share(doc)
	if stored:
		for row in doc.get("items") or []:
			if flt(row.get(FIELD_SHARE)):
				row.set(FIELD_SHARE, 0)  # never the client's
		if cint(doc.get("is_return")):
			_copy_return_shares(doc)
	if cint(doc.get("is_return")) or not tagged:
		return {}

	stacked = [
		row
		for row in tagged
		if get_promotion_conflict_mode(coupons[cstr(row.posnext_coupon_code).upper()]) == PROMOTION_CONFLICT_ADD
	]
	promotions = _promotions_under(doc, stacked) if stacked else {}
	shares = {}
	for row in tagged:
		whole = max(flt(row.get("discount_amount")), 0.0)
		promotion = min(max(flt(promotions.get(id(row))), 0.0), whole)
		share = whole - promotion
		shares[id(row)] = (share, promotion)
		if stored:
			row.set(FIELD_SHARE, flt(share, row.precision(FIELD_SHARE) if hasattr(row, "precision") else 6))
	return shares


def _copy_return_shares(doc) -> None:
	link = RETURN_LINK.get(cstr(doc.get("doctype")))
	rows = [row for row in doc.get("items") or [] if link and row.get(link)]
	if not rows:
		return
	originals = dict(
		frappe.get_all(
			ITEM_DOCTYPE[doc.doctype],
			filters={"name": ["in", list({row.get(link) for row in rows})]},
			fields=["name", FIELD_SHARE],
			as_list=True,
		)
	)
	for row in rows:
		row.set(FIELD_SHARE, flt(originals.get(row.get(link))))


def _promotions_under(doc, rows) -> dict:
	"""``{id(row): promotion discount per unit}`` under each stacked coupon row."""
	found, missing = {}, []
	for row in rows:
		promotion = _snapshot_promotion(doc, row)
		if promotion is None:
			missing.append(row)
		else:
			found[id(row)] = promotion
	if missing:
		found.update(_rederived_promotions(doc, missing))
	return found


def _snapshot_promotion(doc, row) -> float | None:
	"""The promotion per unit the row's signed snapshot holds; None without a valid one."""
	raw = row.get(FIELD_FNF_REPLACED)
	if not raw:
		return None
	data = ff_snapshot.verify(row, raw, doc=doc)
	if data is None or not ff_snapshot.rules_apply_to(data, doc):
		# Refused: a seal that does not verify for this row proves nothing.
		row.set(FIELD_FNF_REPLACED, None)
		return None
	view = frappe._dict(
		item_code=row.item_code,
		qty=flt(row.qty),
		price_list_rate=flt(row.price_list_rate),
		**{key: data[key] for key in _SNAPSHOT_FIELDS if key in data},
	)
	return _promotion_per_unit(view, row.posnext_coupon_code)


def _promotion_per_unit(view, coupon_code) -> float:
	"""A line view's own discount per unit (cart basis), as the coupon engine reads the
	discount an "Add on top" coupon stacks on.

	A Min/Max ("buy one") line comes back materialised the ERPNext way
	(``pricing_rule._materialize_rate``): ``rate`` below ``price_list_rate`` and a PER-UNIT
	``discount_amount`` equal to their gap. That gap is the promotion per unit; reading the
	field as a line total divided it by qty a second time (D-R4-03: 8.33 -> 1.39 on 6 x 50,
	so a 10% coupon looked like 22.84%). Every other evaluated line keeps the line-total
	basis (Accumulative included).
	"""
	if _holds_coupon(view):
		return 0.0  # a coupon (this one or another) is no promotion to stack on (M-A, m-4)
	qty = bd.line_qty(view)
	if not qty:
		return 0.0
	per_unit = _materialised_per_unit(view)
	if per_unit is not None:
		return per_unit
	return _line_existing_discount(view, "line") / qty


def _materialised_per_unit(view) -> float | None:
	"""The per-unit discount of a line priced per unit (rate set below the list price and
	``discount_amount`` their gap, to the cent), else None."""
	price, rate = flt(view.get("price_list_rate")), flt(view.get("rate"))
	if not (price > rate > 0):
		return None
	gap = price - rate
	return gap if abs(gap - flt(view.get("discount_amount"))) <= 0.011 else None


def _rederived_promotions(doc, rows) -> dict:
	"""The promotion per unit on ``rows``, from the offers evaluated on the server's data.

	The invoice's paid rows go in at their list price with no discount (the rows'
	own ``pricing_rules`` are not trusted), on the invoice's posting date. For an F&F
	customer the evaluation stamps F&F on the line, with its own freshly signed snapshot
	of the promotion it displaced: that promotion is read back from it.
	"""
	from posnext_promotions.api.apply_offers import apply_offers
	from posnext_promotions.promotions.friends_and_family_apply import (
		_is_fnf_stamped,
		_restored_copy,
		restore_context,
	)

	paid = [
		row
		for row in doc.get("items") or []
		if row.get("item_code") and not cint(row.get("is_free_item")) and flt(row.get("qty")) > 0
	]
	payload = {
		"doctype": doc.get("doctype"),
		"pos_profile": doc.get("pos_profile"),
		"customer": doc.get("customer"),
		"company": doc.get("company"),
		"posting_date": cstr(doc.get("posting_date")) or None,
		"posting_time": cstr(doc.get("posting_time")) or None,
		"selling_price_list": doc.get("selling_price_list"),
		"currency": doc.get("currency"),
		"coupon_code": "",
		"items": [
			{
				"item_code": row.item_code,
				"item_name": row.get("item_name"),
				"qty": flt(row.qty),
				"uom": row.get("uom"),
				"conversion_factor": flt(row.get("conversion_factor")) or 1,
				"warehouse": row.get("warehouse"),
				"price_list_rate": flt(row.price_list_rate),
				"rate": flt(row.price_list_rate),
				"amount": flt(row.price_list_rate) * flt(row.qty),
				"discount_percentage": 0,
				"discount_amount": 0,
				"discount_source": "",
				"pricing_rules": "",
				"is_free_item": 0,
			}
			for row in paid
		],
	}
	from posnext_promotions.api.one_time_usage import judging_invoice

	message_count = len(frappe.local.message_log or [])
	try:
		# This invoice's own one-time redemption is not an earlier one (D-R3-04 b).
		with judging_invoice(doc.get("name")):
			result = apply_offers(json.loads(json.dumps(payload, default=str)))
	except Exception:
		# No evaluation, no promotion: the stacked rows count their whole discount.
		del frappe.local.message_log[message_count:]
		frappe.log_error(frappe.get_traceback(), "POS Coupon share: offers not evaluated")
		return {}
	lines = (result or {}).get("items") or []
	ctx = restore_context(doc.get("customer"), payload["posting_date"], doc.get("pos_profile"))
	wanted = {id(row): row for row in rows}
	found = {}
	for row, line in zip(paid, lines, strict=False):
		if id(row) not in wanted:
			continue
		view = frappe._dict(
			{"item_code": row.item_code, "qty": flt(row.qty), "price_list_rate": flt(row.price_list_rate), **line}
		)
		if _is_fnf_stamped(view):
			view = _restored_copy(view, ctx)
		found[id(row)] = 0.0 if _is_fnf_stamped(view) else _promotion_per_unit(view, row.posnext_coupon_code)
	return found
