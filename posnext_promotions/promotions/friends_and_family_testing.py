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
	# db.set_value skips Customer.on_update, which forgets this request's eligibility.
	from posnext_promotions.promotions.friends_and_family_eligibility import clear_cache

	clear_cache()
	return email


FF_SETTINGS = "Friends and Family Settings"


@contextmanager
def ff_settings_guard():
	"""Snapshot the F&F Settings Single (fields + child rows) and restore it on exit.

	If the body raises, that exception is what propagates: a restore that fails
	meanwhile is written to the log file (not the DB: the transaction may be the
	reason the body failed) instead of replacing it.
	"""
	from posnext_promotions.promotions import ff_money

	snapshot = frappe.get_doc(FF_SETTINGS).as_dict(convert_dates_to_str=False)
	try:
		yield
	except BaseException:
		try:
			_restore_ff_settings(snapshot)
		except Exception:
			frappe.logger("posnext_promotions").exception("ff_settings_guard: restore failed")
		raise
	else:
		_restore_ff_settings(snapshot)
	finally:
		# A test may change Currency / System Settings precision inside the guard.
		ff_money.clear_cache()


def _restore_ff_settings(snapshot: dict) -> None:
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


INERT_RULE_ITEM = "_Test FF Inert Rule Item"


def _inert_rule_item() -> str:
	if not frappe.db.exists("Item", INERT_RULE_ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": INERT_RULE_ITEM,
				"item_name": INERT_RULE_ITEM,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}) or "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True)
	return INERT_RULE_ITEM


def ensure_named_pricing_rule(name: str, **values) -> str:
	"""An enabled, undated selling Pricing Rule called ``name`` (``values`` overwrite its fields).

	Signed F&F snapshots are only restored while their rules still apply, so tests
	that tag lines with a rule name need the rule. It is inert: it applies only to
	an item no test sells, and it is a Price rule, so no F&F blocking rule picks it up.
	"""
	if not frappe.db.exists("Pricing Rule", name):
		frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": name,
				"apply_on": "Item Code",
				"items": [{"item_code": _inert_rule_item()}],
				"selling": 1,
				"buying": 0,
				"price_or_product_discount": "Price",
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": 1,
			}
		).insert(ignore_permissions=True, set_name=name)
	frappe.db.set_value("Pricing Rule", name, {"disable": 0, "valid_from": None, "valid_upto": None, **values})
	frappe.clear_document_cache("Pricing Rule", name)
	return name
