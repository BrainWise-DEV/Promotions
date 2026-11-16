"""Customer-facing Friends & Family messages.

Two doctypes:

* **Friends and Family Message Event** (named by ``event_code``) — *when* a
  message fires. The engine's standard events below are created/updated on
  migrate as ``is_standard`` records (labels + descriptions only, never message
  text). Users may add custom events and fire them from code / hooks.
* **Friends and Family Message** — the configurable text (Arabic / English) for
  one event (``event`` Link, unique). Nothing is seeded.

``get_customer_message(event_code, context=None)`` returns
``{"event", "ar", "en", "message"}`` for an enabled message whose event is
enabled, or ``None``. Placeholders: ``{percent}`` (Settings discount %),
``{brand}`` (default Company name) plus any key in
``context``. Unknown placeholders are left untouched.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr, flt

MESSAGE_DOCTYPE = "Friends and Family Message"
EVENT_DOCTYPE = "Friends and Family Message Event"
CACHE_KEY = "posnext_promotions:ff_customer_messages"

EVENT_FF_ACTIVATED = "ff_activated"
EVENT_COUPON_LOWER_FF_APPLIED = "coupon_lower_ff_applied"
EVENT_COUPON_HIGHER_APPLIED = "coupon_higher_applied"
EVENT_TIE = "tie"
EVENT_ITEM_EXCLUDED = "item_excluded"
EVENT_MONTHLY_CAP_REACHED = "monthly_cap_reached"
EVENT_EMAIL_CHANGE_OLD_EMAIL = "email_change_old_email"
EVENT_EMAIL_CHANGE_NEW_EMAIL = "email_change_new_email"

# Standard events fired by the engine: code -> (label, description, placeholders).
STANDARD_EVENTS: dict[str, tuple[str, str, str]] = {
	EVENT_FF_ACTIVATED: (
		"F&F activated (after OTP verification) / تفعيل خصم الأصدقاء والعائلة",
		"Fires when the customer becomes eligible for Friends & Family (eligibility check; "
		"after OTP verification once that flow exists).",
		"{percent}, {brand}",
	),
	EVENT_COUPON_LOWER_FF_APPLIED: (
		"Coupon not applied — F&F is better / الكوبون أقل من خصم الأصدقاء والعائلة",
		"Fires when a coupon was entered but the Friends & Family discount is higher, so F&F is applied instead.",
		"{percent}, {brand}",
	),
	EVENT_COUPON_HIGHER_APPLIED: (
		"Coupon applied — better than F&F / تطبيق الكوبون لأنه أعلى",
		"Fires when the coupon gives a bigger discount than Friends & Family and is applied instead.",
		"{percent}, {brand}",
	),
	EVENT_TIE: (
		"Coupon equals F&F — F&F applied / تساوي الكوبون والخصم",
		"Fires when the coupon and the Friends & Family discount are equal; F&F is applied (one discount per order).",
		"{percent}, {brand}",
	),
	EVENT_ITEM_EXCLUDED: (
		"Sale item kept its price / منتج تخفيضات",
		"Fires per line when an already discounted (promotion / manual sale) item keeps its sale price instead of F&F.",
		"{percent}, {brand}",
	),
	EVENT_MONTHLY_CAP_REACHED: (
		"Monthly F&F limit reached / الوصول للحد الشهري",
		"Fires when the customer's monthly Friends & Family cap is used up; the order is charged at standard price.",
		"{percent}, {brand}",
	),
	EVENT_EMAIL_CHANGE_OLD_EMAIL: (
		"Email change — notice to old email / إشعار البريد القديم",
		"For the email-change flow: notice sent to the OLD address. Not fired yet (no email/OTP flow).",
		"{brand}",
	),
	EVENT_EMAIL_CHANGE_NEW_EMAIL: (
		"Email change — verify new email (OTP) / التحقق من البريد الجديد",
		"For the email-change flow: OTP verification request sent to the NEW address. Not fired yet.",
		"{brand}",
	),
}

EVENTS = tuple(STANDARD_EVENTS)


def ensure_standard_events():
	"""Idempotent (migrate): create standard event definitions; keep users' enabled choice."""
	if not frappe.db.table_exists(EVENT_DOCTYPE):
		return
	for code, (label, description, placeholders) in STANDARD_EVENTS.items():
		values = {"event_name": label, "description": description, "placeholders": placeholders, "is_standard": 1}
		if frappe.db.exists(EVENT_DOCTYPE, code):
			frappe.db.set_value(EVENT_DOCTYPE, code, values, update_modified=False)
		else:
			doc = frappe.get_doc({"doctype": EVENT_DOCTYPE, "event_code": code, "enabled": 1, **values})
			doc.flags.allow_standard = True
			doc.insert(ignore_permissions=True)
	clear_cache()


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
	if frappe.db.table_exists(MESSAGE_DOCTYPE) and frappe.db.table_exists(EVENT_DOCTYPE):
		enabled_events = set(frappe.get_all(EVENT_DOCTYPE, filters={"enabled": 1}, pluck="name"))
		for row in frappe.get_all(
			MESSAGE_DOCTYPE, fields=["event", "message_ar", "message_en", "enabled"]
		):
			if cint(row.enabled) and row.event in enabled_events:
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
	"""The default Company name (``{brand}``), else empty."""
	try:
		return cstr(frappe.defaults.get_global_default("company") or frappe.db.get_single_value("Global Defaults", "default_company"))
	except Exception:
		return ""


def get_customer_message(event_code: str, context: dict | None = None, percent=None, brand=None, **extra) -> dict | None:
	"""Arabic + English customer text configured for ``event_code`` (standard or custom).

	``context`` (and extra kwargs) supply placeholder values, e.g.
	``get_customer_message("my_event", {"customer_name": "Ali"})``. ``percent`` /
	``brand`` default to Settings when the text uses them. Returns None when the
	event or its message is missing / disabled.
	"""
	event = event_code
	extra = {**(context or {}), **extra}
	percent = extra.pop("percent", percent)
	brand = extra.pop("brand", brand)
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
