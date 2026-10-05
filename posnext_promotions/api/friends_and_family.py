# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family program config for the POS client."""

import frappe

from posnext_promotions.promotions.friends_and_family import get_active_program


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
	return program.to_client_payload()
