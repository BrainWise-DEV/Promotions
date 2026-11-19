# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""F&F compatibility with schemes and gifts, shared by the cart and the invoice.

The cart (``apply_offers``) and the invoice ``validate`` hook used to apply these
rules differently: the invoice never looked at gifts. Both now go through:

* :func:`normalize` — one :class:`GiftSnapshot` for cart lines + ``free_items_map``
  or invoice rows (paid rows and ``is_free_item`` rows). Lines are keyed by
  ``row_key`` (row ``name``, else ``idx``, else the row object), never list position.
* :func:`block_ineligible_lines` — Buy X Get Y / Routine lines get
  ``fnf_blocked_by_scheme`` before F&F runs, only by rules the server finds
  eligible on the line (:func:`rule_eligible_on_line`, Ruling 119).
* :func:`enforce_gift_policy` — with *Allow Free Gift with F&F* off, drop gifts whose
  trigger line got F&F (a cart-total gift goes when F&F applied anywhere).
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field

import frappe
from frappe.utils import cint, cstr, flt, getdate, nowdate

# Module import (not ``from … import``): tests patch ``gifts._rule_scope_matches``.
from posnext_promotions.promotions import ff_config
from posnext_promotions.promotions import ff_constants as const
from posnext_promotions.promotions import schedule
from posnext_promotions.promotions import friends_and_family_gifts as gifts
from posnext_promotions.promotions.ff_classifier import classify_promotion
from posnext_promotions.promotions.friends_and_family_caps import FIELD_DISCOUNT_SOURCE
from posnext_promotions.promotions.friends_and_family_gifts import DISCOUNT_SOURCE_FRIENDS_AND_FAMILY

# Settings.unclassified_promotion_policy option that blocks F&F on the line.
POLICY_BLOCK_FNF = const.UNCLASSIFIED_POLICIES[1]


@dataclass
class Line:
	row_key: object
	item_code: str
	qty: float
	pricing_rules: list
	is_free_item: bool
	fnf_stamped: bool
	row: object = field(repr=False, compare=False)


@dataclass
class Gift:
	rule_name: str
	item_code: str
	qty: float
	trigger_keys: tuple
	# ``free_items_map`` key on the cart, the free row itself on the invoice.
	ref: object = field(repr=False, compare=False)


@dataclass
class GiftSnapshot:
	lines: dict  # row_key -> Line, in item order
	gifts: list

	def paid_lines(self):
		return [line for line in self.lines.values() if not line.is_free_item]

	def fnf_keys(self) -> set:
		return {line.row_key for line in self.paid_lines() if line.fnf_stamped}


def is_fnf_stamped(item) -> bool:
	return DISCOUNT_SOURCE_FRIENDS_AND_FAMILY in (
		cstr(item.get("discount_source")),
		cstr(item.get(FIELD_DISCOUNT_SOURCE)),
	)


def row_key_list(items) -> list:
	"""One key per row: ``name``, else ``idx``, else the row's identity (duplicates fall back too)."""
	keys, seen = [], set()
	for item in items or []:
		key = cstr(item.get("name")) or cint(item.get("idx")) or None
		if key is None or key in seen:
			key = ("row", id(item))
		seen.add(key)
		keys.append(key)
	return keys


def _gift_rule_name(row) -> str:
	"""The gift rule behind a free row: the first gift / BXGY rule, else the first rule."""
	names = gifts._split_rule_names(row.get("pricing_rules"))
	for name in names:
		if gifts.classify_gift_rule(name):
			return name
	return names[0] if names else ""


def _trigger_keys(rule_name, lines) -> tuple:
	if not rule_name:
		return ()
	return tuple(
		line.row_key
		for line in lines
		if rule_name in line.pricing_rules or gifts._rule_scope_matches(line.row, rule_name)
	)


def normalize(items, free_rows) -> GiftSnapshot:
	"""One shape for cart and invoice. ``free_rows``: cart ``free_items_map`` (or its values)
	or the invoice rows with ``is_free_item=1``.
	"""
	lines = {}
	for key, item in zip(row_key_list(items), items or [], strict=True):
		lines[key] = Line(
			row_key=key,
			item_code=cstr(item.get("item_code")),
			qty=gifts._line_qty(item),
			pricing_rules=gifts._item_rule_names(item),
			is_free_item=bool(cint(item.get("is_free_item"))),
			fnf_stamped=is_fnf_stamped(item),
			row=item,
		)
	paid = [line for line in lines.values() if not line.is_free_item]

	if isinstance(free_rows, Mapping):
		entries = [
			(ref, gifts._rule_name_from_free_key(ref) or _gift_rule_name(row), row) for ref, row in free_rows.items()
		]
	else:
		entries = [(row, _gift_rule_name(row), row) for row in free_rows or []]

	gift_list = [
		Gift(
			rule_name=rule_name,
			item_code=cstr(row.get("item_code")),
			qty=gifts._line_qty(row),
			trigger_keys=_trigger_keys(rule_name, paid),
			ref=ref,
		)
		for ref, rule_name, row in entries
	]
	return GiftSnapshot(lines=lines, gifts=gift_list)


# ---- BXGY / Routine -------------------------------------------------------


def _profile_values(pos_profile) -> dict:
	if not pos_profile or not frappe.db.exists("POS Profile", pos_profile):
		return {}
	return frappe.get_cached_value(
		"POS Profile",
		pos_profile,
		["ignore_pricing_rule", "warehouse", "selling_price_list", "customer", "company"],
		as_dict=True,
	) or {}


def _rule_applies_to_doc(rule, doc, profile: dict) -> bool:
	"""Applicability that does not depend on which offers the client selected.

	POS profile: a rule restricted to a warehouse or a price list applies only to a
	profile with that warehouse / price list; an unrestricted rule applies to every
	profile. Customer / Customer Group / Territory rules must match the doc's customer
	(groups and territories match their descendants too); any other ``applicable_for``
	is not applied here. A one-time-per-customer rule never applies to an anonymous
	sale (no customer / the profile's default customer) or to a customer who already
	redeemed it, as ``apply_offers`` decides for its own rule map.
	"""
	if cint(rule.get("one_time_per_customer")):
		customer = cstr(doc.get("customer"))
		if not customer or customer == cstr(profile.get("customer")):
			return False
		if frappe.db.exists("One Time Customer Offer Usage", {"customer": customer, "pricing_rule": rule.get("name")}):
			return False
	warehouse = cstr(rule.get("warehouse"))
	if warehouse and profile.get("warehouse") and warehouse != cstr(profile.get("warehouse")):
		return False
	price_list = cstr(rule.get("for_price_list"))
	doc_price_list = cstr(doc.get("selling_price_list") or profile.get("selling_price_list"))
	if price_list and doc_price_list and price_list != doc_price_list:
		return False
	applicable_for = cstr(rule.get("applicable_for"))
	if applicable_for:
		fieldname = frappe.scrub(applicable_for)
		if fieldname not in ("customer", "customer_group", "territory"):
			return False
		wanted = cstr(rule.get(fieldname))
		have = cstr(doc.get(fieldname))
		if not have and fieldname != "customer" and doc.get("customer"):
			have = cstr(frappe.get_cached_value("Customer", doc.get("customer"), fieldname))
		if wanted and not _matches(fieldname, wanted, have):
			return False
	return True


def _matches(fieldname: str, wanted: str, have: str) -> bool:
	"""Exact for Customer; Customer Group / Territory also match a descendant of ``wanted``."""
	if wanted == have:
		return True
	if fieldname == "customer" or not have:
		return False
	from frappe.utils.nestedset import get_descendants_of

	return have in get_descendants_of(frappe.unscrub(fieldname), wanted, ignore_permissions=True)


def rule_valid_on(rule, posting_date) -> bool:
	"""``posting_date`` is inside the rule's ``valid_from`` … ``valid_upto`` (either may be blank)."""
	posting_date = getdate(posting_date)
	return (not rule.get("valid_from") or getdate(rule.get("valid_from")) <= posting_date) and (
		not rule.get("valid_upto") or getdate(rule.get("valid_upto")) >= posting_date
	)


def rule_fields() -> list[str]:
	"""Pricing Rule fields :func:`rule_applies_to_doc` reads."""
	fields = [
		"name",
		"disable",
		"selling",
		"coupon_code_based",
		"company",
		"valid_from",
		"valid_upto",
		"applicable_for",
		"customer",
		"customer_group",
		"territory",
		"for_price_list",
		"warehouse",
	]
	if frappe.get_meta("Pricing Rule").has_field("one_time_per_customer"):
		fields.append("one_time_per_customer")
	if schedule.schedule_fields_available():
		fields += [schedule.FROM_TIME_FIELD, schedule.TO_TIME_FIELD]
	return fields


def _doc_coupon_is_for(rule, doc) -> bool:
	"""The doc's header ``coupon_code`` resolves to ``rule`` (ERPNext's Coupon Code → pricing_rule lookup)."""
	code = cstr(doc.get("coupon_code"))
	return bool(code) and frappe.db.get_value("Coupon Code", code, "pricing_rule") == rule.get("name")


def rule_applies_to_doc(rule, doc, profile: dict | None = None) -> bool:
	"""``rule`` (a row with :func:`rule_fields`) applies to ``doc`` at its posting date.

	Enabled and selling; a coupon-based rule only while the doc's ``coupon_code``
	is that rule's coupon (Ruling 26); the doc's POS profile does not ignore
	pricing rules; same company (the doc's, else its profile's); valid on
	``posting_date`` and live at the doc's posting moment (Active From / To Time,
	:mod:`schedule`); and :func:`_rule_applies_to_doc` (profile warehouse / price
	list, customer / group / territory tree, one-time redemption).
	"""
	if profile is None:
		profile = _profile_values(doc.get("pos_profile"))
	if cint(rule.get("disable")) or not cint(rule.get("selling")):
		return False
	if cint(rule.get("coupon_code_based")) and not _doc_coupon_is_for(rule, doc):
		return False
	if cint(profile.get("ignore_pricing_rule")):
		return False
	company = cstr(doc.get("company") or profile.get("company"))
	if rule.get("company") and company and cstr(rule.get("company")) != company:
		return False
	if not rule_valid_on(rule, doc.get("posting_date") or nowdate()):
		return False
	# Date-only rules are settled by the posting date above; a rule whose ends carry
	# a time is judged at the doc's posting moment (now for a live cart).
	if schedule.has_time_bound(rule) and not schedule.is_active_at(rule, schedule.resolve_moment(doc)):
		return False
	return _rule_applies_to_doc(rule, doc, profile)


def blocking_rule_map(doc=None) -> dict:
	"""The one rule source for BXGY / Routine blocking, shared by cart and invoice (Ruling 19).

	Every blocking-kind rule that is enabled, selling, valid at the doc's posting
	moment for its company and applicable to its POS profile, whether or not the
	client selected the offer. An untagged line can still sit under a BXGY rule
	(above ``max_qty`` ERPNext tags nothing), so the cart blocks it by scope + ``min_qty``
	and the invoice must agree.

	Without a doc (or a company): every such rule valid today, any company — the
	no-doc judge fails closed, never narrower than the doc judge (Ruling 122).
	"""
	profile = _profile_values(doc.get("pos_profile")) if doc is not None else {}
	if cint(profile.get("ignore_pricing_rule")):
		return {}
	company = (doc.get("company") or profile.get("company")) if doc is not None else None
	fields = rule_fields()
	base = {"disable": 0, "selling": 1, "coupon_code_based": 0}
	if company:
		base["company"] = company
	# NOTE (cost): up to three Pricing Rule queries per cart apply / invoice validate.
	# ponytail: cache per company + day — Phase 1 Task 6f.
	rows = frappe.get_all(
		"Pricing Rule", filters={**base, "price_or_product_discount": "Product", "same_item": 1}, fields=fields
	)
	if frappe.get_meta("Pricing Rule").has_field("promotion_type"):
		rows += frappe.get_all(
			"Pricing Rule", filters={**base, "promotion_type": gifts.PROMOTION_TYPE_ITEM_LEVEL}, fields=fields
		)
	if frappe.get_meta("Pricing Rule").has_field("min_or_max_discount_qty_limit"):
		# Price "buy one" (gifts.is_price_bxgy_rule narrows it to the free-unit rules).
		rows += frappe.get_all(
			"Pricing Rule",
			filters={
				**base,
				"price_or_product_discount": "Price",
				"apply_discount_on_price": ["in", ["Min", "Max"]],
				"min_or_max_discount_qty_limit": [">", 0],
			},
			fields=fields,
		)

	def applies(row) -> bool:
		if doc is None:
			return rule_valid_on(row, nowdate())
		return rule_applies_to_doc(row, doc, profile)

	names = dict.fromkeys(row.name for row in rows if applies(row) and gifts.scheme_blocking_kind(row.name))
	return {name: gifts._rule_values(name) for name in names}


# Rules ERPNext qualifies on the whole cart or the customer's history (mixed_conditions
# sums the rule's lines, is_cumulative adds past invoices, apply_rule_on_other prices
# another item). One line cannot judge them, so their tag keeps blocking (Ruling 121).
CART_WIDE_FIELDS = ("mixed_conditions", "is_cumulative", "apply_rule_on_other")

_APPLY_ON_TABLE = {
	"Item Code": ("items", "item_code"),
	"Item Group": ("item_groups", "item_group"),
	"Brand": ("brands", "brand"),
}


def _uom_factor(item_code, uom) -> float:
	from erpnext.stock.get_item_details import get_conversion_factor

	return flt(get_conversion_factor(item_code, uom).get("conversion_factor")) or 1.0


def _line_factor(item) -> float:
	"""Stock units per line unit: the Item's factor for the line UOM, never below the one sent."""
	sent = flt(item.get("conversion_factor")) or 1.0
	uom, item_code = cstr(item.get("uom")), cstr(item.get("item_code"))
	if not uom or not item_code:
		return sent
	return max(sent, _uom_factor(item_code, uom))


def _rule_uoms(item, rule_name) -> set[str]:
	"""UOMs of the rule rows that cover this line ("" = any UOM); ``{""}`` when unknown."""
	try:
		rule_doc = frappe.get_cached_doc("Pricing Rule", rule_name)
	except frappe.DoesNotExistError:
		return {""}
	table, field = _APPLY_ON_TABLE.get(cstr(rule_doc.get("apply_on")), (None, None))
	if not table:
		return {""}
	value = cstr(item.get(field))
	return {cstr(row.get("uom")) for row in rule_doc.get(table) or [] if cstr(row.get(field)) == value} or {""}


def line_meets_rule(item, rule, rule_uoms=None) -> bool:
	"""ERPNext's per-line thresholds (``filter_pricing_rules_for_qty_amount``).

	Stock qty (qty × the line's UOM factor) ≥ ``min_qty`` × the factor of the rule
	row's UOM, and qty × price list rate (else rate) ≥ ``min_amt`` × that factor
	(× 1 when the rule row's UOM is the line's). A rule row with a UOM covers only
	lines in that UOM, as in ERPNext's rule lookup. ``rule_uoms``: :func:`_rule_uoms`.

	No ``max_qty`` / ``max_amt``: above them ERPNext tags nothing, yet the line is
	still the scheme's (capped B1G1 at qty 4 stays blocked).
	"""
	qty = gifts._line_qty(item)
	if qty <= 0:
		return False
	uoms = rule_uoms or {""}
	line_uom = cstr(item.get("uom"))
	stock_qty = qty * _line_factor(item)
	amount = qty * flt(item.get("price_list_rate") or item.get("rate"))
	if "" in uoms:
		qty_factor = amt_factor = 1.0
	elif not line_uom:
		# No line UOM (ERPNext then matches every row): the smallest rule factor, so it blocks.
		qty_factor = amt_factor = min(_uom_factor(item.get("item_code"), uom) for uom in uoms)
	elif line_uom in uoms:
		qty_factor, amt_factor = _line_factor(item), 1.0
	else:
		return False
	return stock_qty >= flt(rule.get("min_qty")) * qty_factor and amount >= flt(rule.get("min_amt")) * amt_factor


def _eligibility_fields() -> list[str]:
	return [*rule_fields(), "min_qty", "min_amt", *CART_WIDE_FIELDS]


def _eligibility_row(rule_name, rule_map=None):
	row = frappe.get_cached_value("Pricing Rule", rule_name, _eligibility_fields(), as_dict=True)
	return row or (rule_map or {}).get(rule_name)


def is_cart_wide(rule) -> bool:
	return any(cstr(rule.get(field)) not in ("", "0") for field in CART_WIDE_FIELDS)


def rule_eligible_on_line(item, rule_name, doc=None, profile=None, rule_map=None) -> bool:
	"""``rule_name`` applies to this line as it is now (Ruling 119).

	A rule name on the line is a client tag, not a fact: it may be stale (the
	qty dropped below ``min_qty``, the rule expired, a held draft). With ``doc``:
	:func:`rule_applies_to_doc` (enabled, dates / times, company, profile,
	customer tree, one-time). Without one: enabled and valid today. Either way the
	line meets the rule's thresholds (:func:`line_meets_rule`) and is in its scope.
	``rule_map`` supplies the row of a rule that is not in the database (unit tests).
	"""
	rule = _eligibility_row(rule_name, rule_map)
	if not rule:
		return False
	if doc is not None:
		if not rule_applies_to_doc(rule, doc, profile):
			return False
	elif cint(rule.get("disable")) or not rule_valid_on(rule, nowdate()):
		return False
	return line_meets_rule(item, rule, _rule_uoms(item, rule_name)) and gifts._rule_scope_matches(
		item, rule_name
	)


def tag_blocks(item, rule_name, doc=None, profile=None, rule_map=None) -> bool:
	"""A BXGY / Routine rule tagged on the line blocks F&F (Rulings 119, 121).

	A cart-wide / history rule (:data:`CART_WIDE_FIELDS`) blocks on the tag alone:
	fail closed, no F&F. Any other rule only while :func:`rule_eligible_on_line`.
	"""
	rule = _eligibility_row(rule_name, rule_map)
	if rule and is_cart_wide(rule):
		return True
	return rule_eligible_on_line(item, rule_name, doc, profile, rule_map)


# ---- The judge's verdict --------------------------------------------------
# ``fnf_blocked_by_scheme`` is a plain line field the client can send. The judge
# signs its verdict with a per-process key, bound to the line's item, qty, UOM and
# rules; :func:`judged_verdict` trusts only that. Anything else is judged again.

FIELD_JUDGED = "_fnf_scheme_judged"
_JUDGE_KEY = secrets.token_bytes(32)


def _verdict_mac(item, verdict: str) -> str:
	message = "|".join(
		(
			cstr(item.get("item_code")),
			f"{gifts._line_qty(item):.6f}",
			cstr(item.get("uom")),
			f"{flt(item.get('conversion_factor')):.6f}",
			",".join(sorted(gifts._item_rule_names(item))),
			cstr(verdict),
		)
	)
	return hmac.new(_JUDGE_KEY, message.encode(), hashlib.sha256).hexdigest()


def _set_verdict(item, names: list[str]) -> None:
	verdict = ",".join(names)
	gifts._set(item, gifts.FIELD_FNF_BLOCKED_BY, verdict)
	gifts._set(item, FIELD_JUDGED, _verdict_mac(item, verdict))


def strip_verdict_marks(rows) -> None:
	"""Drop the judge's signature from rows going back to a client (Ruling 122)."""
	for row in rows or []:
		if isinstance(row, dict):
			row.pop(FIELD_JUDGED, None)


def without_verdict_marks(fn):
	"""Decorator: strip :data:`FIELD_JUDGED` from a response's ``items`` / ``free_items``."""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		result = fn(*args, **kwargs)
		if isinstance(result, dict):
			strip_verdict_marks(result.get("items"))
			strip_verdict_marks(result.get("free_items"))
		return result

	return wrapper


def judged_verdict(item) -> list[str] | None:
	"""The judge's rule names for this line, or None when it was not judged as it is now."""
	verdict, mac = item.get(gifts.FIELD_FNF_BLOCKED_BY), item.get(FIELD_JUDGED)
	if verdict is None or not mac or not hmac.compare_digest(cstr(mac), _verdict_mac(item, cstr(verdict))):
		return None
	return gifts._split_rule_names(verdict)


def _load_rule_map(items, doc=None) -> dict:
	"""Rules tagged on the rows plus :func:`blocking_rule_map`."""
	rule_map = {}
	for item in items or []:
		for name in gifts._item_rule_names(item):
			if name not in rule_map:
				values = gifts._rule_values(name)
				if values:
					rule_map[name] = values
	for name, values in blocking_rule_map(doc).items():
		rule_map.setdefault(name, values)
	return rule_map


def block_ineligible_lines(items, rule_map=None, *, doc=None) -> set[int]:
	"""Flag paid lines under a BXGY / Routine scheme (``fnf_blocked_by_scheme``) before F&F.

	The one judge for the cart (``apply_offers``, the F&F-only call) and invoice
	validate (spec §7). A line is blocked only by a rule the server finds eligible
	on it now (:func:`rule_eligible_on_line`, Ruling 119):

	* a BXGY / Routine rule tagged in ``pricing_rules`` (:func:`tag_blocks`) — a
	  stale tag (qty below ``min_qty``, expired, another customer's) does not block;
	  a cart-wide / history rule's tag always does (Ruling 121);
	* an untagged BXGY rule whose scope covers the line (above ``max_qty`` ERPNext
	  tags nothing). BXGY free units are valued *after* F&F, so a paid line can
	  look undiscounted at that point.

	Settings ``unclassified_promotion_policy`` = "Block F&F on line": a line carrying a
	rule the classifier cannot place is blocked too ("Compete": it competes as before).
	The flag overwrites whatever the client sent and is signed (:func:`judged_verdict`),
	so :func:`gifts.line_blocked_by_scheme` trusts only the server's verdict.

	``rule_map=None`` (cart and invoice): rules named on the rows plus
	:func:`blocking_rule_map` for ``doc``. Returns the list positions flagged.
	"""
	if rule_map is None:
		rule_map = _load_rule_map(items, doc)
	rule_map = rule_map or {}
	profile = _profile_values(doc.get("pos_profile")) if doc is not None else None
	bxgy_rules = [
		name for name, row in rule_map.items() if gifts.scheme_blocking_kind(row or name) == gifts.SCHEME_KIND_BXGY
	]
	block_unclassified = ff_config.resolve().unclassified_promotion_policy.value == POLICY_BLOCK_FNF
	flagged: set[int] = set()
	for position, item in enumerate(items or []):
		if cint(item.get("is_free_item")):
			_set_verdict(item, [])
			continue
		tagged = gifts._item_rule_names(item)
		names: list[str] = []
		for rule_name in tagged:
			rule = rule_map.get(rule_name) or rule_name
			if gifts.scheme_blocking_kind(rule):
				if tag_blocks(item, rule_name, doc, profile, rule_map):
					names.append(rule_name)
			elif block_unclassified and _is_unclassified(rule):
				names.append(rule_name)
		for rule_name in bxgy_rules:
			if rule_name not in tagged and rule_eligible_on_line(item, rule_name, doc, profile, rule_map):
				names.append(rule_name)
		_set_verdict(item, names)
		if names:
			flagged.add(position)
	return flagged


def _is_unclassified(rule) -> bool:
	"""An existing rule the classifier cannot place (a missing rule name is not one)."""
	return gifts._rule_values(rule) is not None and classify_promotion(rule) == "unclassified"


# ---- Gifts ---------------------------------------------------------------


def _decide_gifts(snapshot: GiftSnapshot, fnf_keys: set, rule_map: dict):
	"""Returns ``(suppressed rule names, dropped gifts)``. BXGY is never dropped: with the
	setting off its lines get no F&F (``gifts.scheme_blocking_kind``).

	The caller has already checked that free gifts are not allowed with F&F.
	"""
	if not fnf_keys:
		return set(), []

	suppressed: set[str] = set()
	dropped: list[Gift] = []
	for gift in snapshot.gifts:
		rule_name = gift.rule_name
		kind = (
			gifts.classify_gift_rule(rule_map.get(rule_name) or rule_name) if rule_name else gifts.GIFT_KIND_FREE_ITEM
		)
		if kind not in gifts.CONFIGURABLE_GIFT_KINDS:
			continue
		if kind == gifts.GIFT_KIND_CART_TOTAL or not rule_name or set(gift.trigger_keys) & fnf_keys:
			dropped.append(gift)
			if rule_name:
				suppressed.add(rule_name)

	# GWP / free-unit stamps placed on a line that had F&F (cart gift passes run after F&F).
	for key in fnf_keys:
		line = snapshot.lines.get(key)
		if line is None or line.is_free_item:
			continue
		item = line.row
		if not gifts._line_has_gift_stamp(item):
			continue
		kind_of = {n: gifts.classify_gift_rule(rule_map.get(n) or n) for n in gifts._item_rule_names(item)}
		kinds = set(kind_of.values())
		kinds.discard(None)
		gift_rules = [n for n, kind in kind_of.items() if kind in gifts.CONFIGURABLE_GIFT_KINDS]
		if not kinds and cstr(item.get("discount_source")) in gifts._GIFT_LINE_SOURCES:
			# The stamp says "gift" but no rule on the line classifies as one: the gift's
			# rule is one that no longer resolves (deleted). Known rules (a Price rule) stay.
			kinds = {gifts.GIFT_KIND_FREE_ITEM}
			gift_rules = [n for n in kind_of if gifts._rule_values(rule_map.get(n) or n) is None]
		if kinds & gifts.CONFIGURABLE_GIFT_KINDS and gifts.GIFT_KIND_BXGY not in kinds:
			# Only the gift rules go; a Price rule on the same line stays attached.
			suppressed.update(gift_rules)
			gifts._clear_line_gift_stamp(item, gift_rules)

	if suppressed:
		# Detach the dropped gift rules from their trigger lines so the POS does
		# not show them as applied without a gift.
		for line in snapshot.paid_lines():
			names = gifts._item_rule_names(line.row)
			kept = [n for n in names if n not in suppressed]
			if len(kept) != len(names):
				gifts._set(line.row, "pricing_rules", ",".join(kept))
			line.pricing_rules = kept
	return suppressed, dropped


def enforce_gift_policy(
	items,
	free_rows,
	config=None,
	*,
	fnf_keys=None,
	rule_map: dict | None = None,
	applied_rules: set | None = None,
) -> list[str]:
	"""Drop gifts that may not coexist with F&F (Allow Free Gift with F&F off).

	``free_rows``: the cart ``free_items_map`` (dropped keys are popped) or a list of
	invoice free rows (dropped rows are removed from that list; the caller removes them
	from ``doc.items``). ``fnf_keys``: row keys holding F&F *before* the cart gift passes
	ran; default = rows F&F-stamped now. ``applied_rules`` loses rules no gift still uses.
	Returns the suppressed Pricing Rule names, sorted.
	"""
	config = config or ff_config.resolve()
	if config.allow_free_gift_with_ff.value:
		return []
	fnf_keys = set(fnf_row_keys(items) if fnf_keys is None else fnf_keys)
	if not fnf_keys:
		return []
	snapshot = normalize(items, free_rows)
	if not isinstance(free_rows, Mapping):
		# An invoice free row with no rule was not made by the engine (manual / another
		# app): the gift policy does not own it, so it is never removed here.
		snapshot.gifts = [gift for gift in snapshot.gifts if gift.rule_name]
	suppressed, dropped = _decide_gifts(snapshot, fnf_keys, rule_map or {})

	dropped_ids = {id(gift.ref) for gift in dropped}
	if isinstance(free_rows, Mapping):
		for gift in dropped:
			free_rows.pop(gift.ref, None)
		still_used = {gifts._rule_name_from_free_key(key) for key in free_rows}
	else:
		if isinstance(free_rows, list):
			free_rows[:] = [row for row in free_rows if id(row) not in dropped_ids]
		still_used = {gift.rule_name for gift in snapshot.gifts if id(gift.ref) not in dropped_ids}
	if suppressed and applied_rules is not None:
		applied_rules.difference_update(suppressed - still_used)
	return sorted(suppressed)


def fnf_row_keys(items) -> set:
	"""Row keys of paid lines holding F&F now (see :func:`normalize`)."""
	return {
		key
		for key, item in zip(row_key_list(items), items or [], strict=True)
		if not cint(item.get("is_free_item")) and is_fnf_stamped(item)
	}
