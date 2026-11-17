# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Paid invoices: the customer has paid, so Friends & Family never re-prices them.

An invoice is *paid* when it was rung up offline and synced later (``offline_id``
set, or the POS app's ``flags.pos_offline_sync``) or when the POS app saved it with
the payments it collected (``flags.pos_payment_collected``). Promotions reads these
flags only; it never imports the POS app.

A Desk invoice with prefilled payments is **not** paid in this sense: ERPNext does
not fill payments on validate, and Desk users expect F&F to price the invoice.
"""

from __future__ import annotations

OFFLINE_ID_FIELD = "offline_id"
OFFLINE_SYNC_FLAG = "pos_offline_sync"
PAYMENT_COLLECTED_FLAG = "pos_payment_collected"

# ``doc.flags`` key: disagreements found by validate, written to the log at submit.
DISAGREEMENTS_FLAG = "ff_paid_disagreements"

# Disagreement reasons (Friends and Family Log ``reason`` and ``details.reason``).
NOT_ELIGIBLE = "not_eligible"
PROGRAM_INACTIVE = "program_inactive"
COMPATIBILITY = "compatibility"
CAP_EXCEEDED = "cap_exceeded"
MISSING_FNF = "missing_fnf"
OVER_DISCOUNT = "over_discount"
HEADER_DISCOUNT = "header_discount"
UNDER_DISCOUNT = "paid_under_discount"

# Online, the cashier can still fix the cart: these stop the save.
ONLINE_BLOCKING = frozenset({OVER_DISCOUNT, COMPATIBILITY, HEADER_DISCOUNT})

# Online the cashier needs one answer for "the customer may not have this F&F".
_ONLINE_REASONS = {NOT_ELIGIBLE: OVER_DISCOUNT, PROGRAM_INACTIVE: OVER_DISCOUNT, MISSING_FNF: UNDER_DISCOUNT}


def _flag(doc, name: str) -> bool:
	flags = getattr(doc, "flags", None)
	return bool(flags.get(name)) if flags is not None else False


def is_offline(doc) -> bool:
	"""Rung up offline and synced now: the customer has left."""
	return bool(doc.get(OFFLINE_ID_FIELD)) or _flag(doc, OFFLINE_SYNC_FLAG)


def is_paid(doc) -> bool:
	"""Offline-synced, or saved by the POS with the payments it collected."""
	return is_offline(doc) or _flag(doc, PAYMENT_COLLECTED_FLAG)


def online_reason(reason: str) -> str:
	return _ONLINE_REASONS.get(reason, reason)
