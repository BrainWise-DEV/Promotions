# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family auto-apply — percentage from Settings onto covered lines.

The cashier never chooses the F&F rate. Every path that stamps a Friends & Family
discount reads ``discount_percentage`` from the live program (admin Settings). A
client-supplied percentage is ignored.

Conflict rule for PN-126 (full multi-promo comparison is PN-128):
  * Best discount only — do not stack F&F on top of another line percentage.
  * Compare F&F % with the line's existing ``discount_percentage``.
  * Apply F&F when it is strictly larger, or when it ties (F&F wins ties).
  * Leave free-item / GWP stamped lines alone.

Coverage (PN-127): exclusions always win over scope inclusions. When the
customer is eligible but every paid line is excluded or out of scope, the
payload carries ``reason=no_covered_items`` and a clear cart-level ``message``.
Saving Settings clears the singleton cache, so the next apply recalculates.

Callers:

  * :func:`compute_friends_and_family_discounts` — pure read for the POS when
    the customer or cart changes.
  * :func:`apply_friends_and_family_to_items` — mutate cart lines (used from
    ``apply_offers`` and invoice ``validate``).
  * :func:`apply_friends_and_family_on_invoice` — Sales / POS Invoice validate
    hook so a save cannot persist a different F&F rate than Settings.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, nowdate

from posnext_promotions.promotions.friends_and_family import (
	COVER_EXCLUDED_BRAND,
	COVER_EXCLUDED_ITEM,
	COVER_EXCLUDED_ITEM_GROUP,
	COVER_FREE_ITEM,
	COVER_OK,
	COVER_OUT_OF_SCOPE,
)
from posnext_promotions.promotions.friends_and_family_eligibility import (
	get_customer_eligibility,
)

# Stamped on lines that received Friends & Family. Not a Pricing Rule name —
# F&F is configured on the Settings singleton, not as a Pricing Rule row.
DISCOUNT_SOURCE_FRIENDS_AND_FAMILY = "friends_and_family"

# Lines with these sources are never rewritten by F&F (product / gift mechanics).
_PROTECTED_SOURCES = frozenset({"gwp", "free_item"})

REASON_APPLIED = "applied"
REASON_OUT_OF_SCOPE = "out_of_scope"
REASON_EXCLUDED = "excluded"
REASON_NO_COVERED_ITEMS = "no_covered_items"
REASON_OTHER_BETTER = "other_discount_better"
REASON_PROTECTED = "protected_line"
REASON_NOT_DISCOUNTABLE = "not_discountable"
REASON_INELIGIBLE = "customer_ineligible"
REASON_PROGRAM_INACTIVE = "program_inactive"
REASON_STRIPPED = "stripped"


def _coverage_message(program, items, applied_count: int, eligibility_message: str) -> str:
	"""Cart-level note when the customer is eligible but nothing received F&F."""
	if applied_count > 0:
		return eligibility_message or (program.user_message if program else "")
	paid = [
		i
		for i in (items or [])
		if not i.get("is_free_item") and flt(i.get("qty") or i.get("quantity") or 0) > 0
	]
	if not paid:
		return eligibility_message or ""

	excluded = 0
	out_of_scope = 0
	for item in paid:
		cover = program.covers_reason(item)
		if cover in (COVER_EXCLUDED_ITEM, COVER_EXCLUDED_ITEM_GROUP, COVER_EXCLUDED_BRAND):
			excluded += 1
		elif cover == COVER_OUT_OF_SCOPE:
			out_of_scope += 1

	if excluded == len(paid):
		return _(
			"No Friends & Family discount applies — every item in the cart is excluded "
			"(excluded items, item groups, or brands)."
		)
	if excluded or out_of_scope:
		return _(
			"No Friends & Family discount applies to this cart — all lines are excluded or out of scope."
		)
	return eligibility_message or ""


def _attach_coverage_fields(payload, program, items, applied, eligibility) -> None:
	"""Set cart-level message / reason / exclusion lists after line evaluation."""
	payload["message"] = _coverage_message(program, items, applied, eligibility.message)
	if applied == 0 and items:
		has_uncovered = any(
			program.covers_reason(i)
			in (
				COVER_EXCLUDED_ITEM,
				COVER_EXCLUDED_ITEM_GROUP,
				COVER_EXCLUDED_BRAND,
				COVER_OUT_OF_SCOPE,
			)
			for i in items
		)
		if has_uncovered:
			payload["reason"] = REASON_NO_COVERED_ITEMS
	payload["excluded_items"] = sorted(program.excluded_items)
	payload["excluded_item_groups"] = sorted(program.excluded_item_groups)
	payload["excluded_brands"] = sorted(program.excluded_brands)


@dataclass(frozen=True)
class FriendsAndFamilyLineResult:
	item_code: str
	applies: bool
	discount_percentage: float
	discount_amount: float
	rate: float
	reason: str
	existing_discount_percentage: float = 0.0
	replaced_existing: bool = False
	idx: int | None = None

	def to_client_payload(self) -> dict:
		return {
			"item_code": self.item_code,
			"idx": self.idx,
			"applies": self.applies,
			"discount_percentage": self.discount_percentage,
			"discount_amount": self.discount_amount,
			"rate": self.rate,
			"reason": self.reason,
			"existing_discount_percentage": self.existing_discount_percentage,
			"replaced_existing": self.replaced_existing,
		}


def enrich_items_with_catalog(items) -> list:
	"""Backfill ``brand`` / ``item_group`` from Item when the cart payload omits them."""
	items = list(items or [])
	codes = list({cstr(i.get("item_code")) for i in items if i.get("item_code")})
	if not codes:
		return items

	details = {
		r.name: r
		for r in frappe.get_all(
			"Item",
			filters={"name": ["in", codes]},
			fields=["name", "item_group", "brand", "item_name"],
		)
	}

	def _set(item, key, value):
		if isinstance(item, dict):
			item[key] = value
		elif hasattr(item, "set"):
			item.set(key, value)
		else:
			setattr(item, key, value)

	for item in items:
		cached = details.get(cstr(item.get("item_code")))
		if not cached:
			continue
		if not item.get("item_group"):
			_set(item, "item_group", cached.item_group)
		if not item.get("brand"):
			_set(item, "brand", cached.brand)
		if not item.get("item_name") and cached.item_name:
			_set(item, "item_name", cached.item_name)
	return items


def _line_qty(item) -> float:
	return flt(item.get("qty") or item.get("quantity") or 0)


def _line_price_list_rate(item) -> float:
	return flt(item.get("price_list_rate") or item.get("rate") or 0)


def _is_protected_line(item) -> bool:
	if item.get("is_free_item"):
		return True
	if cstr(item.get("discount_source")) in _PROTECTED_SOURCES:
		return True
	if flt(item.get("free_qty")) > 0 or flt(item.get("gwp_free_qty")) > 0:
		return True
	return False


def _is_discountable(item) -> bool:
	return _line_qty(item) > 0 and _line_price_list_rate(item) > 0


def _materialize_amounts(price_list_rate: float, qty: float, percentage: float) -> tuple[float, float]:
	"""Return ``(discount_amount as line total, net rate)``."""
	percentage = flt(percentage)
	discount_amount = price_list_rate * qty * percentage / 100.0
	rate = price_list_rate * (1.0 - percentage / 100.0)
	return discount_amount, rate


def _set_item_attr(item, key: str, value) -> None:
	if isinstance(item, dict):
		item[key] = value
	else:
		item.set(key, value) if hasattr(item, "set") else setattr(item, key, value)


def _clear_friends_and_family_stamp(item) -> None:
	"""Remove an earlier F&F stamp; leave non-F&F discounts untouched."""
	if cstr(item.get("discount_source")) != DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
		return
	_set_item_attr(item, "discount_source", None)
	_set_item_attr(item, "discount_percentage", 0)
	_set_item_attr(item, "discount_amount", 0)
	price_list_rate = _line_price_list_rate(item)
	if price_list_rate:
		_set_item_attr(item, "rate", price_list_rate)


def _stamp_friends_and_family(item, percentage: float) -> tuple[float, float]:
	"""Write Settings % onto the line. Percentage is never taken from the client."""
	qty = _line_qty(item)
	price_list_rate = _line_price_list_rate(item)
	discount_amount, rate = _materialize_amounts(price_list_rate, qty, percentage)
	_set_item_attr(item, "discount_percentage", percentage)
	_set_item_attr(item, "discount_amount", discount_amount)
	_set_item_attr(item, "rate", rate)
	_set_item_attr(item, "price_list_rate", price_list_rate)
	_set_item_attr(item, "discount_source", DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
	_set_item_attr(item, "is_already_discounted", 1)
	return discount_amount, rate


def _evaluate_line(item, program, idx: int | None = None) -> FriendsAndFamilyLineResult:
	item_code = cstr(item.get("item_code"))
	existing = flt(item.get("discount_percentage") or 0)
	# A line already stamped F&F counts as "no competing other discount" for the
	# comparison — we re-stamp with the live Settings % (idempotent / rate lock).
	if cstr(item.get("discount_source")) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
		existing_for_compare = 0.0
	else:
		existing_for_compare = existing

	qty = _line_qty(item)
	price_list_rate = _line_price_list_rate(item)

	def _result(applies, percentage, reason, replaced=False, amount=0.0, rate=None):
		if rate is None:
			rate = price_list_rate * (1.0 - flt(percentage) / 100.0) if price_list_rate else flt(item.get("rate") or 0)
		return FriendsAndFamilyLineResult(
			item_code=item_code,
			applies=applies,
			discount_percentage=flt(percentage) if applies else 0.0,
			discount_amount=flt(amount),
			rate=flt(rate),
			reason=reason,
			existing_discount_percentage=existing,
			replaced_existing=replaced,
			idx=idx,
		)

	if _is_protected_line(item):
		return _result(False, 0, REASON_PROTECTED, rate=flt(item.get("rate") or price_list_rate))

	if not _is_discountable(item):
		return _result(False, 0, REASON_NOT_DISCOUNTABLE, rate=flt(item.get("rate") or 0))

	cover = program.covers_reason(item)
	if cover != COVER_OK:
		reason = (
			REASON_PROTECTED
			if cover == COVER_FREE_ITEM
			else (
				REASON_EXCLUDED
				if cover in (COVER_EXCLUDED_ITEM, COVER_EXCLUDED_ITEM_GROUP, COVER_EXCLUDED_BRAND)
				else REASON_OUT_OF_SCOPE
			)
		)
		return _result(False, 0, reason, rate=flt(item.get("rate") or price_list_rate))

	fnf_pct = flt(program.discount_percentage)
	# Best-only: F&F applies when larger than the competing %, or on a tie.
	if existing_for_compare > fnf_pct:
		return _result(False, 0, REASON_OTHER_BETTER, rate=flt(item.get("rate") or price_list_rate))

	amount, rate = _materialize_amounts(price_list_rate, qty, fnf_pct)
	replaced = existing_for_compare > 0 and existing_for_compare <= fnf_pct
	return _result(True, fnf_pct, REASON_APPLIED, replaced=replaced, amount=amount, rate=rate)


def compute_friends_and_family_discounts(customer=None, items=None, posting_date=None) -> dict:
	"""Per-line F&F decision for ``customer`` + cart ``items`` on ``posting_date``.

	Does not mutate ``items``. The returned ``discount_percentage`` (header and
	per applying line) always comes from Settings — never from the request.
	"""
	posting_date = posting_date or nowdate()
	items = enrich_items_with_catalog(list(items or []))
	eligibility = get_customer_eligibility(customer, posting_date)
	program = eligibility.program if eligibility.eligible else None

	payload = {
		"customer": eligibility.customer,
		"eligible": eligibility.eligible,
		"reason": eligibility.reason,
		"message": eligibility.message,
		"program_active": eligibility.program is not None,
		# Central rate. 0 when the customer cannot receive F&F on this date.
		"discount_percentage": flt(program.discount_percentage) if program else 0.0,
		"discount_scope": program.discount_scope if program else None,
		"user_message": program.user_message if program else "",
		"items": [],
		"applied_count": 0,
	}

	if program is None:
		inactive_reason = (
			REASON_PROGRAM_INACTIVE if eligibility.reason == "program_inactive" else REASON_INELIGIBLE
		)
		for idx, item in enumerate(items):
			payload["items"].append(
				FriendsAndFamilyLineResult(
					item_code=cstr(item.get("item_code")),
					applies=False,
					discount_percentage=0.0,
					discount_amount=0.0,
					rate=flt(item.get("rate") or item.get("price_list_rate") or 0),
					reason=inactive_reason,
					existing_discount_percentage=flt(item.get("discount_percentage") or 0),
					idx=idx,
				).to_client_payload()
			)
		return payload

	applied = 0
	for idx, item in enumerate(items):
		result = _evaluate_line(item, program, idx=idx)
		if result.applies:
			applied += 1
		payload["items"].append(result.to_client_payload())
	payload["applied_count"] = applied
	_attach_coverage_fields(payload, program, items, applied, eligibility)
	return payload


def apply_friends_and_family_to_items(items, customer=None, posting_date=None) -> dict:
	"""Mutate ``items``: stamp F&F where it wins, strip stale F&F stamps otherwise.

	Safe to call repeatedly (idempotent). Returns the same payload shape as
	:func:`compute_friends_and_family_discounts`, reflecting the post-mutation
	state.
	"""
	posting_date = posting_date or nowdate()
	items = enrich_items_with_catalog(list(items or []))
	eligibility = get_customer_eligibility(customer, posting_date)
	program = eligibility.program if eligibility.eligible else None

	payload = {
		"customer": eligibility.customer,
		"eligible": eligibility.eligible,
		"reason": eligibility.reason,
		"message": eligibility.message,
		"program_active": eligibility.program is not None,
		"discount_percentage": flt(program.discount_percentage) if program else 0.0,
		"discount_scope": program.discount_scope if program else None,
		"user_message": program.user_message if program else "",
		"items": [],
		"applied_count": 0,
	}

	applied = 0
	for idx, item in enumerate(items):
		if program is None:
			was_fnf = cstr(item.get("discount_source")) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY
			_clear_friends_and_family_stamp(item)
			reason = (
				REASON_STRIPPED
				if was_fnf
				else (
					REASON_PROGRAM_INACTIVE
					if eligibility.reason == "program_inactive"
					else REASON_INELIGIBLE
				)
			)
			result = FriendsAndFamilyLineResult(
				item_code=cstr(item.get("item_code")),
				applies=False,
				discount_percentage=0.0,
				discount_amount=0.0,
				rate=flt(item.get("rate") or item.get("price_list_rate") or 0),
				reason=reason,
				existing_discount_percentage=0.0,
				idx=idx,
			)
		else:
			result = _evaluate_line(item, program, idx=idx)
			if result.applies:
				amount, rate = _stamp_friends_and_family(item, program.discount_percentage)
				result = FriendsAndFamilyLineResult(
					item_code=result.item_code,
					applies=True,
					discount_percentage=flt(program.discount_percentage),
					discount_amount=amount,
					rate=rate,
					reason=REASON_APPLIED,
					existing_discount_percentage=result.existing_discount_percentage,
					replaced_existing=result.replaced_existing,
					idx=idx,
				)
				applied += 1
			else:
				# Covered comparison lost, or out of scope / excluded: drop a stale
				# F&F stamp but keep a competing better discount.
				_clear_friends_and_family_stamp(item)
				result = FriendsAndFamilyLineResult(
					item_code=result.item_code,
					applies=False,
					discount_percentage=0.0,
					discount_amount=0.0,
					rate=flt(item.get("rate") or item.get("price_list_rate") or 0),
					reason=result.reason,
					existing_discount_percentage=result.existing_discount_percentage,
					idx=idx,
				)

		payload["items"].append(result.to_client_payload())

	payload["applied_count"] = applied
	if program is not None:
		_attach_coverage_fields(payload, program, items, applied, eligibility)
	return payload


def apply_friends_and_family_on_invoice(doc, method=None):
	"""Sales Invoice / POS Invoice ``validate`` — enforce Settings % server-side.

	Runs after other line-discount hooks so F&F can be compared with whatever
	percentage is already on each line. Skips during install / migrate / patch.
	"""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
		return
	items = doc.get("items")
	if not items:
		return

	# Only POS / point-of-sale invoices for now — the program is a POS benefit.
	# Use .get("items") — on frappe._dict, .items is dict.items.
	is_pos_doc = cstr(doc.get("doctype")) == "POS Invoice" or cint(doc.get("is_pos"))
	if not is_pos_doc:
		return

	apply_friends_and_family_to_items(
		items,
		customer=doc.get("customer"),
		posting_date=doc.get("posting_date") or nowdate(),
	)
