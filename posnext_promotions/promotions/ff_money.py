# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Money for the Friends & Family engine: precision, rounding and formatting per currency.

Precision is whatever ERPNext uses for an invoice line amount in that currency
(``get_field_precision`` on Sales Invoice Item ``discount_amount``), so the
engine rounds exactly as the saved invoice does. It follows the site's System
Settings: a global Currency Precision wins; otherwise the currency's own
number format counts only when "Use Number Format from Currency" is on.
Rounding uses ``flt`` (the site's rounding method), as ERPNext does.

``None`` as the currency means the engine's current currency: the invoice's,
or the cart's (its own, else its POS Profile's, else its company's), set by the
entry points with :func:`currency_context`. Outside any context it falls back
to the default company's currency and writes one Error Log per request, so a
caller that forgot to set the currency shows up.
"""

from __future__ import annotations

import functools
import inspect
import json
import math
from contextlib import contextmanager

import frappe
from frappe.utils import flt, fmt_money

_PRECISION_CACHE = "ff_money_precision"
_CURRENT = "ff_money_currency"
_FALLBACK_LOGGED = "ff_money_fallback_logged"
FALLBACK_LOG_TITLE = "Friends & Family money: no currency"
FALLBACK_LOG_MESSAGE = (
	"Friends & Family priced an amount without a currency (no invoice, cart, POS Profile or company); "
	"the default company's currency was used."
)
# Float noise allowed before floor / ceil moves to the next unit (e.g. 1.23 * 100 = 122.99999...).
_EPSILON = 1e-6


def company_currency(company: str | None) -> str | None:
	return frappe.get_cached_value("Company", company, "default_currency") if company else None


def default_currency() -> str | None:
	"""The default company's currency, else the site's default currency."""
	company = frappe.defaults.get_global_default("company")
	return company_currency(company) or frappe.db.get_default("currency")


def fallback_currency() -> str | None:
	"""The default company's currency, for when nothing else resolved; logged once per request."""
	if not getattr(frappe.local, _FALLBACK_LOGGED, False):
		setattr(frappe.local, _FALLBACK_LOGGED, True)
		frappe.log_error(title=FALLBACK_LOG_TITLE, message=FALLBACK_LOG_MESSAGE)
	return default_currency()


def resolve(currency: str | None = None) -> str | None:
	"""``currency``, else the current context's, else the (logged) default."""
	return currency or current_currency() or fallback_currency()


def precision_for(currency: str | None = None) -> int:
	"""Decimal places ERPNext keeps for an invoice amount in ``currency`` (cached per request)."""
	currency = resolve(currency)
	cache = getattr(frappe.local, _PRECISION_CACHE, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, _PRECISION_CACHE, cache)
	if currency not in cache:
		from frappe.model.meta import get_field_precision

		field = frappe.get_meta("Sales Invoice Item").get_field("discount_amount")
		cache[currency] = get_field_precision(field, currency=currency)
	return cache[currency]


def clear_cache() -> None:
	"""Forget cached precisions (after a Currency or System Settings change in the same request)."""
	if hasattr(frappe.local, _PRECISION_CACHE):
		delattr(frappe.local, _PRECISION_CACHE)


def money(value, currency: str | None = None) -> float:
	return flt(value, precision_for(currency))


def floor_money(value, currency: str | None = None) -> float:
	"""``value`` rounded down to the currency's smallest unit."""
	scale = 10 ** precision_for(currency)
	return math.floor(flt(value) * scale + _EPSILON) / scale


def ceil_money(value, currency: str | None = None) -> float:
	"""``value`` rounded up to the currency's smallest unit."""
	scale = 10 ** precision_for(currency)
	return math.ceil(flt(value) * scale - _EPSILON) / scale


def fmt(value, currency: str | None = None) -> str:
	"""``value`` as the cashier reads it: the currency's symbol and precision."""
	currency = resolve(currency)
	return fmt_money(value, precision=precision_for(currency), currency=currency)


# ---- the engine's current currency -----------------------------------------


def current_currency() -> str | None:
	return getattr(frappe.local, _CURRENT, None)


@contextmanager
def currency_context(currency: str | None):
	"""Price in ``currency`` inside the block; the previous currency comes back afterwards."""
	previous = current_currency()
	setattr(frappe.local, _CURRENT, currency or previous)
	try:
		yield
	finally:
		setattr(frappe.local, _CURRENT, previous)


def document_currency(doc) -> str | None:
	return doc.get("currency") or company_currency(doc.get("company"))


def cart_currency(
	currency: str | None = None, pos_profile: str | None = None, company: str | None = None
) -> str | None:
	"""Currency a cart is priced in: its own, else its POS Profile's, else the company's."""
	if currency:
		return currency
	if pos_profile:
		values = frappe.get_cached_value("POS Profile", pos_profile, ["currency", "company"], as_dict=True)
		if values and values.currency:
			return values.currency
		company = company or (values.company if values else None)
	return company_currency(company)


def _payload(value) -> dict:
	if isinstance(value, str):
		try:
			value = json.loads(value or "{}")
		except ValueError:
			return {}
	return value if isinstance(value, dict) else {}


def in_cart_currency(fn):
	"""Decorator for cart entry points: run in the cart's currency.

	Reads ``currency`` / ``pos_profile`` / ``company`` from the call's arguments
	and from an ``invoice_data`` payload (dict or JSON), in that order of
	preference (:func:`cart_currency`). An explicit currency always wins; without
	one, an outer context (an invoice being validated) is kept.
	"""
	signature = inspect.signature(fn)

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		arguments = signature.bind_partial(*args, **kwargs).arguments
		payload = _payload(arguments.get("invoice_data"))
		explicit = arguments.get("currency") or payload.get("currency")
		if not explicit and current_currency():
			return fn(*args, **kwargs)
		currency = cart_currency(
			explicit,
			arguments.get("pos_profile") or payload.get("pos_profile"),
			arguments.get("company") or payload.get("company"),
		)
		with currency_context(currency or fallback_currency()):
			return fn(*args, **kwargs)

	return wrapper


def in_document_currency(fn):
	"""Decorator for ``(doc, method)`` hooks: run in the document's currency."""

	@functools.wraps(fn)
	def wrapper(doc, *args, **kwargs):
		with currency_context(document_currency(doc)):
			return fn(doc, *args, **kwargs)

	return wrapper
