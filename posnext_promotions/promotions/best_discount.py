# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Best-discount engine (PN-128) — one discount per item, never stacked.

Business rule (approved; replaces the BRD's stacking-with-cap):

  * Candidates: Friends & Family, promo code (POS Coupon), automatic promotions
    (Pricing Rules / Promotional Schemes) and any existing item discount
    (manual cashier discount).
  * Only the **highest** applies. On a tie **Friends & Family wins**.
  * Nothing is ever added on top of the winner.

Granularity
-----------
Comparison is done in **money saved**, not in percentages, so a fixed-amount or
rate-based promotion competes fairly with a percentage.

* **Per line** — the default. F&F, automatic item promotions, percentage
  coupons (PN-121 line-level coupons) and manual discounts all live on the
  invoice line (``discount_percentage`` / ``discount_amount``), so each line
  keeps the single best of them (Jira: "per item, apply only the best single
  discount").
* **Whole order** — when the competitor is an *order-level* benefit that cannot
  be split per line without changing what it is:

  - an invoice header discount (``additional_discount_percentage`` /
    ``discount_amount``) from a transaction-scope promotion, an order-level
    coupon, or a manual invoice discount; and
  - fixed-amount coupons and percentage coupons with a ``max_amount`` cap
    (their value is a cart total, distributed across lines).

  Here the engine compares **total savings** of the two complete scenarios —
  "F&F on its covered lines, no order discount" vs "no F&F, order discount
  (plus the other line discounts F&F would have replaced)" — and keeps the
  larger one. Tie → F&F.

Free items / GWP
----------------
Gift and free-unit mechanics (``is_free_item`` rows, GWP and same-item free
units stamped on a paid line) are *product* discounts, not competing price
discounts. They are never compared and never removed by F&F: F&F skips lines
that already carry a free-unit discount, and gift rows stay in the cart.

This module is pure (no DB writes); callers own mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from frappe import _
from frappe.utils import cstr, flt

# ---- candidate sources ---------------------------------------------------

SOURCE_FRIENDS_AND_FAMILY = "friends_and_family"
SOURCE_PROMOTION = "promotion"
SOURCE_COUPON = "coupon"
SOURCE_MANUAL = "manual"
SOURCE_ORDER_PROMOTION = "order_promotion"
SOURCE_ORDER_COUPON = "order_coupon"
SOURCE_ORDER_MANUAL = "order_manual"
SOURCE_FREE_ITEM = "free_item"
SOURCE_NONE = "none"

LINE_SOURCES = (SOURCE_FRIENDS_AND_FAMILY, SOURCE_PROMOTION, SOURCE_COUPON, SOURCE_MANUAL)
ORDER_SOURCES = (SOURCE_ORDER_PROMOTION, SOURCE_ORDER_COUPON, SOURCE_ORDER_MANUAL)

# Tie-break order: lower index wins an exact tie. F&F always first (business
# rule). Between two non-F&F candidates the one already on the line is kept
# (promotion / manual before a newly entered coupon) to avoid churn.
_TIE_PRIORITY = {
	SOURCE_FRIENDS_AND_FAMILY: 0,
	SOURCE_PROMOTION: 1,
	SOURCE_MANUAL: 2,
	SOURCE_COUPON: 3,
	SOURCE_ORDER_PROMOTION: 4,
	SOURCE_ORDER_MANUAL: 5,
	SOURCE_ORDER_COUPON: 6,
}

# discount_source values (see api.promotion_exclusions) grouped by candidate.
_PROMOTION_DISCOUNT_SOURCES = frozenset(
	{"auto_discount", "item_level_promotion", "accumulative_promotion", "pricing_rule"}
)
_FREE_DISCOUNT_SOURCES = frozenset({"gwp", "free_item"})
_COUPON_DISCOUNT_SOURCE = "coupon"
_MANUAL_DISCOUNT_SOURCE = "manual_discount"
_FNF_DISCOUNT_SOURCE = "friends_and_family"

# Machine outcomes on a decision.
OUTCOME_FNF_ONLY = "fnf_only"
OUTCOME_FNF_BETTER = "fnf_better"
OUTCOME_TIE_FNF = "tie_fnf_wins"
OUTCOME_OTHER_BETTER = "other_better"
OUTCOME_NO_FNF = "no_fnf"
OUTCOME_NO_DISCOUNT = "no_discount"

GRANULARITY_LINE = "line"
GRANULARITY_ORDER = "order"

AMOUNT_BASIS_LINE = "line"  # POS cart payloads: discount_amount is the line total
AMOUNT_BASIS_UNIT = "unit"  # saved invoice rows: ERPNext discount_amount is per unit

MONEY_PRECISION = 2

RULE_PAYLOAD = {
	"rule": "best_only_no_stacking",
	"tie_breaker": SOURCE_FRIENDS_AND_FAMILY,
	"compare_by": "amount_saved",
}


def money(value) -> float:
	return flt(value, MONEY_PRECISION)


# ---- line helpers --------------------------------------------------------


def line_qty(item) -> float:
	return flt(item.get("qty") or item.get("quantity") or 0)


def line_price_list_rate(item) -> float:
	return flt(item.get("price_list_rate") or item.get("rate") or 0)


def line_gross(item) -> float:
	"""List-price value of the line (before any discount)."""
	return line_price_list_rate(item) * line_qty(item)


def line_discount_value(item, amount_basis: str = AMOUNT_BASIS_LINE) -> float:
	"""Money the line's current discount saves, whatever its source."""
	if item.get("is_free_item"):
		return 0.0
	qty = line_qty(item)
	gross = line_gross(item)
	pct = flt(item.get("discount_percentage") or 0)
	if pct > 0 and gross > 0:
		return min(gross, gross * pct / 100.0)
	amount = flt(item.get("discount_amount") or 0)
	if amount > 0:
		value = amount * qty if amount_basis == AMOUNT_BASIS_UNIT else amount
		return min(gross, value) if gross > 0 else value
	if amount_basis == AMOUNT_BASIS_UNIT:
		rate = flt(item.get("rate") or 0)
		pl = line_price_list_rate(item)
		if pl > 0 and 0 <= rate < pl and qty > 0:
			return (pl - rate) * qty
	return 0.0


def line_coupon_code(item) -> str:
	"""Coupon that discounted this line (cart ``coupon_code`` or saved ``posnext_coupon_code``)."""
	code = cstr(item.get("posnext_coupon_code"))
	if not code and cstr(item.get("discount_source")) == _COUPON_DISCOUNT_SOURCE:
		code = cstr(item.get("coupon_code"))
	return code.upper()


def has_pricing_rules(item) -> bool:
	value = item.get("pricing_rules")
	if not value:
		return False
	if isinstance(value, list | tuple | set):
		return any(cstr(v) for v in value)
	return bool(cstr(value).strip().strip("[]").strip())


def classify_line_source(item, amount_basis: str = AMOUNT_BASIS_LINE) -> str:
	"""Which candidate the line's *current* discount came from."""
	if item.get("is_free_item"):
		return SOURCE_FREE_ITEM
	source = cstr(item.get("discount_source"))
	if source == _FNF_DISCOUNT_SOURCE:
		return SOURCE_FRIENDS_AND_FAMILY
	if source in _FREE_DISCOUNT_SOURCES or flt(item.get("free_qty")) > 0 or flt(item.get("gwp_free_qty")) > 0:
		return SOURCE_FREE_ITEM
	if line_discount_value(item, amount_basis) <= 0:
		return SOURCE_NONE
	if line_coupon_code(item) or source == _COUPON_DISCOUNT_SOURCE:
		return SOURCE_COUPON
	if source in _PROMOTION_DISCOUNT_SOURCES or has_pricing_rules(item):
		return SOURCE_PROMOTION
	return SOURCE_MANUAL


# ---- candidates & decisions ----------------------------------------------


@dataclass
class Candidate:
	source: str
	amount: float
	percentage: float | None = None
	code: str | None = None  # coupon code / rule name, for labels

	def to_payload(self) -> dict:
		return {
			"source": self.source,
			"amount": money(self.amount),
			"percentage": flt(self.percentage) if self.percentage is not None else None,
			"code": self.code,
			"label": source_label(self.source, self.code)["en"],
			"label_ar": source_label(self.source, self.code)["ar"],
		}


def pick_best(candidates) -> Candidate | None:
	"""Largest saving wins; exact tie (to the cent) → lowest tie priority (F&F first)."""
	live = [c for c in candidates or [] if c and money(c.amount) > 0]
	if not live:
		return None
	return min(live, key=lambda c: (-money(c.amount), _TIE_PRIORITY.get(c.source, 99)))


@dataclass
class Decision:
	"""Outcome of one comparison (a line, or the whole order)."""

	granularity: str
	winner: Candidate | None
	candidates: list[Candidate] = field(default_factory=list)
	idx: int | None = None
	item_code: str | None = None

	@property
	def fnf(self) -> Candidate | None:
		return next((c for c in self.candidates if c.source == SOURCE_FRIENDS_AND_FAMILY), None)

	@property
	def best_other(self) -> Candidate | None:
		return pick_best([c for c in self.candidates if c.source != SOURCE_FRIENDS_AND_FAMILY])

	@property
	def winner_source(self) -> str:
		return self.winner.source if self.winner else SOURCE_NONE

	@property
	def fnf_wins(self) -> bool:
		return self.winner_source == SOURCE_FRIENDS_AND_FAMILY

	@property
	def suppressed(self) -> list[str]:
		return [
			c.source
			for c in self.candidates
			if money(c.amount) > 0 and (self.winner is None or c is not self.winner)
		]

	@property
	def outcome(self) -> str:
		fnf, other = self.fnf, self.best_other
		fnf_amt = money(fnf.amount) if fnf else 0
		other_amt = money(other.amount) if other else 0
		if not fnf_amt and not other_amt:
			return OUTCOME_NO_DISCOUNT
		if not fnf_amt:
			return OUTCOME_NO_FNF
		if not other_amt:
			return OUTCOME_FNF_ONLY
		if fnf_amt > other_amt:
			return OUTCOME_FNF_BETTER
		if fnf_amt == other_amt:
			return OUTCOME_TIE_FNF
		return OUTCOME_OTHER_BETTER

	def messages(self) -> dict:
		return build_messages(self.outcome, self.fnf, self.best_other)

	def to_payload(self) -> dict:
		msgs = self.messages()
		payload = {
			"granularity": self.granularity,
			"winner": self.winner_source,
			"winner_amount": money(self.winner.amount) if self.winner else 0.0,
			"outcome": self.outcome,
			"candidates": [c.to_payload() for c in self.candidates if money(c.amount) > 0],
			"suppressed": self.suppressed,
			"message": msgs["message"],
			"message_en": msgs["en"],
			"message_ar": msgs["ar"],
			"customer_message": msgs.get("customer"),
		}
		if self.granularity == GRANULARITY_LINE:
			payload["idx"] = self.idx
			payload["item_code"] = self.item_code
		return payload


def decide(candidates, granularity=GRANULARITY_LINE, idx=None, item_code=None) -> Decision:
	candidates = [c for c in candidates or [] if c is not None]
	return Decision(
		granularity=granularity,
		winner=pick_best(candidates),
		candidates=candidates,
		idx=idx,
		item_code=item_code,
	)


# ---- bilingual messages ----------------------------------------------------

_LABELS = {
	SOURCE_FRIENDS_AND_FAMILY: ("Friends & Family discount", "خصم الأصدقاء والعائلة"),
	SOURCE_PROMOTION: ("the promotion", "العرض الترويجي"),
	SOURCE_COUPON: ("coupon {code}", "الكوبون {code}"),
	SOURCE_MANUAL: ("the existing item discount", "خصم الصنف الحالي"),
	SOURCE_ORDER_PROMOTION: ("the invoice promotion", "عرض الفاتورة"),
	SOURCE_ORDER_COUPON: ("coupon {code}", "الكوبون {code}"),
	SOURCE_ORDER_MANUAL: ("the invoice discount", "خصم الفاتورة"),
	SOURCE_FREE_ITEM: ("the free item offer", "عرض الصنف المجاني"),
	SOURCE_NONE: ("no discount", "بدون خصم"),
}


def source_label(source: str, code: str | None = None) -> dict:
	en, ar = _LABELS.get(source, (source, source))
	code = cstr(code) or ""
	if "{code}" in en:
		if code:
			en, ar = en.format(code=code), ar.format(code=code)
		else:
			en, ar = ("the coupon", "الكوبون")
	return {"en": en, "ar": ar}


def _value_text(candidate: Candidate | None) -> str:
	if candidate is None:
		return "0"
	if candidate.percentage:
		return f"{flt(candidate.percentage):g}%"
	return f"{money(candidate.amount):.2f}"


def build_messages(outcome: str, fnf: Candidate | None, other: Candidate | None) -> dict:
	"""Cashier-facing explanation in English (+ translated) and Arabic."""
	o_label = source_label(other.source, other.code) if other else source_label(SOURCE_NONE)
	fnf_v, other_v = _value_text(fnf), _value_text(other)

	if outcome == OUTCOME_FNF_BETTER:
		en = "Friends & Family discount ({0}) is better than {1} ({2}) — Friends & Family applied, {1} not applied."
		ar = "خصم الأصدقاء والعائلة ({0}) أفضل من {1} ({2}) — تم تطبيق خصم الأصدقاء والعائلة ولم يتم تطبيق {1}."
	elif outcome == OUTCOME_TIE_FNF:
		en = "Friends & Family discount ({0}) equals {1} ({2}) — Friends & Family applied on a tie."
		ar = "خصم الأصدقاء والعائلة ({0}) يساوي {1} ({2}) — عند التساوي يتم تطبيق خصم الأصدقاء والعائلة."
	elif outcome == OUTCOME_OTHER_BETTER:
		en = "{1} ({2}) is better than the Friends & Family discount ({0}) — {1} applied instead of Friends & Family."
		ar = "{1} ({2}) أفضل من خصم الأصدقاء والعائلة ({0}) — تم تطبيق {1} بدلاً من خصم الأصدقاء والعائلة."
	elif outcome == OUTCOME_FNF_ONLY:
		en = "Friends & Family discount ({0}) applied."
		ar = "تم تطبيق خصم الأصدقاء والعائلة ({0})."
	elif outcome == OUTCOME_NO_FNF:
		en = "{1} ({2}) applied."
		ar = "تم تطبيق {1} ({2})."
	else:
		en = "No discount applies."
		ar = "لا يوجد خصم."

	def _cap(text: str) -> str:
		return text[:1].upper() + text[1:]

	return {
		"en": _cap(en.format(fnf_v, o_label["en"], other_v)),
		"ar": ar.format(fnf_v, o_label["ar"], other_v),
		"message": _cap(_(en).format(fnf_v, _(o_label["en"]), other_v)),
		"customer": customer_message_for(outcome, other),
	}


_COUPON_SOURCES = frozenset({SOURCE_COUPON, SOURCE_ORDER_COUPON})


def customer_message_for(outcome: str, other: Candidate | None) -> dict | None:
	"""Friends and Family Message for a coupon vs F&F decision (None when no coupon involved)."""
	if other is None or other.source not in _COUPON_SOURCES:
		return None
	from posnext_promotions.promotions import friends_and_family_messages as ffm

	event = {
		OUTCOME_FNF_BETTER: ffm.EVENT_COUPON_LOWER_FF_APPLIED,
		OUTCOME_TIE_FNF: ffm.EVENT_TIE,
		OUTCOME_OTHER_BETTER: ffm.EVENT_COUPON_HIGHER_APPLIED,
	}.get(outcome)
	try:
		return ffm.get_customer_message(event) if event else None
	except Exception:
		return None


# ---- summary ---------------------------------------------------------------


def summarize(line_decisions, order_decision: Decision | None = None) -> dict:
	"""``best_discount`` block for API payloads (PN-130 UI reads this)."""
	line_decisions = [d for d in line_decisions or [] if d is not None]
	compared = [d for d in line_decisions if d.outcome != OUTCOME_NO_DISCOUNT]
	payload = {
		**RULE_PAYLOAD,
		"granularity": GRANULARITY_ORDER if order_decision else GRANULARITY_LINE,
		"lines": [d.to_payload() for d in compared],
		"order": order_decision.to_payload() if order_decision else None,
		"fnf_total": money(sum(d.fnf.amount for d in compared if d.fnf and d.fnf_wins)),
		"other_total": money(sum(d.winner.amount for d in compared if d.winner and not d.fnf_wins)),
		"suppressed": sorted({s for d in compared for s in d.suppressed}),
		"stacking": False,
	}
	if order_decision:
		payload["winner"] = order_decision.winner_source
		payload["message"] = order_decision.messages()["message"]
		payload["message_en"] = order_decision.messages()["en"]
		payload["message_ar"] = order_decision.messages()["ar"]
		payload["customer_message"] = order_decision.messages().get("customer")
	else:
		winners = {d.winner_source for d in compared if d.winner}
		payload["winner"] = (
			SOURCE_FRIENDS_AND_FAMILY
			if winners == {SOURCE_FRIENDS_AND_FAMILY}
			else (winners.pop() if len(winners) == 1 else ("mixed" if winners else SOURCE_NONE))
		)
		# Headline: the first line where something was actually suppressed,
		# else the first compared line.
		headline = next((d for d in compared if d.suppressed), compared[0] if compared else None)
		msgs = headline.messages() if headline else build_messages(OUTCOME_NO_DISCOUNT, None, None)
		payload["message"] = msgs["message"]
		payload["message_en"] = msgs["en"]
		payload["message_ar"] = msgs["ar"]
		payload["customer_message"] = msgs.get("customer")
	return payload
