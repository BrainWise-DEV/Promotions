# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Fixtures shared by the Friends & Family tests and demo data.

Approved Domains + a verified customer email are mandatory for F&F, so every
fixture that enables the program lists an approved domain and gives its
customers a verified email on it.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import frappe
from frappe.utils import now_datetime

TEST_EMAIL_DOMAIN = "fftest.example"


def approved_domain_rows(domain: str = TEST_EMAIL_DOMAIN, company: str = "F&F Tests") -> list[dict]:
	return [{"email_domain": domain, "company": company, "enabled": 1}]


def email_for(customer: str, domain: str = TEST_EMAIL_DOMAIN) -> str:
	local = re.sub(r"[^a-z0-9]+", ".", (customer or "").lower()).strip(".") or "customer"
	return f"{local}@{domain}"


def give_verified_email(customer: str, domain: str = TEST_EMAIL_DOMAIN, email: str | None = None) -> str:
	"""Set ``email_id`` on ``domain`` and mark it OTP-verified (when POSNext's fields exist).

	Uses ``db.set_value`` because POSNext's Customer.validate protects the verified fields.
	"""
	email = email or email_for(customer, domain)
	values = {"email_id": email}
	meta = frappe.get_meta("Customer")
	if meta.has_field("custom_email_verified") and meta.has_field("custom_verified_email"):
		values.update({"custom_email_verified": 1, "custom_verified_email": email})
		if meta.has_field("custom_email_verified_on"):
			values["custom_email_verified_on"] = now_datetime()
	frappe.db.set_value("Customer", customer, values, update_modified=False)
	return email


FF_SETTINGS = "Friends and Family Settings"


@contextmanager
def ff_settings_guard():
	"""Snapshot the F&F Settings Single (fields + child rows) and restore it on exit."""
	snapshot = frappe.get_doc(FF_SETTINGS).as_dict(convert_dates_to_str=False)
	try:
		yield
	finally:
		doc = frappe.get_doc(FF_SETTINGS)
		for df in doc.meta.fields:
			if df.fieldtype in ("Table", "Table MultiSelect"):
				doc.set(df.fieldname, [])
				for row in snapshot.get(df.fieldname) or []:
					doc.append(df.fieldname, {k: v for k, v in row.items() if k not in ("name", "idx", "parent", "parentfield", "parenttype", "creation", "modified")})
			elif df.fieldtype not in frappe.model.no_value_fields:
				doc.set(df.fieldname, snapshot.get(df.fieldname))
		doc.flags.ignore_permissions = True
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)
