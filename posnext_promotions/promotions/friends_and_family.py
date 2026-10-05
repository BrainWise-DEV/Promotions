# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family program — the runtime view the discount engine reads.

Admins configure the program on the **Friends and Family Settings** singleton;
this module turns that document into an immutable :class:`FriendsAndFamilyProgram`
for one evaluation date, or ``None`` when the program must not apply.

Settings are read through ``frappe.get_cached_doc``. Saving the singleton clears
that cache entry, so the next cart / invoice recalculation sees the new values
without a deploy or restart.

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
SCOPES = (SCOPE_ENTIRE_CART, SCOPE_BRAND, SCOPE_ITEM_GROUP)


@dataclass(frozen=True)
class FriendsAndFamilyProgram:
	discount_percentage: float
	discount_scope: str
	brands: frozenset[str]
	# Pre-expanded to descendants so matching a line is a plain lookup.
	item_groups: frozenset[str]
	excluded_brands: frozenset[str]
	start_date: str | None
	end_date: str | None
	user_message: str

	def covers(self, item) -> bool:
		"""Whether a cart line falls inside the configured scope.

		Entire Cart covers every paid line except brands listed under Excluded Brands.
		Brand / Item Group scopes are inclusion-only (exclusions apply only to Entire Cart).
		"""
		if item.get("is_free_item"):
			return False
		brand = cstr(item.get("brand"))
		if self.discount_scope == SCOPE_ENTIRE_CART:
			if brand and brand in self.excluded_brands:
				return False
			return True
		if self.discount_scope == SCOPE_BRAND:
			return brand in self.brands
		if self.discount_scope == SCOPE_ITEM_GROUP:
			return cstr(item.get("item_group")) in self.item_groups
		return False

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
			"excluded_brands": sorted(self.excluded_brands),
			"start_date": self.start_date,
			"end_date": self.end_date,
			"user_message": self.user_message,
		}


def get_settings():
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def is_within_dates(settings, on_date=None) -> bool:
	"""Open-ended on either side when the bound is blank; both bounds inclusive."""
	on_date = getdate(on_date or nowdate())
	if settings.get("start_date") and on_date < getdate(settings.start_date):
		return False
	return not (settings.get("end_date") and on_date > getdate(settings.end_date))


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
	item_groups: set[str] = set()
	for row in settings.get("applicable_item_groups") or []:
		if row.item_group:
			item_groups.update(get_item_group_with_descendants(row.item_group))
	excluded_brands = frozenset(
		cstr(row.brand) for row in settings.get("excluded_brands") or [] if row.brand
	)

	if scope == SCOPE_BRAND and not brands:
		return None
	if scope == SCOPE_ITEM_GROUP and not item_groups:
		return None

	return FriendsAndFamilyProgram(
		discount_percentage=percentage,
		discount_scope=scope,
		brands=brands,
		item_groups=frozenset(item_groups),
		excluded_brands=excluded_brands,
		start_date=cstr(settings.start_date) if settings.get("start_date") else None,
		end_date=cstr(settings.end_date) if settings.get("end_date") else None,
		user_message=cstr(settings.get("user_message")),
	)
