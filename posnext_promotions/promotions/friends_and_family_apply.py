# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family auto-apply — percentage from Settings onto covered lines.

The cashier never chooses the F&F rate. Every path that stamps a Friends & Family
discount reads ``discount_percentage`` from the live program (admin Settings). A
client-supplied percentage is ignored.

Conflict rule (PN-128, :mod:`posnext_promotions.promotions.best_discount`):
  * Best discount only — F&F is never stacked on another discount.
  * Per line, F&F competes in **money saved** with whatever discount the line
    already carries (automatic promotion, line-level coupon, manual discount).
    F&F applies when it saves strictly more, or on a tie (F&F wins ties).
  * When F&F wins, the losing discount is removed from the line (its Pricing
    Rules / coupon tag are cleared) so it cannot ride along.
  * Order-level competitors (invoice header discount from a transaction
    promotion / order coupon / manual invoice discount) are compared on total
    savings by :func:`apply_friends_and_family_on_invoice` and ``apply_offers``.
  * Free-item / GWP stamped lines are left alone (product discounts, not
    compared).

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
    hook: enforces the Settings rate and best-only (no stacking) server-side.

Every payload carries a ``best_discount`` block (winner, candidates with
amounts, suppressed sources, English + Arabic message) for the POS UI.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, nowdate

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions.friends_and_family import (
	COVER_EXCLUDED_BRAND,
	COVER_EXCLUDED_ITEM,
	COVER_EXCLUDED_ITEM_GROUP,
	COVER_FREE_ITEM,
	COVER_OK,
	COVER_OUT_OF_SCOPE,
)
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	CapContext,
	allocate,
	build_cap_payload,
	ceil_cents,
	get_cap_context,
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
REASON_ORDER_DISCOUNT_BETTER = "order_discount_better"
REASON_CAP_REACHED = "cap_reached"


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
	# PN-128 best-discount comparison for this line (None when not compared).
	competing_source: str | None = None
	competing_amount: float = 0.0
	decision: dict | None = None
	# PN-129: F&F before caps, and whether a cap reduced / removed it.
	fnf_uncapped_amount: float = 0.0
	capped: bool = False

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
			"competing_source": self.competing_source,
			"competing_amount": bd.money(self.competing_amount),
			"best_discount": self.decision,
			"fnf_uncapped_amount": bd.money(self.fnf_uncapped_amount),
			"capped": self.capped,
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
	"""Lines carrying a gift / free-unit benefit are never rewritten by F&F.

	A bare ``discount_source="gwp"`` tag with no discount only means a GWP rule
	*matched* the line (e.g. qty below the gift threshold) — that line has no
	benefit to protect, so F&F may apply (PN-128).
	"""
	if item.get("is_free_item"):
		return True
	if flt(item.get("free_qty")) > 0 or flt(item.get("gwp_free_qty")) > 0:
		return True
	if cstr(item.get("discount_source")) in _PROTECTED_SOURCES:
		return flt(item.get("discount_amount")) > 0 or flt(item.get("discount_percentage")) > 0
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


def _is_fnf_stamped(item) -> bool:
	return DISCOUNT_SOURCE_FRIENDS_AND_FAMILY in (
		cstr(item.get("discount_source")),
		cstr(item.get(FIELD_DISCOUNT_SOURCE)),
	)


def _clear_fnf_audit_fields(item) -> None:
	if cstr(item.get(FIELD_DISCOUNT_SOURCE)) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
		_set_item_attr(item, FIELD_DISCOUNT_SOURCE, None)
	if flt(item.get(FIELD_FNF_AMOUNT)):
		_set_item_attr(item, FIELD_FNF_AMOUNT, 0)


def _clear_friends_and_family_stamp(item) -> None:
	"""Remove an earlier F&F stamp; leave non-F&F discounts untouched."""
	if not _is_fnf_stamped(item):
		_clear_fnf_audit_fields(item)
		return
	_strip_line_discount(item)


def _strip_line_discount(item) -> None:
	"""Back to list price with no discount metadata."""
	_set_item_attr(item, "discount_source", None)
	_set_item_attr(item, "discount_percentage", 0)
	_set_item_attr(item, "discount_amount", 0)
	_set_item_attr(item, "is_already_discounted", 0)
	_clear_fnf_audit_fields(item)
	price_list_rate = _line_price_list_rate(item)
	if price_list_rate:
		_set_item_attr(item, "rate", price_list_rate)


def _capped_line_values(price_list_rate: float, qty: float, line_amount: float, amount_basis: str) -> tuple:
	"""``(actual line saving, net rate, percentage, stored discount_amount)`` for a capped line.

	Saved invoice rows hold a per-unit rate in currency precision, so the rate is
	rounded **up** to the cent — the line never saves more than its allowance.
	"""
	gross = price_list_rate * qty
	if gross <= 0 or qty <= 0:
		return 0.0, price_list_rate, 0.0, 0.0
	line_amount = max(0.0, min(flt(line_amount), gross))
	if amount_basis == bd.AMOUNT_BASIS_UNIT:
		rate = min(price_list_rate, max(0.0, ceil_cents(price_list_rate - line_amount / qty)))
		stored = bd.money(price_list_rate - rate)
		actual = bd.money(stored * qty)
	else:
		rate = price_list_rate - line_amount / qty
		actual = line_amount
		stored = line_amount
	return actual, rate, actual / gross * 100.0, stored


def _stamp_friends_and_family(
	item, percentage: float, amount_basis: str = bd.AMOUNT_BASIS_LINE, line_amount: float | None = None
) -> tuple[float, float]:
	"""Write the F&F discount onto the line. Percentage is never taken from the client.

	Returns ``(line discount total, net rate)``. On saved invoice rows
	(``amount_basis="unit"``) ``discount_amount`` is stored per unit, as ERPNext
	expects; on cart payloads it is the line total, as ``apply_offers`` uses.
	``line_amount`` (PN-129) stamps a cap-reduced saving instead of the full %.
	"""
	qty = _line_qty(item)
	price_list_rate = _line_price_list_rate(item)
	if line_amount is not None:
		discount_amount, rate, percentage, stored_amount = _capped_line_values(
			price_list_rate, qty, line_amount, amount_basis
		)
	else:
		discount_amount, rate = _materialize_amounts(price_list_rate, qty, percentage)
		stored_amount = (
			price_list_rate * flt(percentage) / 100.0
			if amount_basis == bd.AMOUNT_BASIS_UNIT
			else discount_amount
		)
	_set_item_attr(item, "discount_percentage", percentage)
	_set_item_attr(item, "discount_amount", stored_amount)
	_set_item_attr(item, "rate", rate)
	_set_item_attr(item, "price_list_rate", price_list_rate)
	_set_item_attr(item, "discount_source", DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
	_set_item_attr(item, "is_already_discounted", 1)
	# Persisted audit trail (PN-129): drives the monthly cap and reporting.
	_set_item_attr(item, FIELD_DISCOUNT_SOURCE, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
	_set_item_attr(item, FIELD_FNF_AMOUNT, bd.money(discount_amount))
	return discount_amount, rate


def _drop_competing_tags(item, competing_source: str | None) -> list[str]:
	"""F&F won the line: detach the losing promotion / coupon so it cannot ride along.

	Returns the Pricing Rule names removed from the line (for applied-rule pruning).
	"""
	removed: list[str] = []
	if competing_source == bd.SOURCE_PROMOTION:
		raw = item.get("pricing_rules")
		if isinstance(raw, list | tuple | set):
			removed = [cstr(r) for r in raw if cstr(r)]
		elif raw:
			text = cstr(raw).strip()
			if text.startswith("["):
				import json

				try:
					removed = [cstr(r) for r in json.loads(text) if cstr(r)]
				except ValueError:
					removed = []
			else:
				removed = [r.strip() for r in text.split(",") if r.strip()]
		_set_item_attr(item, "pricing_rules", "")
		if item.get("applied_promotional_schemes"):
			_set_item_attr(item, "applied_promotional_schemes", [])
		if item.get("is_accumulative_discount"):
			_set_item_attr(item, "is_accumulative_discount", 0)
	if competing_source == bd.SOURCE_COUPON or item.get("posnext_coupon_code"):
		if item.get("posnext_coupon_code"):
			_set_item_attr(item, "posnext_coupon_code", None)
		if item.get("coupon_code"):
			_set_item_attr(item, "coupon_code", None)
	return removed


def _looks_like_fnf_stamp(item, program, amount_basis: str) -> bool:
	"""Treat the line's current discount as our own earlier F&F stamp.

	Cart payloads carry ``discount_source``. Saved invoice rows do not persist
	it, so on the invoice path a percentage equal to the Settings rate with no
	Pricing Rule / coupon behind it is our stamp (re-stamping is idempotent).
	"""
	if _is_fnf_stamped(item):
		return True
	if amount_basis != bd.AMOUNT_BASIS_UNIT:
		return False
	if bd.has_pricing_rules(item) or bd.line_coupon_code(item):
		return False
	return abs(flt(item.get("discount_percentage")) - flt(program.discount_percentage)) < 0.0001


def _competing_candidate(item, program, amount_basis: str) -> bd.Candidate | None:
	"""The non-F&F discount currently on the line, as a best-discount candidate."""
	if _looks_like_fnf_stamp(item, program, amount_basis):
		return None
	amount = bd.line_discount_value(item, amount_basis)
	if amount <= 0:
		return None
	source = bd.classify_line_source(item, amount_basis)
	if source in (bd.SOURCE_NONE, bd.SOURCE_FREE_ITEM, bd.SOURCE_FRIENDS_AND_FAMILY):
		return None
	pct = flt(item.get("discount_percentage") or 0)
	code = bd.line_coupon_code(item) or None if source == bd.SOURCE_COUPON else None
	return bd.Candidate(source=source, amount=amount, percentage=pct if pct > 0 else None, code=code)


def _evaluate_line(
	item,
	program,
	idx: int | None = None,
	amount_basis: str = bd.AMOUNT_BASIS_LINE,
	fnf_amount_override: float | None = None,
) -> FriendsAndFamilyLineResult:
	"""Best-of decision for one line. ``fnf_amount_override`` = cap-reduced F&F saving (PN-129)."""
	item_code = cstr(item.get("item_code"))
	existing = flt(item.get("discount_percentage") or 0)
	qty = _line_qty(item)
	price_list_rate = _line_price_list_rate(item)

	def _result(
		applies,
		percentage,
		reason,
		replaced=False,
		amount=0.0,
		rate=None,
		competitor=None,
		decision=None,
		uncapped=0.0,
		capped=False,
	):
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
			competing_source=competitor.source if competitor else None,
			competing_amount=competitor.amount if competitor else 0.0,
			decision=decision.to_payload() if decision else None,
			fnf_uncapped_amount=flt(uncapped),
			capped=capped,
		)

	if _is_protected_line(item):
		return _result(False, 0, REASON_PROTECTED, rate=flt(item.get("rate") or price_list_rate))

	if not _is_discountable(item):
		return _result(False, 0, REASON_NOT_DISCOUNTABLE, rate=flt(item.get("rate") or 0))

	competitor = _competing_candidate(item, program, amount_basis)

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
		return _result(False, 0, reason, rate=flt(item.get("rate") or price_list_rate), competitor=competitor)

	fnf_pct = flt(program.discount_percentage)
	fnf_full, fnf_rate = _materialize_amounts(price_list_rate, qty, fnf_pct)
	fnf_amount, fnf_line_pct = fnf_full, fnf_pct
	capped = fnf_amount_override is not None and bd.money(fnf_amount_override) < bd.money(fnf_full)
	if capped:
		fnf_amount, fnf_rate, fnf_line_pct, _stored = _capped_line_values(
			price_list_rate, qty, fnf_amount_override, amount_basis
		)
	fnf = (
		bd.Candidate(source=bd.SOURCE_FRIENDS_AND_FAMILY, amount=fnf_amount, percentage=flt(fnf_line_pct, 2))
		if bd.money(fnf_amount) > 0
		else None
	)
	decision = bd.decide([fnf, competitor], idx=idx, item_code=item_code)

	if not decision.fnf_wins:
		would_have_won = competitor is None or bd.money(fnf_full) >= bd.money(competitor.amount)
		return _result(
			False,
			0,
			REASON_CAP_REACHED if capped and would_have_won else REASON_OTHER_BETTER,
			rate=flt(item.get("rate") or price_list_rate),
			competitor=competitor,
			decision=decision if decision.candidates else None,
			uncapped=fnf_full,
			capped=capped,
		)

	return _result(
		True,
		fnf_line_pct,
		REASON_APPLIED,
		replaced=competitor is not None,
		amount=fnf_amount,
		rate=fnf_rate,
		competitor=competitor,
		decision=decision,
		uncapped=fnf_full,
		capped=capped,
	)


def _plan_lines(items, program, customer, posting_date, amount_basis, exclude_invoice=None) -> tuple:
	"""Per-line decisions with PN-129 caps applied. Returns ``(results, cap context, uncapped total)``."""
	results = [
		_evaluate_line(item, program, idx=idx, amount_basis=amount_basis) for idx, item in enumerate(items)
	]
	ctx = get_cap_context(program, customer, posting_date, exclude=exclude_invoice)
	uncapped_total = sum(r.discount_amount for r in results if r.applies)
	allowance = ctx.allowance
	if allowance is None or bd.money(uncapped_total) <= bd.money(allowance):
		return results, ctx, uncapped_total

	alloc = allocate(
		[(r.idx, r.fnf_uncapped_amount, r.competing_amount) for r in results if r.fnf_uncapped_amount > 0],
		allowance,
	)
	final = []
	for item, res in zip(items, results, strict=False):
		if res.fnf_uncapped_amount > 0:
			res = _evaluate_line(
				item,
				program,
				idx=res.idx,
				amount_basis=amount_basis,
				fnf_amount_override=alloc.get(res.idx, 0.0),
			)
		final.append(res)
	return final, ctx, uncapped_total


def _applied_total(results) -> float:
	return sum(flt(r.discount_amount) for r in results if r.applies)


def _attach_caps(payload, caps: dict) -> None:
	payload["caps"] = caps
	payload["best_discount"]["caps"] = caps
	if caps.get("capped"):
		payload["message"] = caps["message"]
		payload["message_en"] = caps["message_en"]
		payload["message_ar"] = caps["message_ar"]
		payload["cap_hit"] = caps["cap_hit"]


def _decision_from_payload(payload: dict | None, idx=None, item_code=None):
	if not payload:
		return None
	candidates = [
		bd.Candidate(source=c["source"], amount=c["amount"], percentage=c.get("percentage"), code=c.get("code"))
		for c in payload.get("candidates") or []
	]
	return bd.decide(candidates, idx=idx, item_code=item_code)


def _line_decisions(results) -> list:
	"""Rebuild :class:`best_discount.Decision` objects from line results for the summary."""
	return [
		d
		for d in (_decision_from_payload(res.decision, idx=res.idx, item_code=res.item_code) for res in results)
		if d is not None
	]


def summarize_line_payloads(lines) -> dict:
	"""``best_discount`` summary from client line payloads (``items`` of an F&F payload)."""
	return bd.summarize(
		[
			d
			for d in (
				_decision_from_payload(line.get("best_discount"), idx=line.get("idx"), item_code=line.get("item_code"))
				for line in lines or []
			)
			if d is not None
		]
	)


def _base_payload(eligibility, program) -> dict:
	return {
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


def compute_friends_and_family_discounts(
	customer=None,
	items=None,
	posting_date=None,
	amount_basis: str = bd.AMOUNT_BASIS_LINE,
	exclude_invoice: str | None = None,
) -> dict:
	"""Per-line F&F decision for ``customer`` + cart ``items`` on ``posting_date``.

	Does not mutate ``items``. The returned ``discount_percentage`` (header and
	per applying line) always comes from Settings — never from the request.
	"""
	posting_date = posting_date or nowdate()
	items = enrich_items_with_catalog(list(items or []))
	eligibility = get_customer_eligibility(customer, posting_date)
	program = eligibility.program if eligibility.eligible else None
	payload = _base_payload(eligibility, program)

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
		payload["best_discount"] = bd.summarize([])
		_attach_caps(payload, build_cap_payload(CapContext(), 0, 0))
		return payload

	results, ctx, uncapped_total = _plan_lines(
		items, program, customer, posting_date, amount_basis, exclude_invoice
	)
	caps = build_cap_payload(ctx, uncapped_total, _applied_total(results))
	applied = sum(1 for r in results if r.applies)
	payload["items"] = [r.to_client_payload() for r in results]
	payload["applied_count"] = applied
	_attach_coverage_fields(payload, program, items, applied, eligibility)
	payload["best_discount"] = bd.summarize(_line_decisions(results))
	_attach_caps(payload, caps)
	return payload


def apply_friends_and_family_to_items(
	items,
	customer=None,
	posting_date=None,
	amount_basis: str = bd.AMOUNT_BASIS_LINE,
	exclude_invoice: str | None = None,
) -> dict:
	"""Mutate ``items``: stamp F&F where it wins, strip stale F&F stamps otherwise.

	Where F&F wins a line the losing promotion / coupon is detached from it
	(``pricing_rules`` / coupon tag cleared) so nothing stacks. The removed
	Pricing Rule names are returned in ``suppressed_pricing_rules``.

	Safe to call repeatedly (idempotent). Returns the same payload shape as
	:func:`compute_friends_and_family_discounts`, reflecting the post-mutation
	state.
	"""
	posting_date = posting_date or nowdate()
	items = enrich_items_with_catalog(list(items or []))
	eligibility = get_customer_eligibility(customer, posting_date)
	program = eligibility.program if eligibility.eligible else None
	payload = _base_payload(eligibility, program)
	payload["suppressed_pricing_rules"] = []

	suppressed_rules: set[str] = set()
	if program is None:
		results = []
		for idx, item in enumerate(items):
			was_fnf = _is_fnf_stamped(item)
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
			results.append(
				FriendsAndFamilyLineResult(
					item_code=cstr(item.get("item_code")),
					applies=False,
					discount_percentage=0.0,
					discount_amount=0.0,
					rate=flt(item.get("rate") or item.get("price_list_rate") or 0),
					reason=reason,
					existing_discount_percentage=0.0,
					idx=idx,
				)
			)
		caps = build_cap_payload(CapContext(), 0, 0)
	else:
		planned, ctx, uncapped_total = _plan_lines(
			items, program, customer, posting_date, amount_basis, exclude_invoice
		)
		results = []
		for item, result in zip(items, planned, strict=False):
			if result.applies:
				suppressed_rules.update(_drop_competing_tags(item, result.competing_source))
				amount, rate = _stamp_friends_and_family(
					item,
					program.discount_percentage,
					amount_basis,
					line_amount=result.discount_amount if result.capped else None,
				)
				result = replace(result, discount_amount=amount, rate=rate)
			else:
				# Covered comparison lost (or cap exhausted), or out of scope /
				# excluded: drop a stale F&F stamp but keep a competing discount.
				_clear_friends_and_family_stamp(item)
				result = replace(
					result,
					discount_percentage=0.0,
					discount_amount=0.0,
					rate=flt(item.get("rate") or item.get("price_list_rate") or 0),
				)
			results.append(result)
		# Built after stamping: on saved rows the per-unit rate is rounded, so
		# the applied total is what the invoice really gives.
		caps = build_cap_payload(ctx, uncapped_total, _applied_total(results))

	applied = sum(1 for r in results if r.applies)
	payload["items"] = [r.to_client_payload() for r in results]
	payload["applied_count"] = applied
	payload["suppressed_pricing_rules"] = sorted(suppressed_rules)
	if program is not None:
		_attach_coverage_fields(payload, program, items, applied, eligibility)
	payload["best_discount"] = bd.summarize(_line_decisions(results))
	_attach_caps(payload, caps)
	return payload


# ---- invoice (server-side enforcement) ------------------------------------

_LINE_SNAPSHOT_FIELDS = (
	"discount_percentage",
	"discount_amount",
	"rate",
	"pricing_rules",
	"posnext_coupon_code",
	"discount_source",
	"is_already_discounted",
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
)


def _snapshot_line(item) -> dict:
	return {f: item.get(f) for f in _LINE_SNAPSHOT_FIELDS}


def _restore_line(item, snap: dict) -> None:
	for f, value in snap.items():
		if value is None and item.get(f) is None:
			continue
		_set_item_attr(item, f, value)


def _header_discount_value(doc) -> float:
	return flt(doc.get("discount_amount") or 0)


def _pos_coupon_value(coupon_code: str, base_net: float, base_grand: float) -> tuple[float, float | None] | None:
	"""``(value, percentage)`` of an order-level POS Coupon on a base (``None`` if unknown)."""
	code = cstr(coupon_code).strip().upper()
	if not code or not frappe.db.table_exists("POS Coupon"):
		return None
	row = frappe.db.get_value(
		"POS Coupon",
		{"coupon_code": code},
		["discount_type", "discount_percentage", "discount_amount", "max_amount", "apply_on"],
		as_dict=True,
	)
	if not row:
		return None
	base = base_grand if cstr(row.apply_on) == "Grand Total" else base_net
	pct = None
	if cstr(row.discount_type) == "Percentage":
		pct = flt(row.discount_percentage)
		value = flt(base) * pct / 100.0
	else:
		value = flt(row.discount_amount)
	if flt(row.max_amount) and value > flt(row.max_amount):
		value = flt(row.max_amount)
		pct = None  # capped: the face % no longer describes it
	return max(0.0, min(value, flt(base))), pct


def _order_candidate_without_fnf(doc, items, b_line_values) -> bd.Candidate:
	"""Header discount the invoice would carry if F&F did not apply (scenario B)."""
	gross = sum(bd.line_gross(i) for i in items if not i.get("is_free_item"))
	base_net = max(0.0, gross - sum(b_line_values))
	base_grand = base_net + flt(doc.get("total_taxes_and_charges") or 0)
	pct = flt(doc.get("additional_discount_percentage") or 0)
	coupon_code = cstr(doc.get("coupon_code")).strip()
	value = _header_discount_value(doc)
	if coupon_code:
		coupon = _pos_coupon_value(coupon_code, base_net, base_grand)
		coupon_pct = None
		if coupon is not None:
			value = max(value, coupon[0])
			coupon_pct = coupon[1]
		return bd.Candidate(
			source=bd.SOURCE_ORDER_COUPON,
			amount=value,
			code=coupon_code.upper(),
			percentage=coupon_pct or pct or None,
		)
	if pct:
		base = base_grand if cstr(doc.get("apply_discount_on")) == "Grand Total" else base_net
		value = max(value, base * pct / 100.0)
	return bd.Candidate(source=bd.SOURCE_ORDER_MANUAL, amount=value, percentage=pct or None)


def _resolve_order_level(doc, items, snapshots, b_line_values, ff_payload, fnf_like=None) -> dict | None:
	"""F&F lines vs an invoice header discount → keep the scenario that saves more.

	A = F&F (with per-line best-of) and **no** header discount.
	B = no F&F (lines keep their non-F&F discounts) **plus** the header discount.
	Tie → A (F&F).
	"""
	if not ff_payload.get("applied_count"):
		return None
	header_pct = flt(doc.get("additional_discount_percentage") or 0)
	header_amt = _header_discount_value(doc)
	if not header_pct and not header_amt:
		return None

	a_total = sum(bd.line_discount_value(i, bd.AMOUNT_BASIS_UNIT) for i in items)
	order_b = _order_candidate_without_fnf(doc, items, b_line_values)
	fnf_side = bd.Candidate(
		source=bd.SOURCE_FRIENDS_AND_FAMILY,
		amount=a_total,
		percentage=flt(ff_payload.get("discount_percentage")) or None,
	)
	other_side = bd.Candidate(
		source=order_b.source,
		amount=sum(b_line_values) + order_b.amount,
		percentage=order_b.percentage,
		code=order_b.code,
	)
	decision = bd.decide([fnf_side, other_side], granularity=bd.GRANULARITY_ORDER)

	if decision.fnf_wins:
		doc.additional_discount_percentage = 0
		doc.discount_amount = 0
		if doc.get("base_discount_amount"):
			doc.base_discount_amount = 0
		# An order-level coupon that lost contributes nothing — drop it so its
		# usage is not counted (line-level coupon tags were handled per line).
		if doc.get("coupon_code") and not any(i.get("posnext_coupon_code") for i in items):
			doc.coupon_code = None
	else:
		fnf_like = fnf_like or [False] * len(items)
		for item, snap, was_fnf in zip(items, snapshots, fnf_like, strict=False):
			now_fnf = _is_fnf_stamped(item)
			_restore_line(item, snap)
			if was_fnf or now_fnf:
				# Our F&F stamp (new or carried in from the cart) — remove it,
				# keeping whatever else the line carried (Pricing Rules, tags).
				_strip_line_discount(item)
		if order_b.source == bd.SOURCE_ORDER_COUPON and not header_pct:
			doc.discount_amount = order_b.amount
		ff_payload["applied_count"] = 0
		ff_payload["reason"] = REASON_ORDER_DISCOUNT_BETTER
		if ff_payload.get("caps"):
			ff_payload["caps"]["fnf_applied_total"] = 0.0
		for line in ff_payload.get("items") or []:
			if line.get("applies"):
				line.update(
					{"applies": False, "reason": REASON_ORDER_DISCOUNT_BETTER, "discount_percentage": 0.0}
				)
	return decision.to_payload()


def _invoice_state(doc, items) -> tuple:
	return (
		tuple(
			(flt(i.get("discount_percentage")), flt(i.get("discount_amount")), flt(i.get("rate")))
			for i in items
		),
		flt(doc.get("additional_discount_percentage")),
		flt(doc.get("discount_amount")),
		cstr(doc.get("coupon_code")),
	)


def _stamp_return_usage(items) -> None:
	"""Credit-note rows give F&F allowance back: store the negative saving for the returned qty.

	The row copies ``posnext_discount_source`` / rate / price list rate from
	the original line, so the per-unit saving is ``price_list_rate - rate``.
	"""
	for row in items:
		if cstr(row.get(FIELD_DISCOUNT_SOURCE)) != DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
			if flt(row.get(FIELD_FNF_AMOUNT)):
				_set_item_attr(row, FIELD_FNF_AMOUNT, 0)
			continue
		price_list_rate = flt(row.get("price_list_rate"))
		rate = flt(row.get("rate"))
		per_unit = price_list_rate - rate if price_list_rate > rate else flt(row.get("discount_amount"))
		_set_item_attr(row, FIELD_FNF_AMOUNT, -bd.money(abs(flt(row.get("qty"))) * max(0.0, per_unit)))


def apply_friends_and_family_on_invoice(doc, method=None):
	"""Sales Invoice / POS Invoice ``validate`` — enforce Settings % and best-only.

	Runs after the other line-discount hooks, so each line's current discount
	is the competitor:

	  1. Per line: F&F vs the line's own discount (promotion / coupon tag /
	     manual) — larger saving wins, tie → F&F, loser detached.
	  2. Order level: if F&F applied anywhere and the invoice also carries a
	     header discount (order coupon / transaction promotion / manual), keep
	     only the scenario that saves more (tie → F&F).
	  3. Totals are recalculated when anything changed, so the saved invoice
	     can never hold F&F stacked with another discount.

	  4. PN-129 caps (per order / monthly per customer) are applied inside the
	     per-line plan; each F&F row persists ``posnext_discount_source`` and
	     ``posnext_fnf_discount_amount`` for the monthly usage ledger.

	Returns / credit notes only get their (negative) F&F usage stamped.

	Skips during install / migrate / patch.
	"""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
		return
	items = doc.get("items")
	if not items:
		return

	if cint(doc.get("is_return")):
		_stamp_return_usage(items)
		return

	# Only POS / point-of-sale invoices for now — the program is a POS benefit.
	# Use .get("items") — on frappe._dict, .items is dict.items.
	is_pos_doc = cstr(doc.get("doctype")) == "POS Invoice" or cint(doc.get("is_pos"))
	if not is_pos_doc:
		return

	before = _invoice_state(doc, items)
	snapshots = [_snapshot_line(i) for i in items]
	posting_date = doc.get("posting_date") or nowdate()

	# Scenario-B line values: what each line saves without F&F.
	program = get_customer_eligibility(doc.get("customer"), posting_date)
	program = program.program if program.eligible else None
	b_line_values = []
	fnf_like = []
	for item in items:
		was_fnf = program is not None and _looks_like_fnf_stamp(item, program, bd.AMOUNT_BASIS_UNIT)
		fnf_like.append(was_fnf)
		b_line_values.append(0.0 if was_fnf else bd.line_discount_value(item, bd.AMOUNT_BASIS_UNIT))

	ff_payload = apply_friends_and_family_to_items(
		items,
		customer=doc.get("customer"),
		posting_date=posting_date,
		amount_basis=bd.AMOUNT_BASIS_UNIT,
		exclude_invoice=doc.get("name") or None,
	)
	order_decision = _resolve_order_level(doc, items, snapshots, b_line_values, ff_payload, fnf_like)
	if order_decision:
		ff_payload["best_discount"]["order"] = order_decision
		ff_payload["best_discount"]["granularity"] = bd.GRANULARITY_ORDER
		ff_payload["best_discount"]["winner"] = order_decision["winner"]
		for key in ("message", "message_en", "message_ar"):
			ff_payload["best_discount"][key] = order_decision[key]

	flags = getattr(doc, "flags", None)
	if flags is not None:
		flags.friends_and_family = ff_payload

	# frappe._dict answers any attribute with None, so test callability.
	recalc = getattr(doc, "calculate_taxes_and_totals", None)
	if _invoice_state(doc, items) != before and callable(recalc):
		recalc()
	return ff_payload
