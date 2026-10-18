# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family program — the runtime view the discount engine reads.

Admins configure the program on the **Friends and Family Settings** singleton;
this module turns that document into an immutable :class:`FriendsAndFamilyProgram`
for one evaluation date, or ``None`` when the program must not apply.

Settings are read through ``frappe.get_cached_doc``. Saving the singleton clears
that cache entry, so the next cart / invoice recalculation sees the new values
(including inclusion / exclusion list edits) without a deploy or restart.

Whether a given *customer* is entitled to the program is decided in
:mod:`posnext_promotions.promotions.friends_and_family_eligibility`; this module
only answers "is the program live, and which lines does it cover".
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe.utils import cint, cstr, flt, getdate, nowdate

from posnext_promotions.promotions.scope import get_item_group_with_descendants

SETTINGS_DOCTYPE = "Friends and Family Settings"

SCOPE_ENTIRE_CART = "Entire Cart"
SCOPE_BRAND = "Brand"
SCOPE_ITEM_GROUP = "Item Group"
SCOPE_ITEM_CODE = "Item Code"
SCOPES = (SCOPE_ENTIRE_CART, SCOPE_BRAND, SCOPE_ITEM_GROUP, SCOPE_ITEM_CODE)

# Machine reasons returned by covers_reason (also used by the apply engine).
COVER_OK = "covered"
COVER_FREE_ITEM = "free_item"
COVER_EXCLUDED_ITEM = "excluded_item"
COVER_EXCLUDED_ITEM_GROUP = "excluded_item_group"
COVER_EXCLUDED_BRAND = "excluded_brand"
COVER_OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class FriendsAndFamilyProgram:
	discount_percentage: float
	discount_scope: str
	brands: frozenset[str]
	# Pre-expanded to descendants so matching a line is a plain lookup.
	item_groups: frozenset[str]
	items: frozenset[str]
	excluded_brands: frozenset[str]
	excluded_items: frozenset[str]
	# Pre-expanded to descendants.
	excluded_item_groups: frozenset[str]
	start_date: str | None
	end_date: str | None
	user_message: str
	# PN-129 caps (money). 0 = no cap.
	max_discount_per_order: float = 0.0
	max_discount_per_customer_month: float = 0.0

	@property
	def has_caps(self) -> bool:
		return self.max_discount_per_order > 0 or self.max_discount_per_customer_month > 0

	def covers_reason(self, item) -> str:
		"""Why a cart line is / is not covered. Exclusions always win.

		Order:
		  1. Free / GWP lines are never covered.
		  2. Explicit excluded items.
		  3. Excluded item groups (incl. descendants).
		  4. Scope inclusion (Entire Cart / Brand / Item Group / Item Code).
		  5. For Entire Cart only: excluded brands.
		"""
		if item.get("is_free_item"):
			return COVER_FREE_ITEM

		item_code = cstr(item.get("item_code"))
		if item_code and item_code in self.excluded_items:
			return COVER_EXCLUDED_ITEM

		item_group = cstr(item.get("item_group"))
		if item_group and item_group in self.excluded_item_groups:
			return COVER_EXCLUDED_ITEM_GROUP

		brand = cstr(item.get("brand"))

		if self.discount_scope == SCOPE_ENTIRE_CART:
			if brand and brand in self.excluded_brands:
				return COVER_EXCLUDED_BRAND
			return COVER_OK

		if self.discount_scope == SCOPE_BRAND:
			return COVER_OK if brand in self.brands else COVER_OUT_OF_SCOPE

		if self.discount_scope == SCOPE_ITEM_GROUP:
			return COVER_OK if item_group in self.item_groups else COVER_OUT_OF_SCOPE

		if self.discount_scope == SCOPE_ITEM_CODE:
			return COVER_OK if item_code in self.items else COVER_OUT_OF_SCOPE

		return COVER_OUT_OF_SCOPE

	def covers(self, item) -> bool:
		"""Whether a cart line falls inside the configured scope after exclusions."""
		return self.covers_reason(item) == COVER_OK

	def line_percentage(self, item) -> float:
		"""The F&F percentage for this line; 0 when the line is out of scope."""
		return self.discount_percentage if self.covers(item) else 0.0

	def to_client_payload(self) -> dict:
		return {
			"active": True,
			"discount_percentage": self.discount_percentage,
			"discount_scope": self.discount_scope,
			"brands": sorted(self.brands),
			"item_groups": sorted(self.item_groups),
			"items": sorted(self.items),
			"excluded_brands": sorted(self.excluded_brands),
			"excluded_items": sorted(self.excluded_items),
			"excluded_item_groups": sorted(self.excluded_item_groups),
			"start_date": self.start_date,
			"end_date": self.end_date,
			"user_message": self.user_message,
			"max_discount_per_order": self.max_discount_per_order,
			"max_discount_per_customer_month": self.max_discount_per_customer_month,
		}


# Custom Field on POS Profile (posnext_promotions/custom/pos_profile.json).
PROFILE_ALLOW_FIELD = "allow_friends_family"
# Custom Field on Promotional Scheme (posnext_promotions/custom/promotional_scheme.json).
SCHEME_APPLY_WITH_FF_FIELD = "apply_with_ff"


def get_settings():
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def profile_allows_ff(pos_profile) -> bool:
	"""Whether F&F may run on ``pos_profile`` (the **Allow F&F** check).

	No profile (desk / API calls without one), an unknown profile, or a site
	where the field is not installed yet all mean "allowed", so existing setups
	keep working. A blank value (row created before the field existed) counts as
	allowed too; only an explicit 0 turns F&F off for the profile.
	"""
	pos_profile = cstr(pos_profile).strip()
	if not pos_profile:
		return True
	if not frappe.get_meta("POS Profile").has_field(PROFILE_ALLOW_FIELD):
		return True
	value = frappe.db.get_value("POS Profile", pos_profile, PROFILE_ALLOW_FIELD)
	return True if value is None else bool(cint(value))


def is_within_dates(settings, on_date=None) -> bool:
	"""Open-ended on either side when the bound is blank; both bounds inclusive."""
	on_date = getdate(on_date or nowdate())
	if settings.get("start_date") and on_date < getdate(settings.start_date):
		return False
	return not (settings.get("end_date") and on_date > getdate(settings.end_date))


def _expand_item_groups(rows, fieldname: str = "item_group") -> frozenset[str]:
	groups: set[str] = set()
	for row in rows or []:
		value = row.get(fieldname) if hasattr(row, "get") else getattr(row, fieldname, None)
		if value:
			groups.update(get_item_group_with_descendants(value))
	return frozenset(groups)


def get_active_program(on_date=None) -> FriendsAndFamilyProgram | None:
	"""The program as it applies on ``on_date`` (defaults to today), or ``None``.

	Pass the invoice's ``posting_date`` so an offline sale synced later is judged
	on the day it happened.
	"""
	settings = get_settings()
	if not cint(settings.get("enabled")):
		return None
	if not is_within_dates(settings, on_date):
		return None

	percentage = flt(settings.get("discount_percentage"))
	if percentage <= 0 or percentage > 100:
		return None

	scope = cstr(settings.get("discount_scope"))
	if scope not in SCOPES:
		return None

	brands = frozenset(cstr(row.brand) for row in settings.get("applicable_brands") or [] if row.brand)
	item_groups = _expand_item_groups(settings.get("applicable_item_groups") or [])
	items = frozenset(
		cstr(row.item_code) for row in settings.get("applicable_items") or [] if row.item_code
	)
	excluded_brands = frozenset(
		cstr(row.brand) for row in settings.get("excluded_brands") or [] if row.brand
	)
	excluded_items = frozenset(
		cstr(row.item_code) for row in settings.get("excluded_items") or [] if row.item_code
	)
	excluded_item_groups = _expand_item_groups(settings.get("excluded_item_groups") or [])

	if scope == SCOPE_BRAND and not brands:
		return None
	if scope == SCOPE_ITEM_GROUP and not item_groups:
		return None
	if scope == SCOPE_ITEM_CODE and not items:
		return None

	return FriendsAndFamilyProgram(
		discount_percentage=percentage,
		discount_scope=scope,
		brands=brands,
		item_groups=item_groups,
		items=items,
		excluded_brands=excluded_brands,
		excluded_items=excluded_items,
		excluded_item_groups=excluded_item_groups,
		start_date=cstr(settings.start_date) if settings.get("start_date") else None,
		end_date=cstr(settings.end_date) if settings.get("end_date") else None,
		user_message=cstr(settings.get("user_message")),
		max_discount_per_order=max(0.0, flt(settings.get("max_discount_per_order"))),
		max_discount_per_customer_month=max(0.0, flt(settings.get("max_discount_per_customer_month"))),
	)
