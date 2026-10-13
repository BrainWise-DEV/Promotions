"""Customer-facing Friends & Family messages (Friends and Family Message doctype).

``get_customer_message(event, **ctx)`` returns ``{"event", "ar", "en", "message"}``
for an enabled record of that event, or ``None`` when there is no enabled record
(nothing is seeded; records are configured per site). Placeholders:
``{percent}`` (Settings discount %) and ``{brand}`` (Settings "Brand Name", else
the default Company). Unknown placeholders are left untouched.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr, flt

MESSAGE_DOCTYPE = "Friends and Family Message"
CACHE_KEY = "posnext_promotions:ff_customer_messages"

EVENT_FF_ACTIVATED = "ff_activated"
EVENT_COUPON_LOWER_FF_APPLIED = "coupon_lower_ff_applied"
EVENT_COUPON_HIGHER_APPLIED = "coupon_higher_applied"
EVENT_TIE = "tie"
EVENT_ITEM_EXCLUDED = "item_excluded"
EVENT_MONTHLY_CAP_REACHED = "monthly_cap_reached"
EVENT_EMAIL_CHANGE_OLD_EMAIL = "email_change_old_email"
EVENT_EMAIL_CHANGE_NEW_EMAIL = "email_change_new_email"

EVENTS = (
	EVENT_FF_ACTIVATED,
	EVENT_COUPON_LOWER_FF_APPLIED,
	EVENT_COUPON_HIGHER_APPLIED,
	EVENT_TIE,
	EVENT_ITEM_EXCLUDED,
	EVENT_MONTHLY_CAP_REACHED,
	EVENT_EMAIL_CHANGE_OLD_EMAIL,
	EVENT_EMAIL_CHANGE_NEW_EMAIL,
)


class _SafeDict(dict):
	def __missing__(self, key):
		return "{" + key + "}"


def _fill(text: str, values: dict) -> str:
	if not text:
		return ""
	try:
		return text.format_map(_SafeDict(values))
	except (ValueError, IndexError):
		return text


def _load_records() -> dict:
	cached = frappe.cache().get_value(CACHE_KEY)
	if cached is not None:
		return cached
	records = {}
	if frappe.db.table_exists(MESSAGE_DOCTYPE):
		for row in frappe.get_all(
			MESSAGE_DOCTYPE, fields=["event", "message_ar", "message_en", "enabled"]
		):
			if cint(row.enabled):
				records[row.event] = {"ar": cstr(row.message_ar), "en": cstr(row.message_en)}
	frappe.cache().set_value(CACHE_KEY, records)
	return records


def clear_cache():
	frappe.cache().delete_value(CACHE_KEY)


def _settings_value(fieldname):
	try:
		return frappe.db.get_single_value("Friends and Family Settings", fieldname)
	except Exception:
		return None


def _default_brand() -> str:
	"""Settings "Brand Name", else the default Company name, else empty."""
	brand = cstr(_settings_value("brand_name")).strip()
	if brand:
		return brand
	try:
		return cstr(frappe.defaults.get_global_default("company") or frappe.db.get_single_value("Global Defaults", "default_company"))
	except Exception:
		return ""


def get_customer_message(event: str, percent=None, brand=None, **extra) -> dict | None:
	"""Arabic + English customer text for an enabled record of ``event``; None when there is none."""
	try:
		record = _load_records().get(event)
	except Exception:
		record = None
	if not record or not (record.get("ar") or record.get("en")):
		return None
	ar, en = record.get("ar") or "", record.get("en") or ""
	text = ar + en
	if percent is None and "{percent}" in text:
		percent = flt(_settings_value("discount_percentage"))
	if brand is None and "{brand}" in text:
		brand = _default_brand()
	values = {"percent": f"{flt(percent):g}" if percent is not None else "", "brand": brand or "", **extra}
	ar, en = _fill(ar, values), _fill(en, values)
	return {"event": event, "ar": ar, "en": en, "message": ar or en}
