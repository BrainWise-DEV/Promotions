# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Paid invoices: the customer has paid, so Friends & Family never re-prices them.

An invoice is *paid* when it was rung up offline and synced later (``offline_id``
set, or the POS app's ``flags.pos_offline_sync``) or when the POS app saved it with
the payments it collected (``flags.pos_payment_collected``). When the POS app re-saves a
stored draft that already carried a payment and a header, keeping a header no larger than
the stored one (as an amount and as a share of the total before it), it also sets
``flags.pos_paid_draft``: that invoice is judged as paid online, except that its header
discount is logged instead of thrown. The stored header is the bound; any other save has
no flag. Promotions reads these flags only; it never imports the POS app.

A Desk invoice with prefilled payments is **not** paid in this sense: ERPNext does
not fill payments on validate, and Desk users expect F&F to price the invoice.
"""

from __future__ import annotations

OFFLINE_ID_FIELD = "offline_id"
OFFLINE_SYNC_FLAG = "pos_offline_sync"
PAYMENT_COLLECTED_FLAG = "pos_payment_collected"
# Set by the POS app when it re-saves a stored paid draft keeping (no more than) the header
# it held; that header is then logged, not thrown.
PAID_DRAFT_FLAG = "pos_paid_draft"

# ``doc.flags`` key: disagreements found by validate, written to the log at submit.
DISAGREEMENTS_FLAG = "ff_paid_disagreements"
# ``doc.flags`` key: the server's capped F&F computation for a paid invoice; the submit log
# takes its caps (capped, cap_hit, uncapped amount, Cap Hit rows) from it (D-R2-02).
PLAN_FLAG = "ff_paid_plan"

# Disagreement reasons (Friends and Family Log ``reason`` and ``details.reason``).
NOT_ELIGIBLE = "not_eligible"
PROGRAM_INACTIVE = "program_inactive"
COMPATIBILITY = "compatibility"
CAP_EXCEEDED = "cap_exceeded"
MISSING_FNF = "missing_fnf"
OVER_DISCOUNT = "over_discount"
HEADER_DISCOUNT = "header_discount"
UNDER_DISCOUNT = "paid_under_discount"
# A gift the gift policy forbids next to F&F: logged only (a paid invoice is never changed).
GIFT_WITH_FNF = "gift_with_fnf"

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


def is_paid_draft(doc) -> bool:
	"""A stored paid draft the POS app re-saves: its header disagreement is logged, never thrown."""
	return _flag(doc, PAID_DRAFT_FLAG)


def online_reason(reason: str) -> str:
	return _ONLINE_REASONS.get(reason, reason)
