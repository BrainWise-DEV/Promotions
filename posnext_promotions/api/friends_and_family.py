# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family program config for the POS client."""

import frappe
from frappe.utils import cstr

from posnext_promotions.promotions.friends_and_family import get_active_program
from posnext_promotions.promotions.friends_and_family_eligibility import (
	get_customer_eligibility,
	get_eligibility_rules_payload,
)


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

	Informational only: server-side pricing must re-check through
	``friends_and_family_eligibility.get_program_for_customer`` (PN-126), so a
	client cannot grant F&F by ignoring this answer.
	"""
	customer = cstr(customer).strip()
	if customer and frappe.db.exists("Customer", customer):
		frappe.has_permission("Customer", "read", doc=customer, throw=True)
	return get_customer_eligibility(customer, posting_date).to_client_payload()
