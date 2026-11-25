# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family never applies to Sales Orders or Quotations (Phase 1).

No hook checks and no cap counts F&F on these documents, so the server strips it
until full Sales Order support exists. The POS strips it first; this is the backstop
for Desk, API and modified clients. Strip and log, never throw: a Desk user is not blocked.

Sales Order Item / Quotation Item have no ``posnext_*`` columns, so a line is read from
the in-memory payload keys.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt

from posnext_promotions.promotions import friends_and_family_log as ff_log
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	FIELD_FNF_PERCENTAGE,
)

FNF = "friends_and_family"
# Documents F&F never applies to (their validate strips it, see hooks.py).
NON_INVOICE_DOCTYPES = frozenset({"Sales Order", "Quotation"})
_LINE_KEYS = (FIELD_DISCOUNT_SOURCE, FIELD_FNF_AMOUNT, FIELD_FNF_PERCENTAGE)
_HEADER_PREFIX = "posnext_fnf_"


def ff_applies_to(doctype) -> bool:
	"""False for a Sales Order / Quotation: there F&F is never a rival discount."""
	return cstr(doctype) not in NON_INVOICE_DOCTYPES


def _is_fnf_line(item) -> bool:
	return (
		cstr(item.get(FIELD_DISCOUNT_SOURCE)) == FNF
		or flt(item.get(FIELD_FNF_PERCENTAGE)) > 0
		or flt(item.get(FIELD_FNF_AMOUNT)) > 0
	)


def _strip_line(item) -> None:
	item.discount_percentage = 0
	item.discount_amount = 0
	# No list price means nothing to restore; keep the rate rather than make the line free.
	if flt(item.get("price_list_rate")):
		item.rate = flt(item.price_list_rate, item.precision("rate"))
	for key in _LINE_KEYS:
		vars(item).pop(key, None)


def strip_ff(doc, method=None) -> int:
	"""Remove F&F from a Sales Order / Quotation. Returns the number of lines stripped."""
	stripped = 0
	for item in doc.get("items") or []:
		if _is_fnf_line(item):
			_strip_line(item)
			stripped += 1
	for key in [k for k in vars(doc) if k.startswith(_HEADER_PREFIX)]:
		vars(doc).pop(key)
	if stripped:
		frappe.msgprint(
			_("Friends & Family discounts apply to invoices only; removed from {0} lines.").format(stripped)
		)
		ff_log.log_non_invoice_stripped(doc, stripped)
		# doc_events run after the controller's validate, which already totalled the discounted rows.
		recalc = getattr(doc, "calculate_taxes_and_totals", None)
		if callable(recalc):
			recalc()
	return stripped
