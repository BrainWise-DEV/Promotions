"""Customer-facing Friends & Family messages (Friends and Family Message doctype).

``get_customer_message(event, **ctx)`` returns ``{"event", "ar", "en", "message"}``
for an enabled record of that event, falling back to the built-in default text
below when the record is missing/disabled or the doctype is not installed yet.
Placeholders: ``{percent}`` (Settings discount %, e.g. 30) and ``{brand}``
(default "ميرايا" / "Miraaya"). Unknown placeholders are left untouched.
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

DEFAULT_BRAND_AR = "ميرايا"
DEFAULT_BRAND_EN = "Miraaya"

# event -> (description, Arabic, English)
DEFAULT_MESSAGES: dict[str, tuple[str, str, str]] = {
	EVENT_FF_ACTIVATED: (
		"After OTP verification — the F&F discount is now active",
		"خصمك الحصري بنسبة {percent}% لبرنامج الأصدقاء والعائلة أصبح مفعّلًا الآن. استمتع بتسوقك في {brand}!",
		"Your exclusive {percent}% Friends & Family discount is now active. Enjoy shopping at {brand}!",
	),
	EVENT_COUPON_LOWER_FF_APPLIED: (
		"A coupon was entered but the F&F discount is better",
		"لم يتم تطبيق الكوبون الخاص بك، لأن خصم الأصدقاء والعائلة يمنحك قيمة أفضل. تم تطبيق خصم الأصدقاء والعائلة بدلًا منه.",
		"Your coupon was not applied because your Friends & Family discount gives you better value. "
		"The Friends & Family discount was applied instead.",
	),
	EVENT_COUPON_HIGHER_APPLIED: (
		"The coupon is better than F&F and was applied",
		"تم تطبيق الكوبون الخاص بك، فهو يمنحك خصمًا أكبر من مزايا برنامج الأصدقاء والعائلة لهذا الطلب.",
		"Your coupon was applied, as it gives you a bigger discount than your Friends & Family benefit on this order.",
	),
	EVENT_TIE: (
		"Coupon and F&F are equal — F&F applied, one discount per order",
		"تم تطبيق خصم الأصدقاء والعائلة الخاص بك، وهو يطابق قيمة الكوبون. يُطبق خصم واحد فقط لكل طلب.",
		"Your Friends & Family discount was applied; it matches the coupon value. Only one discount applies per order.",
	),
	EVENT_ITEM_EXCLUDED: (
		"Item already discounted (sale) — F&F not applied to it",
		"لا ينطبق خصم الأصدقاء والعائلة على منتجات التخفيضات. تم الإبقاء على سعر التخفيض الحالي لهذا المنتج.",
		"The Friends & Family discount does not apply to sale items. The current sale price was kept for this item.",
	),
	EVENT_MONTHLY_CAP_REACHED: (
		"Customer reached the monthly F&F cap",
		"لقد وصلت إلى الحد الأقصى لخصم الأصدقاء والعائلة لهذا الشهر. سيتم احتساب طلبك بالسعر القياسي.",
		"You have reached your Friends & Family discount limit for this month. Your order is charged at the standard price.",
	),
	EVENT_EMAIL_CHANGE_OLD_EMAIL: (
		"Sent to the OLD email when an email change is requested",
		"تم تقديم طلب لتحديث البريد الإلكتروني في حسابك على {brand}. إذا لم تقم بهذا التغيير بنفسك، يرجى التواصل مع فريق الدعم فورًا.",
		"A request was made to update the email on your {brand} account. If you did not make this change, "
		"please contact support immediately.",
	),
	EVENT_EMAIL_CHANGE_NEW_EMAIL: (
		"Sent to the NEW email to verify F&F eligibility (OTP)",
		"يرجى التحقق من عنوان بريدك الإلكتروني الجديد لتأكيد أهليتك لبرنامج الأصدقاء والعائلة في {brand}. أدخل رمز التحقق (OTP) المرسل إلى هذا العنوان.",
		"Please verify your new email address to confirm your eligibility for the {brand} Friends & Family "
		"program. Enter the verification code (OTP) sent to this address.",
	),
}

EVENTS = tuple(DEFAULT_MESSAGES)


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


def _default_percent():
	try:
		return flt(frappe.db.get_single_value("Friends and Family Settings", "discount_percentage"))
	except Exception:
		return 0


def get_customer_message(event: str, percent=None, brand_ar=None, brand_en=None, **extra) -> dict:
	"""Arabic + English customer text for an F&F event (record, else default)."""
	default = DEFAULT_MESSAGES.get(event)
	try:
		record = _load_records().get(event) or {}
	except Exception:
		record = {}
	ar = record.get("ar") or (default[1] if default else "")
	en = record.get("en") or (default[2] if default else "")
	if percent is None and ("{percent}" in ar or "{percent}" in en):
		percent = _default_percent()
	pct = f"{flt(percent):g}" if percent is not None else ""
	ar = _fill(ar, {"percent": pct, "brand": brand_ar or DEFAULT_BRAND_AR, **extra})
	en = _fill(en, {"percent": pct, "brand": brand_en or DEFAULT_BRAND_EN, **extra})
	return {"event": event, "ar": ar, "en": en, "message": ar or en}


def seed_default_messages():
	"""Idempotent: create missing event records; never overwrite edited ones."""
	if not frappe.db.table_exists(MESSAGE_DOCTYPE):
		return
	for event, (description, ar, en) in DEFAULT_MESSAGES.items():
		if frappe.db.exists(MESSAGE_DOCTYPE, event):
			continue
		frappe.get_doc(
			{
				"doctype": MESSAGE_DOCTYPE,
				"event": event,
				"enabled": 1,
				"description": description,
				"message_ar": ar,
				"message_en": en,
			}
		).insert(ignore_permissions=True)
	clear_cache()
