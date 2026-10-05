# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family program config and auto-apply for the POS client."""

from __future__ import annotations

import json

import frappe
from frappe.utils import cstr

from posnext_promotions.promotions.friends_and_family import get_active_program
from posnext_promotions.promotions.friends_and_family_apply import (
	apply_friends_and_family_to_items,
	compute_friends_and_family_discounts,
)
from posnext_promotions.promotions.friends_and_family_eligibility import (
	get_customer_eligibility,
	get_eligibility_rules_payload,
)


def _parse_items(items):
	if items is None:
		return []
	if isinstance(items, str):
		items = json.loads(items or "[]")
	if isinstance(items, dict) and "items" in items:
		# Allow passing a full invoice-like payload.
		items = items.get("items") or []
	return list(items or [])


@frappe.whitelist()
def get_friends_and_family_program(posting_date: str | None = None) -> dict:
	"""The live program for ``posting_date`` (today when omitted).

	Returns ``{"active": False}`` when the program is disabled, outside its
	dates, or has nothing in scope. Clients should re-fetch rather than cache
	indefinitely so admin changes take effect on the next recalculation.
	"""
	program = get_active_program(posting_date)
	if program is None:
		return {"active": False}
	payload = program.to_client_payload()
	payload["eligibility"] = get_eligibility_rules_payload()
	return payload


@frappe.whitelist()
def check_friends_and_family_eligibility(customer: str | None = None, posting_date: str | None = None) -> dict:
	"""Call when the cashier sets the invoice customer.

	Returns ``eligible``, a machine ``reason`` (``customer_group``,
	``customer_flag``, ``not_eligible``, ``program_inactive``, ``no_customer``,
	``customer_not_found``, ``customer_disabled``), a cashier-facing ``message``,
	and — only when eligible — ``discount_percentage`` and the ``program``.

	Informational only: server-side pricing re-checks through
	``friends_and_family_eligibility.get_program_for_customer`` /
	``friends_and_family_apply.apply_friends_and_family_to_items``, so a client
	cannot grant F&F by ignoring this answer.
	"""
	customer = cstr(customer).strip()
	if customer and frappe.db.exists("Customer", customer):
		frappe.has_permission("Customer", "read", doc=customer, throw=True)
	return get_customer_eligibility(customer, posting_date).to_client_payload()


@frappe.whitelist()
def apply_friends_and_family_discount(
	customer: str | None = None,
	items=None,
	posting_date: str | None = None,
	mutate: int | str | None = 1,
) -> dict:
	"""Compute (and optionally stamp) Friends & Family line discounts.

	Call from the POS when the customer is set or cart lines change. The
	percentage always comes from Friends and Family Settings — any
	``discount_percentage`` on the request payload is ignored.

	Args:
	        customer: Invoice customer.
	        items: Cart lines (list) or JSON string. Each line should carry
	                ``item_code``, ``qty``, ``price_list_rate`` (or ``rate``), and
	                optionally ``brand`` / ``item_group`` / ``discount_percentage`` /
	                ``discount_source``. Catalog fields are backfilled from Item.
	        posting_date: Invoice posting date (defaults to today). After the
	                program end date, auto-apply stops immediately.
	        mutate: When truthy (default), return lines with F&F stamped where it
	                wins. When falsy, return a read-only decision without mutating.

	Returns:
	        ``eligible``, ``discount_percentage`` (Settings rate or 0), ``message``,
	        ``applied_count``, and per-line ``items`` with ``applies``,
	        ``discount_percentage``, ``discount_amount``, ``rate``, ``reason``.

	POS contract (frontend follow-up in ``pos_next``):
	  1. On customer change → ``check_friends_and_family_eligibility``.
	  2. On item add / qty change / customer change → this method (or rely on
	     ``apply_offers``, which also runs the same engine).
	  3. Never send a cashier-editable F&F percentage; the server overwrites it.
	"""
	customer = cstr(customer).strip() or None
	if customer and frappe.db.exists("Customer", customer):
		frappe.has_permission("Customer", "read", doc=customer, throw=True)

	parsed = _parse_items(items)
	# Normalise to mutable dicts so apply can stamp fields even when the caller
	# passed plain mappings.
	lines = [frappe._dict(row) if not isinstance(row, frappe._dict) else row for row in parsed]

	from frappe.utils import cint

	if cint(mutate):
		return apply_friends_and_family_to_items(lines, customer=customer, posting_date=posting_date)

	return compute_friends_and_family_discounts(customer=customer, items=lines, posting_date=posting_date)
