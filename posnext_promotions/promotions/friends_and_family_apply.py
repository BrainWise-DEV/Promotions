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
	SCHEME_APPLY_WITH_FF_FIELD,
	SETTINGS_DOCTYPE,
)
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	FIELD_FNF_PERCENTAGE,
	FIELD_FNF_PROGRAM,
	FIELD_FNF_REPLACED,
	CapContext,
	allocate,
	build_cap_payload,
	cart_cap_basis,
	get_cap_context,
)
from posnext_promotions.promotions import (
	ff_compatibility,
	ff_config,
	ff_ledger,
	ff_money,
	ff_paid,
	ff_snapshot,
)
from posnext_promotions.promotions import friends_and_family_gifts as gifts
from posnext_promotions.promotions.friends_and_family_gifts import line_blocked_by_scheme
from posnext_promotions.promotions.ff_paid import is_paid  # noqa: F401  (public: Task 6c interface)
from posnext_promotions.promotions.friends_and_family_eligibility import (
	REASON_PROFILE_NOT_ALLOWED,
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
# Line belongs to a Buy X Get Y / Routine (Item Level Discount) scheme — not eligible.
REASON_SCHEME_NOT_ELIGIBLE = "scheme_not_eligible"


def _coverage_message(program, items, applied_count: int, eligibility_message: str) -> str:
	"""Cart-level note when the customer is eligible but nothing received F&F."""
	if applied_count > 0:
		return eligibility_message or ""
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

	@property
	def customer_message(self) -> dict | None:
		"""item_excluded text when the line keeps an existing sale (promotion / manual) discount."""
		if self.applies or self.reason != REASON_OTHER_BETTER:
			return None
		if self.competing_source not in (bd.SOURCE_PROMOTION, bd.SOURCE_MANUAL):
			return None
		from posnext_promotions.promotions.friends_and_family_messages import (
			EVENT_ITEM_EXCLUDED,
			get_customer_message,
		)

		try:
			return get_customer_message(EVENT_ITEM_EXCLUDED)
		except Exception:
			return None

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
			"customer_message": self.customer_message,
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
	if flt(item.get(FIELD_FNF_PERCENTAGE)):
		_set_item_attr(item, FIELD_FNF_PERCENTAGE, 0)
	if item.get(FIELD_FNF_PROGRAM):
		_set_item_attr(item, FIELD_FNF_PROGRAM, None)


# Line fields that describe the discount F&F replaced (restored on the next run).
_REPLACED_FIELDS = (
	"discount_source",
	"discount_percentage",
	"discount_amount",
	"rate",
	"pricing_rules",
	"posnext_coupon_code",
	"posnext_coupon_discount",
	"coupon_code",
	"is_already_discounted",
)


SNAPSHOT_HOURS_FIELD = "replaced_discount_snapshot_hours"


def _snapshot_ttl_seconds() -> int:
	hours = cint(frappe.get_cached_doc(SETTINGS_DOCTYPE).get(SNAPSHOT_HOURS_FIELD))
	if hours < 1:
		# Settings not saved since the field was added (Settings.validate stores >= 1).
		hours = cint(frappe.get_meta(SETTINGS_DOCTYPE).get_field(SNAPSHOT_HOURS_FIELD).default)
	return hours * 3600


def _replaced_snapshot(item, ctx) -> str | None:
	data = {key: item.get(key) for key in _REPLACED_FIELDS}
	return ff_snapshot.sign(item, data, ttl_seconds=_snapshot_ttl_seconds(), doc=ctx)


def _restore_replaced_discount(item, doc) -> bool:
	"""Put back the discount an earlier F&F stamp replaced (PN-133).

	Saved invoices are validated more than once (save, then submit). Without
	this, the second run sees an F&F line with no competitor — the promotion it
	beat is gone — so the comparison (and its audit log) is lost, and a cap
	that later reduces F&F could not hand the line back to its promotion.

	The snapshot sits in a client-writable field, so only a server-signed one
	for this line's values is used, and only while every Pricing Rule in it is
	still applies to ``doc`` (the invoice, or :func:`restore_context` for a cart):
	enabled, valid on its posting date, same company / profile / customer
	(Ruling 24). Otherwise it is dropped and the line is priced from today's rules.
	"""
	raw = item.get(FIELD_FNF_REPLACED)
	if not raw:
		return False
	_set_item_attr(item, FIELD_FNF_REPLACED, None)
	if not _is_fnf_stamped(item):
		return False
	data = ff_snapshot.verify(item, raw, doc=doc)
	# Signature first: a forged snapshot never reaches the rule lookups.
	if data is None or not ff_snapshot.rules_apply_to(data, doc):
		return False
	_clear_fnf_audit_fields(item)
	for key in _REPLACED_FIELDS:
		if key in data:
			_set_item_attr(item, key, data[key])
	return True


def restore_context(customer, posting_date, pos_profile=None) -> frappe._dict:
	"""Doc-like view of a cart for :func:`_restore_replaced_discount`."""
	return frappe._dict(customer=customer, posting_date=posting_date, pos_profile=pos_profile)


def _restored_copy(item, doc):
	"""Non-mutating view of ``item`` with its replaced discount put back."""
	if not item.get(FIELD_FNF_REPLACED):
		return item
	copy = frappe._dict(item.as_dict() if callable(getattr(item, "as_dict", None)) else dict(item))
	_restore_replaced_discount(copy, doc)
	return copy


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
	if item.get(FIELD_FNF_REPLACED):
		_set_item_attr(item, FIELD_FNF_REPLACED, None)
	price_list_rate = _line_price_list_rate(item)
	if price_list_rate:
		_set_item_attr(item, "rate", price_list_rate)


def _capped_line_values(price_list_rate: float, qty: float, line_amount: float, amount_basis: str) -> tuple:
	"""``(actual line saving, net rate, percentage, stored discount_amount)`` for a capped line.

	Saved invoice rows hold a per-unit rate in currency precision, so the rate is
	rounded **up** to the currency's smallest unit — the line never saves more
	than its allowance.
	"""
	gross = price_list_rate * qty
	if gross <= 0 or qty <= 0:
		return 0.0, price_list_rate, 0.0, 0.0
	line_amount = max(0.0, min(flt(line_amount), gross))
	if amount_basis == bd.AMOUNT_BASIS_UNIT:
		rate = ff_money.ceil_money(price_list_rate - line_amount / qty)
		rate = min(price_list_rate, max(0.0, rate))
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
	program_percentage = percentage
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
	# Line snapshot: the program's rate (the line's own, possibly capped, % is
	# discount_percentage). Program names arrive in Phase 3; blank until then.
	_set_item_attr(item, FIELD_FNF_PERCENTAGE, flt(program_percentage))
	_set_item_attr(item, FIELD_FNF_PROGRAM, None)
	return discount_amount, rate


def _pricing_rule_names(item) -> list[str]:
	"""Pricing Rule names on a line (list, JSON string or comma separated string)."""
	raw = item.get("pricing_rules")
	if not raw:
		return []
	if isinstance(raw, list | tuple | set):
		return [cstr(r) for r in raw if cstr(r)]
	text = cstr(raw).strip()
	if text.startswith("["):
		import json

		try:
			return [cstr(r) for r in json.loads(text) if cstr(r)]
		except ValueError:
			return []
	return [r.strip() for r in text.split(",") if r.strip()]


def promotion_competes_with_ff(item) -> bool:
	"""May the promotion on this line be compared with F&F? (Promotional Scheme "Apply with F&F")

	``False`` only when at least one Promotional Scheme behind the line's Pricing
	Rules has **Apply with F&F** unticked: such a scheme never competes, F&F
	replaces it. Pricing Rules that are not generated by a scheme, and schemes with
	the check ticked, compete on money saved as before.
	"""
	for rule in _pricing_rule_names(item):
		scheme = frappe.db.get_value("Pricing Rule", rule, "promotional_scheme")
		if not scheme:
			continue
		flag = frappe.db.get_value("Promotional Scheme", scheme, SCHEME_APPLY_WITH_FF_FIELD)
		if not cint(flag):
			return False
	return True


def _drop_competing_tags(item, competing_source: str | None) -> list[str]:
	"""F&F won the line: detach the losing promotion / coupon so it cannot ride along.

	Returns the Pricing Rule names removed from the line (for applied-rule pruning).
	"""
	removed: list[str] = []
	if competing_source == bd.SOURCE_PROMOTION:
		removed = _pricing_rule_names(item)
		_set_item_attr(item, "pricing_rules", "")
		if item.get("applied_promotional_schemes"):
			_set_item_attr(item, "applied_promotional_schemes", [])
		if item.get("is_accumulative_discount"):
			_set_item_attr(item, "is_accumulative_discount", 0)
	if competing_source == bd.SOURCE_COUPON or item.get("posnext_coupon_code"):
		if item.get("posnext_coupon_code"):
			_set_item_attr(item, "posnext_coupon_code", None)
		if item.get("posnext_coupon_discount"):
			_set_item_attr(item, "posnext_coupon_discount", 0)
		if item.get("coupon_code"):
			_set_item_attr(item, "coupon_code", None)
	return removed


def _looks_like_fnf_stamp(item, program, amount_basis: str) -> bool:
	"""Treat the line's current discount as our own earlier F&F stamp.

	Only an explicit marker counts: the source tag (``discount_source`` on cart
	payloads, ``posnext_discount_source`` on saved rows) or the stored F&F
	percentage. A manual or legacy discount that merely equals the program's
	percentage is a competitor, not our stamp.
	"""
	return _is_fnf_stamped(item) or flt(item.get(FIELD_FNF_PERCENTAGE)) > 0


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
	skipped = source == bd.SOURCE_PROMOTION and not promotion_competes_with_ff(item)
	return bd.Candidate(
		source=source, amount=amount, percentage=pct if pct > 0 else None, code=code, skipped=skipped
	)


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

	if line_blocked_by_scheme(item):
		# Miraaya: Buy X Get Y and Routine (Item Level Discount) lines get no F&F;
		# they keep their promotion. Other lines are unaffected.
		return _result(False, 0, REASON_SCHEME_NOT_ELIGIBLE, rate=flt(item.get("rate") or price_list_rate))

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
		would_have_won = (
			competitor is None or competitor.skipped or bd.money(fnf_full) >= bd.money(competitor.amount)
		)
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


def _plan_lines(
	items,
	program,
	customer,
	posting_date,
	amount_basis,
	exclude_invoice=None,
	conversion_rate: float | None = None,
	pos_profile: str | None = None,
	company: str | None = None,
	ignore_caps: bool = False,
) -> tuple:
	"""Per-line decisions with PN-129 caps applied. Returns ``(results, cap context, uncapped total)``.

	Caps are company currency and per company; ``conversion_rate`` turns them into
	this order's currency. ``None`` (cart) = the POS Profile's company and rate,
	looked up only when caps are set. ``ignore_caps``: the full entitlement (paid
	invoice check), no cap lookup.
	"""
	if ignore_caps:
		results = [
			_evaluate_line(item, program, idx=idx, amount_basis=amount_basis) for idx, item in enumerate(items)
		]
		return results, CapContext(), sum(r.discount_amount for r in results if r.applies)
	if conversion_rate is None:
		if program.has_caps:
			profile_company, conversion_rate = cart_cap_basis(pos_profile, posting_date)
			company = company or profile_company
		else:
			conversion_rate = 1.0
	results = [
		_evaluate_line(item, program, idx=idx, amount_basis=amount_basis) for idx, item in enumerate(items)
	]
	ctx = get_cap_context(
		program, customer, posting_date, exclude=exclude_invoice, conversion_rate=conversion_rate, company=company
	)
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
		bd.Candidate(
			source=c["source"],
			amount=c["amount"],
			percentage=c.get("percentage"),
			code=c.get("code"),
			skipped=bool(c.get("skipped")),
		)
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


def _inactive_reason(eligibility) -> str:
	if eligibility.reason == REASON_PROFILE_NOT_ALLOWED:
		return REASON_PROFILE_NOT_ALLOWED
	return REASON_PROGRAM_INACTIVE if eligibility.reason == "program_inactive" else REASON_INELIGIBLE


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
		"items": [],
		"applied_count": 0,
	}


@ff_money.in_cart_currency
def compute_friends_and_family_discounts(
	customer=None,
	items=None,
	posting_date=None,
	amount_basis: str = bd.AMOUNT_BASIS_LINE,
	exclude_invoice: str | None = None,
	pos_profile: str | None = None,
	conversion_rate: float | None = None,
	company: str | None = None,
	ignore_caps: bool = False,
) -> dict:
	"""Per-line F&F decision for ``customer`` + cart ``items`` on ``posting_date``.

	Does not mutate ``items``. The returned ``discount_percentage`` (header and
	per applying line) always comes from Settings — never from the request.
	``ignore_caps``: the entitlement before PN-129 caps.
	"""
	posting_date = posting_date or nowdate()
	signed_for = restore_context(customer, posting_date, pos_profile)
	items = enrich_items_with_catalog([_restored_copy(i, signed_for) for i in items or []])
	eligibility = get_customer_eligibility(customer, posting_date, pos_profile)
	program = eligibility.program if eligibility.eligible else None
	payload = _base_payload(eligibility, program)

	if program is None:
		inactive_reason = _inactive_reason(eligibility)
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
		items,
		program,
		customer,
		posting_date,
		amount_basis,
		exclude_invoice,
		conversion_rate,
		pos_profile,
		company,
		ignore_caps=ignore_caps,
	)
	caps = build_cap_payload(ctx, uncapped_total, _applied_total(results))
	applied = sum(1 for r in results if r.applies)
	payload["items"] = [r.to_client_payload() for r in results]
	payload["applied_count"] = applied
	_attach_coverage_fields(payload, program, items, applied, eligibility)
	payload["best_discount"] = bd.summarize(_line_decisions(results))
	_attach_caps(payload, caps)
	return payload


@ff_money.in_cart_currency
def apply_friends_and_family_to_items(
	items,
	customer=None,
	posting_date=None,
	amount_basis: str = bd.AMOUNT_BASIS_LINE,
	exclude_invoice: str | None = None,
	pos_profile: str | None = None,
	conversion_rate: float | None = None,
	company: str | None = None,
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
	signed_for = restore_context(customer, posting_date, pos_profile)
	restored = {idx for idx, item in enumerate(items) if _restore_replaced_discount(item, signed_for)}
	eligibility = get_customer_eligibility(customer, posting_date, pos_profile)
	program = eligibility.program if eligibility.eligible else None
	payload = _base_payload(eligibility, program)
	payload["suppressed_pricing_rules"] = []

	suppressed_rules: set[str] = set()
	if program is None:
		results = []
		for idx, item in enumerate(items):
			was_fnf = idx in restored or _is_fnf_stamped(item)
			_clear_friends_and_family_stamp(item)
			reason = REASON_STRIPPED if was_fnf else _inactive_reason(eligibility)
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
			items, program, customer, posting_date, amount_basis, exclude_invoice, conversion_rate, pos_profile, company
		)
		results = []
		for item, result in zip(items, planned, strict=False):
			if result.applies:
				replaced = (
					_replaced_snapshot(item, signed_for)
					if result.competing_source not in (None, bd.SOURCE_NONE) and flt(result.competing_amount) > 0
					else None
				)
				suppressed_rules.update(_drop_competing_tags(item, result.competing_source))
				amount, rate = _stamp_friends_and_family(
					item,
					program.discount_percentage,
					amount_basis,
					line_amount=result.discount_amount if result.capped else None,
				)
				_set_item_attr(item, FIELD_FNF_REPLACED, replaced)
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
	FIELD_FNF_PERCENTAGE,
	FIELD_FNF_PROGRAM,
	FIELD_FNF_REPLACED,
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


def _scenario_b_lines(items, snapshots, fnf_like) -> list:
	"""Paid cart lines as they would be **without** F&F (scenario B), for the coupon engine.

	Each line gets back its pre-F&F state (``snapshots``); a line that is itself
	an F&F stamp goes back to list price with no discount.
	"""
	lines = []
	fnf_like = fnf_like or [False] * len(items)
	for item, snap, was_fnf in zip(items, snapshots, fnf_like, strict=False):
		if item.get("is_free_item"):
			continue
		line = frappe._dict(item.as_dict() if callable(getattr(item, "as_dict", None)) else dict(item))
		line.update(snap or {})
		if was_fnf or _is_fnf_stamped(line):
			line.discount_source = None
			line.discount_percentage = 0
			line.discount_amount = 0
			line.is_already_discounted = 0
			line[FIELD_DISCOUNT_SOURCE] = None
			price_list_rate = _line_price_list_rate(line)
			if price_list_rate:
				line.rate = price_list_rate
		line.amount = flt(line.get("rate")) * flt(line.get("qty"))
		lines.append(line)
	return lines


def _pos_coupon_value(
	coupon_code: str, base_net: float, base_grand: float, lines=None, tax_rate: float = 0
) -> tuple[float, float | None] | None:
	"""``(value, percentage)`` of an order-level POS Coupon (``None`` if unknown).

	With ``lines`` the value comes from :func:`best_discount_coupon.coupon_value_on`,
	the rule ``calculate_coupon_discount`` uses: out-of-scope lines and excluded brands
	do not count, already-discounted lines follow the coupon's ``promotion_conflict``,
	and a "Grand Total" coupon adds only the exclusive taxes (``tax_rate``). Without
	lines it falls back to the coupon % of the whole base.
	"""
	code = cstr(coupon_code).strip().upper()
	if not code or not frappe.db.table_exists("POS Coupon"):
		return None
	name = frappe.db.get_value("POS Coupon", {"coupon_code": code})
	if not name:
		return None
	row = frappe.get_cached_doc("POS Coupon", name)
	pct = flt(row.discount_percentage) if cstr(row.discount_type) == "Percentage" else None
	if lines:
		# Same value rule as validate_coupon / calculate_coupon_discount (cart = invoice).
		from posnext_promotions.promotions.best_discount_coupon import coupon_value_on

		value = coupon_value_on(row, lines, tax_rate)
	else:
		base = base_grand if cstr(row.apply_on) == "Grand Total" else base_net
		value = flt(base) * pct / 100.0 if pct is not None else flt(row.discount_amount)
	if flt(row.max_amount) and value > flt(row.max_amount):
		value = flt(row.max_amount)
		pct = None  # capped: the face % no longer describes it
	base = base_grand if cstr(row.apply_on) == "Grand Total" else base_net
	return max(0.0, min(value, flt(base))), pct


def _scenario_b_bases(doc, items, b_line_values) -> tuple[float, float, float]:
	"""``(net base, grand base, exclusive tax rate)`` of the invoice without F&F.

	Taxes included in the print rate are already inside the lines: only the
	exclusive tax rows are added (gap audit A I3).
	"""
	from posnext_promotions.api.coupon_engine import exclusive_tax_rate

	gross = sum(bd.line_gross(i) for i in items if not i.get("is_free_item"))
	base_net = max(0.0, gross - sum(b_line_values))
	tax_rate = exclusive_tax_rate(doc.get("taxes") or [])
	return base_net, base_net * (1.0 + tax_rate), tax_rate


def _coupon_without_fnf(doc, items, b_line_values, snapshots=None, fnf_like=None):
	"""``(value, percentage)`` of the invoice's POS Coupon without F&F; ``None`` if not one."""
	coupon_code = cstr(doc.get("coupon_code")).strip()
	if not coupon_code:
		return None
	base_net, base_grand, tax_rate = _scenario_b_bases(doc, items, b_line_values)
	lines = _scenario_b_lines(items, snapshots, fnf_like) if snapshots else None
	return _pos_coupon_value(coupon_code, base_net, base_grand, lines=lines, tax_rate=tax_rate)


def _order_candidate_without_fnf(doc, items, b_line_values, snapshots=None, fnf_like=None) -> bd.Candidate:
	"""Header discount the invoice would carry if F&F did not apply (scenario B)."""
	base_net, base_grand, _tax_rate = _scenario_b_bases(doc, items, b_line_values)
	pct = flt(doc.get("additional_discount_percentage") or 0)
	coupon_code = cstr(doc.get("coupon_code")).strip()
	value = _header_discount_value(doc)
	if coupon_code:
		coupon = _coupon_without_fnf(doc, items, b_line_values, snapshots, fnf_like)
		coupon_pct = None
		if coupon is not None:
			# The coupon engine is authoritative for a POS Coupon: it skips lines
			# that already carry a promotion, so the coupon never stacks on them.
			value = coupon[0] if snapshots else max(value, coupon[0])
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


def _is_pos_coupon(code) -> bool:
	code = cstr(code).strip().upper()
	return bool(code) and bool(frappe.db.table_exists("POS Coupon")) and bool(
		frappe.db.exists("POS Coupon", {"coupon_code": code})
	)


def _cap_header_at_coupon(doc, items, snapshots, b_line_values, fnf_like) -> bool:
	"""An unpaid invoice's header discount never exceeds its POS Coupon's value.

	Runs whether or not F&F applied: the client may send any header amount, the
	server prices the coupon. Never raises the header. True when the invoice's
	coupon is a POS Coupon. (Paid invoices never get here: see :func:`_freeze_paid_invoice`.)
	"""
	if not _is_pos_coupon(doc.get("coupon_code")):
		return False
	header = _header_discount_value(doc)
	if header <= 0 and not flt(doc.get("additional_discount_percentage")):
		return True
	coupon = _coupon_without_fnf(doc, items, b_line_values, snapshots, fnf_like)
	if coupon is not None and bd.money(header) > bd.money(coupon[0]):
		value = bd.money(coupon[0])
		doc.additional_discount_percentage = 0
		doc.discount_amount = value
		if doc.get("base_discount_amount"):
			doc.base_discount_amount = bd.money(value * (flt(doc.get("conversion_rate")) or 1.0))
	return True


def _drop_unused_coupon(doc, items) -> None:
	"""A POS Coupon that gives nothing (no header discount, no line tagged) is not used."""
	if _header_discount_value(doc) or flt(doc.get("additional_discount_percentage")):
		return
	if any(i.get("posnext_coupon_code") for i in items):
		return
	doc.coupon_code = None


def _resolve_order_level(doc, items, snapshots, b_line_values, ff_payload, fnf_like=None) -> dict | None:
	"""F&F lines vs an invoice header discount → keep the scenario that saves more.

	A = F&F (with per-line best-of) and **no** header discount.
	B = no F&F (lines keep their non-F&F discounts) **plus** the header discount.
	Tie → A (F&F). A POS Coupon's header is first capped at the coupon's value,
	and a POS Coupon left giving nothing is dropped (its usage must not count).
	"""
	pos_coupon = _cap_header_at_coupon(doc, items, snapshots, b_line_values, fnf_like)
	decision = _order_level_decision(doc, items, snapshots, b_line_values, ff_payload, fnf_like)
	if pos_coupon:
		_drop_unused_coupon(doc, items)
	return decision


def _grand_total_coupon_factor(doc, items, b_line_values) -> float:
	"""``1 + exclusive tax rate`` when the header is a "Grand Total" POS Coupon, else 1."""
	code = cstr(doc.get("coupon_code")).strip().upper()
	if not _is_pos_coupon(code):
		return 1.0
	if cstr(frappe.db.get_value("POS Coupon", {"coupon_code": code}, "apply_on")) != "Grand Total":
		return 1.0
	return 1.0 + _scenario_b_bases(doc, items, b_line_values)[2]


def _order_level_decision(doc, items, snapshots, b_line_values, ff_payload, fnf_like=None) -> dict | None:
	if not ff_payload.get("applied_count"):
		return None
	header_pct = flt(doc.get("additional_discount_percentage") or 0)
	header_amt = _header_discount_value(doc)
	if not header_pct and not header_amt:
		return None

	# Ruling 56: against a "Grand Total" POS Coupon (valued with its exclusive tax) the line
	# discounts count with the exclusive tax they also save — one unit on both sides.
	factor = _grand_total_coupon_factor(doc, items, b_line_values)
	a_total = sum(bd.line_discount_value(i, bd.AMOUNT_BASIS_UNIT) for i in items) * factor
	order_b = _order_candidate_without_fnf(doc, items, b_line_values, snapshots, fnf_like)
	capped = bool((ff_payload.get("caps") or {}).get("capped"))
	fnf_side = bd.Candidate(
		source=bd.SOURCE_FRIENDS_AND_FAMILY,
		amount=a_total,
		# A capped F&F is shown by its money value, not the program %.
		percentage=None if capped else (flt(ff_payload.get("discount_percentage")) or None),
	)
	other_side = bd.Candidate(
		source=order_b.source,
		amount=sum(b_line_values) * factor + order_b.amount,
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
			# Back to the pre-F&F line (incl. the promotion F&F had replaced);
			# strip only what is itself an F&F stamp.
			_restore_line(item, snap)
			if was_fnf or _is_fnf_stamped(item):
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


RETURN_WARNINGS_FLAG = "ff_return_warnings"


def _claims_fnf(row) -> bool:
	"""The client sent F&F markers on this row (not trusted, only used to decide on a warning)."""
	return (
		cstr(row.get(FIELD_DISCOUNT_SOURCE)) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY
		or bool(flt(row.get(FIELD_FNF_AMOUNT)))
		or flt(row.get(FIELD_FNF_PERCENTAGE)) > 0
	)


def _fnf_items_on(doc) -> set:
	"""Item codes that carried F&F on the invoice this return is against."""
	against = doc.get("return_against")
	doctype = cstr(doc.get("doctype"))
	if not against or doctype not in ff_ledger.RETURN_LINK_FIELDS:
		return set()
	Item = frappe.qb.DocType(f"{doctype} Item")
	return set(
		(
			frappe.qb.from_(Item)
			.select(Item.item_code)
			.where(
				(Item.parent == against)
				& (Item.parenttype == doctype)
				& (Item[FIELD_DISCOUNT_SOURCE] == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
			)
		).run(pluck=True)
	)


def _stamp_return_usage(doc, items) -> None:
	"""Credit-note rows give F&F allowance back, derived from the ORIGINAL row only.

	The amount comes from :func:`ff_ledger.return_credits` (the original row's
	recorded F&F amount, pro rata to the returned qty). The F&F markers are
	copied from the original row, so reports see the return; a row whose
	original carried no F&F gets them cleared, whatever the client sent.
	Rows that may have had F&F but have no usable original row are kept on
	``doc.flags`` for a log warning at submit (drafts are never logged).
	"""
	warnings = []
	fnf_items = None
	for result in ff_ledger.return_credits(doc):
		row, original = result.row, result.original
		claims = _claims_fnf(row)
		if original is not None:
			_set_item_attr(row, FIELD_DISCOUNT_SOURCE, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
			_set_item_attr(row, FIELD_FNF_PERCENTAGE, flt(original.fnf_percentage))
			_set_item_attr(row, FIELD_FNF_PROGRAM, original.fnf_program or None)
		else:
			if cstr(row.get(FIELD_DISCOUNT_SOURCE)) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
				_set_item_attr(row, FIELD_DISCOUNT_SOURCE, None)
			if flt(row.get(FIELD_FNF_PERCENTAGE)):
				_set_item_attr(row, FIELD_FNF_PERCENTAGE, 0)
			if row.get(FIELD_FNF_PROGRAM):
				_set_item_attr(row, FIELD_FNF_PROGRAM, None)
		_set_item_attr(row, FIELD_FNF_AMOUNT, flt(result.credit))
		if result.warning:
			if fnf_items is None:
				fnf_items = _fnf_items_on(doc)
			if claims or cstr(row.get("item_code")) in fnf_items:
				warnings.append(
					{
						"invoice_row": row.get("name"),
						"item_code": row.get("item_code"),
						"qty": flt(row.get("qty")),
						"reason": result.warning,
					}
				)
	flags = getattr(doc, "flags", None)
	if flags is not None:
		flags[RETURN_WARNINGS_FLAG] = warnings


def _remove_invoice_rows(doc, rows) -> None:
	drop = {id(row) for row in rows}
	kept = [row for row in doc.get("items") if id(row) not in drop]
	if isinstance(doc, dict):
		doc["items"] = kept
		return
	doc.set("items", kept)
	for position, row in enumerate(doc.get("items"), start=1):
		row.idx = position


def _enforce_invoice_gift_policy(doc, items) -> bool:
	"""Remove free rows the gift policy forbids. True when F&F should be re-run.

	A removed gift's trigger rows lose that gift's rule tag (the policy detaches it).
	A row whose discount came only from removed gift rules also loses that discount,
	so F&F can reclaim it, like ``apply_offers`` does after it drops a gift. A row
	that still carries other rules keeps its discount and those rules.
	"""
	free_rows = [row for row in items if cint(row.get("is_free_item"))]
	if not free_rows:
		return False
	# Rules on paid rows that still carry a non-F&F discount, before the policy detaches them.
	tagged = {
		id(row): set(gifts._item_rule_names(row))
		for row in items
		if not cint(row.get("is_free_item"))
		and not _is_fnf_stamped(row)
		and (flt(row.get("discount_percentage")) or flt(row.get("discount_amount")))
	}
	kept = list(free_rows)
	suppressed = set(ff_compatibility.enforce_gift_policy(items, kept, ff_config.resolve()))
	removed = [row for row in free_rows if all(row is not k for k in kept)]
	if not removed:
		return False
	_remove_invoice_rows(doc, removed)
	for row in doc.get("items"):
		if tagged.get(id(row), set()) & suppressed and not gifts._item_rule_names(row):
			gifts._clear_line_gift_stamp(row)
	return True


# ---- paid invoices (Task 6c) ------------------------------------------------

# Line reasons that mean "a scheme or gift owns this line": F&F on it is a compatibility breach.
_COMPATIBILITY_LINE_REASONS = frozenset({REASON_SCHEME_NOT_ELIGIBLE, REASON_PROTECTED})


def _row_copy(item) -> frappe._dict:
	return frappe._dict(item.as_dict() if callable(getattr(item, "as_dict", None)) else dict(item))


def _line_tolerance(item, unit: float) -> float:
	"""One currency unit per unit sold: saved rows hold a per-unit rate rounded to the currency."""
	return unit * max(1.0, abs(_line_qty(item)))


def _carried_fnf(item, value: float, uncapped: float, capped: float, tolerance: float) -> float:
	"""F&F the paid row carries, valued from its persisted prices (ERPNext order, as the ledger).

	A row with an F&F marker carries its whole discount as F&F. The POS marks F&F
	lines with ``posnext_discount_source``; a row without a marker (an older client, or
	a row the POS left unmarked) counts as F&F only when it has no Pricing Rule or
	coupon tag and its discount is what the program gives it (full or capped).
	Any other unmarked discount is a competitor the unpaid path would keep too.
	"""
	if value <= 0:
		return 0.0
	if _looks_like_fnf_stamp(item, None, bd.AMOUNT_BASIS_UNIT):
		return value
	if bd.has_pricing_rules(item) or item.get("posnext_coupon_code") or uncapped <= 0:
		return 0.0
	if abs(value - uncapped) <= tolerance or (capped > 0 and abs(value - capped) <= tolerance):
		return value
	return 0.0


def _stamp_paid_fnf_audit(item, amount: float, percentage: float) -> None:
	"""Audit markers only (ledger, log, reports). Prices and discounts stay as paid."""
	_set_item_attr(item, FIELD_DISCOUNT_SOURCE, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
	_set_item_attr(item, FIELD_FNF_AMOUNT, bd.money(amount))
	_set_item_attr(item, FIELD_FNF_PERCENTAGE, flt(percentage))
	_set_item_attr(item, FIELD_FNF_PROGRAM, None)


def _over_reason(payload: dict, line: dict) -> str:
	"""Why a row may not carry the F&F it carries."""
	if not payload.get("eligible"):
		return (
			ff_paid.PROGRAM_INACTIVE if payload.get("reason") == REASON_PROGRAM_INACTIVE else ff_paid.NOT_ELIGIBLE
		)
	if line.get("reason") in _COMPATIBILITY_LINE_REASONS:
		return ff_paid.COMPATIBILITY
	return ff_paid.OVER_DISCOUNT


def _disagreement(
	reason: str, item=None, carried: float = 0.0, uncapped: float = 0.0, capped: float = 0.0, **extra
) -> dict:
	entry = {
		"reason": reason,
		"carried": bd.money(carried),
		"entitled_uncapped": bd.money(uncapped),
		"entitled_capped": bd.money(capped),
		**extra,
	}
	if item is not None:
		entry.update(
			{"invoice_row": item.get("name"), "idx": item.get("idx"), "item_code": item.get("item_code")}
		)
	return entry


def _paid_header_disagreement(doc, items, carried_total: float, unit: float) -> dict | None:
	"""Header discount on a paid invoice: never stacked on F&F, never above the coupon's value.

	The coupon check runs whether or not F&F applies (as the unpaid cap does).
	"""
	header_amt = _header_discount_value(doc)
	header_pct = flt(doc.get("additional_discount_percentage") or 0)
	if header_amt <= 0 and header_pct <= 0:
		return None
	if carried_total > 0:
		return _disagreement(ff_paid.HEADER_DISCOUNT, carried=carried_total, header=bd.money(header_amt), stacked=1)
	if not cstr(doc.get("coupon_code")).strip():
		return None
	snapshots = [_snapshot_line(i) for i in items]
	b_line_values = [bd.line_discount_value(i, bd.AMOUNT_BASIS_UNIT) for i in items]
	coupon = _order_candidate_without_fnf(doc, items, b_line_values, snapshots, [False] * len(items))
	if header_amt > flt(coupon.amount) + unit:
		return _disagreement(
			ff_paid.HEADER_DISCOUNT,
			header=bd.money(header_amt),
			coupon_code=coupon.code,
			coupon_value=bd.money(coupon.amount),
		)
	return None


def _paid_throw_message(entry: dict) -> str:
	if entry["reason"] == ff_paid.HEADER_DISCOUNT:
		if entry.get("stacked"):
			return _(
				"The invoice discount {0} cannot be combined with the Friends & Family discount on its lines."
			).format(ff_money.fmt(entry["header"]))
		return _("The invoice discount {0} is more than coupon {1} gives ({2}).").format(
			ff_money.fmt(entry["header"]), entry.get("coupon_code"), ff_money.fmt(entry["coupon_value"])
		)
	if entry["reason"] == ff_paid.COMPATIBILITY:
		return _(
			"Row {0} ({1}): a promotion or gift on this line cannot be combined with Friends & Family, "
			"but the line carries a Friends & Family discount of {2}."
		).format(entry.get("idx"), entry.get("item_code"), ff_money.fmt(entry["carried"]))
	return _(
		"Row {0} ({1}): Friends & Family discount {2} is more than the {3} this customer is entitled to."
	).format(
		entry.get("idx"),
		entry.get("item_code"),
		ff_money.fmt(entry["carried"]),
		ff_money.fmt(entry["entitled_uncapped"]),
	)


def _freeze_paid_invoice(doc, items) -> None:
	"""Paid invoice: compare its F&F with the entitlement, never re-price it.

	The entitlement is computed read-only, uncapped and capped, on copies of the
	rows. Rows that carry F&F get the audit markers (for the usage ledger and the
	log); prices, discounts, totals and the header discount are never changed.

	* Offline (the customer has left): never throws; each disagreement goes to
	  ``doc.flags`` and is logged at submit (``Offline Disagreement``).
	* Online: more F&F than the uncapped entitlement, F&F on a line a scheme owns,
	  or a header discount that is stacked / above its coupon → throw, naming the
	  row. F&F above the capped entitlement is a cap breach: logged, and the
	  ledger applies ``on_cap_breach_at_submit`` at submit. Less F&F than the
	  entitlement is kept and logged (``paid_under_discount``).
	"""
	offline = ff_paid.is_offline(doc)
	copies = [_row_copy(i) for i in items]
	ff_compatibility.block_ineligible_lines(copies, doc=doc)
	kwargs = {
		"customer": doc.get("customer"),
		"items": copies,
		"posting_date": doc.get("posting_date") or nowdate(),
		"amount_basis": bd.AMOUNT_BASIS_UNIT,
		"exclude_invoice": doc.get("name") or None,
		"pos_profile": doc.get("pos_profile"),
		"conversion_rate": flt(doc.get("conversion_rate")) or 1.0,
		"company": doc.get("company"),
	}
	uncapped = compute_friends_and_family_discounts(ignore_caps=True, **kwargs)
	capped = compute_friends_and_family_discounts(**kwargs)
	percentage = flt(uncapped.get("discount_percentage"))
	unit = 10 ** -ff_money.precision_for(ff_money.current_currency())

	found = []
	lines = []
	carried_total = capped_total = within_total = tolerance_total = 0.0
	for item, u_line, c_line in zip(items, uncapped["items"], capped["items"], strict=False):
		if cint(item.get("is_free_item")):
			continue
		tolerance = _line_tolerance(item, unit)
		u_amt = flt(u_line["discount_amount"]) if u_line.get("applies") else 0.0
		c_amt = flt(c_line["discount_amount"]) if c_line.get("applies") else 0.0
		carried = _carried_fnf(item, bd.line_discount_value(item, bd.AMOUNT_BASIS_UNIT), u_amt, c_amt, tolerance)
		capped_total += c_amt
		lines.append((item, carried, u_amt, c_amt, tolerance))
		if carried <= 0:
			# Ruling 48: a row left at full price carries no F&F — drop stale client markers,
			# or the submit log would record a "Discount Applied" the row never gave.
			_clear_paid_fnf_markers(item)
			continue
		_stamp_paid_fnf_audit(item, carried, percentage or flt(item.get("discount_percentage")))
		carried_total += carried
		within_total += min(carried, u_amt)
		tolerance_total += tolerance
		if carried > u_amt + tolerance:
			found.append(_disagreement(_over_reason(uncapped, u_line), item, carried, u_amt, c_amt))

	if within_total > capped_total + max(tolerance_total, unit):
		found.append(
			_disagreement(ff_paid.CAP_EXCEEDED, carried=within_total, capped=capped_total, caps=capped.get("caps"))
		)
	elif carried_total < capped_total - unit:
		for item, carried, u_amt, c_amt, tolerance in lines:
			if carried < c_amt - tolerance:
				found.append(_disagreement(ff_paid.MISSING_FNF, item, carried, u_amt, c_amt))

	header = _paid_header_disagreement(doc, items, carried_total, unit)
	if header:
		found.append(header)
	found.extend(_paid_gift_disagreements(items, {id(line[0]) for line in lines if line[1] > 0}))

	if not offline:
		for entry in found:
			entry["reason"] = ff_paid.online_reason(entry["reason"])
		# A header stacked on F&F rows is logged, not thrown: the cart may have sent
		# both, and the unpaid path used to resolve that silently.
		blocking = [e for e in found if e["reason"] in ff_paid.ONLINE_BLOCKING and not e.get("stacked")]
		if blocking:
			frappe.throw(
				"<br>".join(_paid_throw_message(entry) for entry in blocking)
				+ "<br>"
				+ _("The invoice is already paid, so it was not re-priced. Correct the cart and take payment again."),
				title=_("Friends & Family discount not allowed"),
			)

	flags = getattr(doc, "flags", None)
	if flags is not None:
		# Not the entitlement payload: the submit log then records what the rows carry.
		flags.friends_and_family = None
		flags[ff_paid.DISAGREEMENTS_FLAG] = {"offline": offline, "entries": found}


def _paid_gift_disagreements(items, fnf_ids: set) -> list:
	"""Gifts the gift policy forbids next to this paid invoice's F&F: reported, never removed.

	The policy runs on copies, keyed on the rows that carry F&F (the POS marks them with
	``posnext_discount_source``, and free rows keep their rule names; rows from older clients
	carry no marker, so the default "stamped now" rows would miss them). Each free row it
	would drop, and each F&F row whose gift stamp it would clear, is one disagreement.
	"""
	if not fnf_ids:
		return []
	copies = [_row_copy(i) for i in items]
	keys = ff_compatibility.row_key_list(copies)
	fnf_keys = {key for key, item in zip(keys, items, strict=True) if id(item) in fnf_ids}
	kept = [c for c in copies if cint(c.get("is_free_item"))]
	suppressed = ff_compatibility.enforce_gift_policy(copies, kept, ff_config.resolve(), fnf_keys=fnf_keys)
	if not suppressed:
		return []
	found = []
	for item, copy in zip(items, copies, strict=True):
		if cint(copy.get("is_free_item")):
			gone = all(copy is not k for k in kept)
		else:
			gone = (flt(copy.get("discount_amount")), flt(copy.get("discount_percentage"))) != (
				flt(item.get("discount_amount")),
				flt(item.get("discount_percentage")),
			)
		if gone:
			found.append(_disagreement(ff_paid.GIFT_WITH_FNF, item, rules=suppressed))
	return found


def _clear_paid_fnf_markers(item) -> None:
	"""Remove F&F markers only (audit fields and the cart source tag); prices stay as paid."""
	_clear_fnf_audit_fields(item)
	if cstr(item.get("discount_source")) == DISCOUNT_SOURCE_FRIENDS_AND_FAMILY:
		_set_item_attr(item, "discount_source", None)


def _loyalty_total(doc) -> float:
	"""The total ERPNext checks a redemption against (``validate_loyalty_points``)."""
	disabled = getattr(doc, "is_rounded_total_disabled", None)
	if callable(disabled):
		rounding_off = disabled()
	else:  # plain dict invoice: same rule from its own field / Global Defaults
		rounding_off = (
			doc.get("disable_rounded_total")
			if "disable_rounded_total" in doc
			else frappe.db.get_single_value("Global Defaults", "disable_rounded_total")
		)
	return flt(doc.get("grand_total") if cint(rounding_off) else doc.get("rounded_total"))


def _check_loyalty_redemption(doc) -> None:
	"""Re-run ERPNext's redemption limit after the F&F step (Ruling 47).

	Only when points are redeemed, against the same total and with the same
	comparison as ERPNext, so nothing ERPNext accepts is rejected here.
	"""
	if not cint(doc.get("redeem_loyalty_points")):
		return
	if flt(doc.get("loyalty_amount")) > _loyalty_total(doc):
		frappe.throw(_("Loyalty redemption exceeds the total after the Friends & Family discount."))


@ff_money.in_document_currency
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

	  5. A paid invoice (:func:`ff_paid.is_paid`) is never re-priced: see
	     :func:`_freeze_paid_invoice`. After the F&F step the loyalty redemption
	     is re-checked against the grand total (not on offline sync).

	Returns / credit notes only get their (negative) F&F usage stamped, from
	the original rows. Consolidated invoices are left untouched.

	Skips during install / migrate / patch.
	"""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
		return
	# Consolidated invoices merge POS Invoices that were already decided and recorded.
	if cint(doc.get("is_consolidated")):
		return
	items = doc.get("items")
	if not items:
		return

	if cint(doc.get("is_return")):
		_stamp_return_usage(doc, items)
		return

	# Only POS / point-of-sale invoices for now — the program is a POS benefit.
	# Use .get("items") — on frappe._dict, .items is dict.items.
	is_pos_doc = cstr(doc.get("doctype")) == "POS Invoice" or cint(doc.get("is_pos"))
	if not is_pos_doc:
		return

	if ff_paid.is_paid(doc):
		# Never re-price a paid invoice (offline sync or payment collected).
		_freeze_paid_invoice(doc, items)
		if not ff_paid.is_offline(doc):
			_check_loyalty_redemption(doc)
		return None

	before = _invoice_state(doc, items)
	# Re-open earlier decisions: lines get back the discount F&F replaced.
	for item in items:
		_restore_replaced_discount(item, doc)
	# Buy X Get Y / Routine lines get no F&F (same rule as the cart).
	ff_compatibility.block_ineligible_lines(items, doc=doc)
	snapshots = [_snapshot_line(i) for i in items]
	posting_date = doc.get("posting_date") or nowdate()

	# Scenario-B line values: what each line saves without F&F.
	pos_profile = doc.get("pos_profile")
	# Caps are company currency (ledger base_amount); this invoice may be in another.
	conversion_rate = flt(doc.get("conversion_rate")) or 1.0
	program = get_customer_eligibility(doc.get("customer"), posting_date, pos_profile)
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
		pos_profile=pos_profile,
		conversion_rate=conversion_rate,
		company=doc.get("company"),
	)
	order_decision = _resolve_order_level(doc, items, snapshots, b_line_values, ff_payload, fnf_like)
	# Gift policy after the order-level decision, as on the cart: a gift goes only when
	# its trigger line kept F&F. The invoice never adds gifts, it only removes them.
	rerun = _enforce_invoice_gift_policy(doc, items)
	if rerun:
		items = doc.get("items")
		ff_payload = apply_friends_and_family_to_items(
			items,
			customer=doc.get("customer"),
			posting_date=posting_date,
			amount_basis=bd.AMOUNT_BASIS_UNIT,
			exclude_invoice=doc.get("name") or None,
			pos_profile=pos_profile,
			conversion_rate=conversion_rate,
			company=doc.get("company"),
		)
	items = doc.get("items")
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
	if _invoice_state(doc, doc.get("items")) != before and callable(recalc):
		recalc()
	_check_loyalty_redemption(doc)
	return ff_payload
