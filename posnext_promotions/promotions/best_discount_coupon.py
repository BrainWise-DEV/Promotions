# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Promo code (POS Coupon) vs Friends & Family / promotions — PN-128.

``validate_coupon`` (with cart ``items``) runs the coupon through
:func:`resolve_coupon_best_discount` so a coupon is only granted where it is
the single best discount:

* The coupon is always a line discount, before tax, for every customer (K1, D-R4-05):
  its share of each line is what :func:`coupon_engine.apply_coupon_to_items` allocates.
  Against the discount already on a line (promotion / manual) it is judged per line;
  an existing promotion keeps a tie (no churn).
* Against F&F, Friends and Family Settings **F&F and a Coupon on One Cart** decides
  (``ff_config.resolve().coupon_combination``):

  - "Better of the two for the whole cart" (default): F&F on the cart vs the coupon on
    the cart, F&F on a tie. The winner takes the whole cart; the two never mix.
  - "Better per line (unused coupon share is not used)": each line takes F&F or its
    coupon share, F&F on a tie; the share of a line F&F keeps is lost.
  - "Better per line, coupon fully used": as above, then a fixed or capped coupon is
    spread again over the lines it took, up to each line's room.

  A coupon's minimum purchase is judged on its eligible cart, before F&F splits it.

The F&F side is the saving the customer would really get, **after** the
PN-129 caps (per order / monthly), so a cap-reduced F&F can lose to a coupon.

Lines currently stamped F&F are offered to the coupon engine as undiscounted
(otherwise the coupon would never see them — F&F lines count as "already
discounted"), so a stronger coupon can replace F&F.

When the coupon wins nothing the response is ``valid: False`` with a clear
English/Arabic message (e.g. "Friends & Family discount (30%) is better than
coupon SAVE15 (15%) …") and ``reason: better_discount_exists``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import frappe
from frappe import _
from frappe.utils import cstr, flt

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_config, ff_constants, ff_money, ff_non_invoice
from posnext_promotions.promotions.friends_and_family_apply import (
	_is_fnf_stamped,
	_restore_replaced_discount,
	restore_context,
	compute_friends_and_family_discounts,
	enrich_items_with_catalog,
)
from posnext_promotions.promotions.friends_and_family_eligibility import get_customer_eligibility

REASON_BETTER_DISCOUNT_EXISTS = "better_discount_exists"


def _neutral_copy(item, ctx, coupon_code: str = ""):
	"""Line as the coupon engine should see it: an F&F stamp is not a blocker.

	For the coupon ``coupon_code`` is priced: a restored discount that is a coupon (F&F had
	replaced a coupon line) is no discount to stack on. The line goes back to list price,
	so ``coupon_code`` is priced on it once and never on top of itself or of another
	coupon (re-review M-A, G5 review m-4).
	"""
	line = frappe._dict(dict(item))
	if _restore_replaced_discount(line, ctx):
		if coupon_code and _holds_coupon(line):
			_plain_line(line)
		return line
	if _is_fnf_stamped(line):
		_plain_line(line)
	return line


def _holds_coupon(line) -> bool:
	"""The line's own discount is a POS Coupon's (any code)."""
	return bool(bd.line_coupon_code(line)) or cstr(line.get("discount_source")) == bd._COUPON_DISCOUNT_SOURCE


def _plain_line(line) -> None:
	"""``line`` at list price with no discount of its own."""
	line.discount_source = None
	line.discount_percentage = 0
	line.discount_amount = 0
	line.is_already_discounted = 0
	if line.get("posnext_coupon_code") or line.get("coupon_code"):
		line.posnext_coupon_code = None
		line.coupon_code = None
		line.pricing_rules = ""
	price_list_rate = bd.line_price_list_rate(line)
	if price_list_rate:
		line.rate = price_list_rate
		line.amount = price_list_rate * bd.line_qty(line)


def _capped_fnf_candidates(items, customer, posting_date, pos_profile=None) -> dict:
	"""F&F the customer would get per line *without* the coupon — caps (PN-129) included.

	The coupon then competes with that (possibly cap-reduced) F&F saving. A capped
	line is shown by its money value: its % is neither the program's nor meaningful.
	"""
	from posnext_promotions.api.apply_offers import _compat_doc
	from posnext_promotions.promotions import ff_compatibility

	ctx = restore_context(customer, posting_date, pos_profile)
	neutral = [_neutral_copy(i, ctx) for i in items]
	# Judged after the restore, as the cart and the invoice judge (spec §7, Rulings 121, 122).
	ff_compatibility.block_ineligible_lines(
		neutral, doc=_compat_doc({"pos_profile": pos_profile, "posting_date": posting_date}, customer)
	)
	plan = compute_friends_and_family_discounts(
		customer=customer,
		items=neutral,
		posting_date=posting_date,
		pos_profile=pos_profile,
	)
	candidates = {}
	for line in plan.get("items") or []:
		if not line.get("applies"):
			continue
		candidates[line.get("idx")] = bd.Candidate(
			source=bd.SOURCE_FRIENDS_AND_FAMILY,
			amount=flt(line.get("discount_amount")),
			percentage=None if line.get("capped") else (flt(line.get("discount_percentage"), 2) or None),
		)
	return candidates


def _existing_candidate(item, coupon_code: str) -> bd.Candidate | None:
	"""Non-F&F discount already on the line (a promotion or manual discount)."""
	source = bd.classify_line_source(item)
	if source in (bd.SOURCE_NONE, bd.SOURCE_FREE_ITEM, bd.SOURCE_FRIENDS_AND_FAMILY):
		return None
	if source == bd.SOURCE_COUPON and bd.line_coupon_code(item) == coupon_code:
		return None  # the same coupon re-validated — not a competitor
	amount = bd.line_discount_value(item)
	if amount <= 0:
		return None
	pct = flt(item.get("discount_percentage") or 0)
	return bd.Candidate(
		source=source,
		amount=amount,
		percentage=pct if pct > 0 else None,
		code=bd.line_coupon_code(item) or None if source == bd.SOURCE_COUPON else None,
	)


def _coupon_line_amount(update: dict, item) -> float:
	"""What the coupon itself saves on the line (a stacked line's discount also holds the promotion)."""
	if update.get("stacked"):
		return flt(update.get("coupon_only_discount") or 0)
	pct = flt(update.get("discount_percentage") or 0)
	if pct > 0:
		return bd.line_gross(item) * pct / 100.0
	return flt(update.get("discount_amount") or 0)


def _coupon_percentage(coupon) -> float | None:
	return flt(coupon.get("discount_percentage")) if cstr(coupon.get("discount_type")) == "Percentage" else None


def _tops_up_line_discounts(coupon) -> bool:
	"""A header "Better of the two" coupon, other than an uncapped %: a line keeps its own
	discount and the coupon only tops it up to the coupon's amount, so the two are never
	combined (Ruling 93)."""
	from posnext_promotions.api.coupon_engine import PROMOTION_CONFLICT_BETTER, get_promotion_conflict_mode

	uncapped_percentage = _coupon_percentage(coupon) is not None and not flt(coupon.get("max_amount"))
	return not uncapped_percentage and get_promotion_conflict_mode(coupon) == PROMOTION_CONFLICT_BETTER


def _coupon_amounts(coupon, rows, tax_rate: float) -> list[float]:
	"""Coupon money per row of a coupon carried as an invoice **header** discount.

	The cart never sends one (every coupon is a line discount, K1); an invoice queued
	offline by an older POS, or made on the desk, may. A "Grand Total" % coupon also
	saves the exclusive tax on it. A header "Better of the two" coupon gives each line
	only what it adds to the line's own discount: ``max(0, coupon amount on the line -
	the line's discount)``. The value rule for the unpaid header cap and the paid check.
	``max_amount`` caps the total after the tax, as :func:`apply_coupon_discount` does.
	"""
	pct = _coupon_percentage(coupon)
	scale = 1.0 + tax_rate if pct is not None else 1.0
	amounts = [_coupon_line_amount(update, item) * scale for update, item in rows]
	if _tops_up_line_discounts(coupon):
		amounts = [
			max(0.0, amount - flt(update.get("existing_discount")) * (1.0 + tax_rate))
			if not update.get("stacked")
			else amount
			for amount, (update, _item) in zip(amounts, rows, strict=True)
		]
	cap = flt(coupon.get("max_amount"))
	total = sum(amounts)
	if pct is not None and cap and total > cap:
		amounts = [amount * cap / total for amount in amounts]
	return amounts


def _with_tax(candidate: bd.Candidate | None, tax_rate: float) -> bd.Candidate | None:
	"""A line-discount candidate in grand-total money: it also saves its exclusive tax."""
	if candidate is None or not tax_rate:
		return candidate
	return replace(candidate, amount=candidate.amount * (1.0 + tax_rate))


def _line_updates(result: dict, items) -> list:
	"""``(line update, its cart line)`` pairs of an ``apply_coupon_to_items`` result."""
	return [
		(update, items[update.get("line_key")])
		for update in result.get("line_updates") or []
		if update.get("line_key") is not None and update.get("line_key") < len(items)
	]


def coupon_value_on(
	coupon, lines, tax_rate: float = 0.0, amount_basis: str = bd.AMOUNT_BASIS_LINE, pos_profile=None
) -> float:
	"""The coupon's money on ``lines`` as an invoice header (:func:`_coupon_amounts`).

	``amount_basis``: :data:`best_discount.AMOUNT_BASIS_UNIT` for saved invoice rows, whose
	own discount is then read from what they charge, never from ``discount_amount``.
	``pos_profile``: its POS Settings Max Discount limits an "Add on top" coupon, as on the cart.
	"""
	from posnext_promotions.api.coupon_engine import apply_coupon_to_items, max_discount_allowed

	lines = [frappe._dict(dict(line)) for line in lines or []]
	# A header holds the coupon's exact money, not whole cents a unit per line (D-R5-06).
	result = apply_coupon_to_items(
		coupon, lines, amount_basis, max_discount_allowed(pos_profile), whole_cents=False
	)
	if not result.get("valid"):
		return 0.0
	rate = tax_rate if cstr(coupon.get("apply_on")) == "Grand Total" else 0.0
	return sum(_coupon_amounts(coupon, _line_updates(result, lines), rate))


@dataclass
class CouponDecision:
	"""Coupon vs F&F (and the discounts already on the lines) for one cart.

	``validate_coupon`` and ``calculate_coupon_discount`` both read this, so they
	always agree on the winner and on the coupon's value.
	"""

	coupon_code: str
	result: dict  # apply_coupon_to_items on the lines without their F&F stamp
	items: list
	coupon_value: float = 0.0  # the coupon's own money on every line it can take (pre-tax)
	line_decisions: list = field(default_factory=list)
	order_decision: bd.Decision | None = None  # "whole cart": coupon vs F&F on the cart
	kept: list = field(default_factory=list)  # line updates the coupon keeps ("replaces" set)
	kept_total: float = 0.0  # the coupon's own money on the kept lines (pre-tax)
	fnf_lines: int = 0  # lines where the customer has F&F without the coupon
	replaces_fnf_on_cart: bool = False  # "whole cart" and the coupon won: no F&F on any line
	tax_rate: float = 0.0  # the exclusive tax every candidate's money includes (Ruling 56)
	coupon_own: dict = field(default_factory=dict)  # line_key -> the coupon's own money, pre-tax
	rejection: dict | None = None  # reason + messages when the coupon keeps nothing

	@property
	def coupon_wins(self) -> bool:
		return bool(self.kept)

	@property
	def summary(self) -> dict:
		return bd.summarize(self.line_decisions, self.order_decision)


def _rejection(decision: CouponDecision) -> dict:
	headline = decision.order_decision or next(
		(d for d in decision.line_decisions if d.winner_source != bd.SOURCE_COUPON), None
	)
	summary = decision.summary
	msgs = headline.messages() if headline else {"message": summary["message"], "en": "", "ar": ""}
	rival = headline.best_other if headline else None
	if (
		rival is not None
		and not headline.shown
		and rival.source == bd.SOURCE_COUPON
		and rival.percentage is None
	):
		# A fixed or capped coupon is named by its value, never by its share of one line,
		# and F&F by the money it gives on those lines, before tax (D-R5-07). The sentence
		# follows the two sums, not the first line the coupon lost (review M5).
		untaxed = 1.0 + decision.tax_rate
		taken = [d for d in decision.line_decisions if d.fnf_wins]
		fnf = bd.Candidate(
			source=bd.SOURCE_FRIENDS_AND_FAMILY, amount=sum(d.fnf.amount for d in taken) / untaxed
		)
		whole = replace(rival, amount=flt(decision.result.get("total_discount")))
		if bd.money(whole.amount) > bd.money(fnf.amount):
			# Other lines kept their own discount: set F&F against the coupon on its lines only.
			# The coupon's own money there: a stacked line's figure also holds its promotion (R1-M2).
			whole = replace(rival, amount=sum(flt(decision.coupon_own.get(d.idx)) for d in taken))
		outcome = (
			bd.OUTCOME_TIE_FNF if bd.money(fnf.amount) == bd.money(whole.amount) else bd.OUTCOME_FNF_BETTER
		)
		msgs = bd.build_cart_messages(outcome, fnf, whole)
	return {
		"reason": REASON_BETTER_DISCOUNT_EXISTS,
		"message": msgs["message"],
		"message_en": msgs["en"],
		"message_ar": msgs["ar"],
		"customer_message": msgs.get("customer"),
	}


@ff_money.in_cart_currency
def decide_coupon_vs_fnf(cart, coupon, customer=None, pos_profile=None) -> CouponDecision:
	"""The one coupon-vs-F&F resolver (PN-128).

	``cart``: ``items``, optional ``posting_date``, ``taxes`` (the cart's tax rows;
	without them the POS Profile's tax template decides which taxes are exclusive) and
	``doctype`` (on a Sales Order / Quotation F&F never applies, so it is no rival).
	The coupon is priced per line (``apply_coupon_to_items``) and set against F&F as
	the Settings' ``coupon_combination`` says (module docstring). With no F&F rival
	every mode is the same per-line rule.
	"""
	from posnext_promotions.api.coupon_engine import (
		apply_coupon_to_items,
		exclusive_tax_rate,
		max_discount_allowed,
	)

	cart = cart or {}
	pos_profile = pos_profile or cart.get("pos_profile")
	posting_date = cart.get("posting_date")
	code = cstr(coupon.get("coupon_code")).upper()
	items = enrich_items_with_catalog([frappe._dict(dict(i)) for i in cart.get("items") or []])

	ctx = restore_context(customer, posting_date, pos_profile)
	neutral = [_neutral_copy(i, ctx, code) for i in items]
	max_pct = max_discount_allowed(pos_profile)
	result = apply_coupon_to_items(coupon, neutral, max_discount_pct=max_pct)
	decision = CouponDecision(coupon_code=code, result=result, items=items)
	if not result.get("valid"):
		return decision

	program = None
	if ff_non_invoice.ff_applies_to(cart.get("doctype")):
		eligibility = get_customer_eligibility(customer, posting_date, pos_profile)
		program = eligibility.program if eligibility.eligible else None
	fnf_by_line = _capped_fnf_candidates(items, customer, posting_date, pos_profile) if program is not None else {}
	decision.fnf_lines = len(fnf_by_line)

	tax_rate = (
		exclusive_tax_rate(cart.get("taxes"), pos_profile) if cstr(coupon.get("apply_on")) == "Grand Total" else 0.0
	)
	decision.tax_rate = tax_rate
	if tax_rate:
		# Ruling 56: one unit for every candidate — what the customer saves including the
		# exclusive tax. A line discount also lowers the tax charged on that line, so the
		# coupon's line money is scaled the same way, whatever the coupon type.
		fnf_by_line = {idx: _with_tax(c, tax_rate) for idx, c in fnf_by_line.items()}
	# Named by its % only while the % is what it gives (a capped coupon by its money, M-1).
	pct = _coupon_percentage(coupon) if not flt(coupon.get("max_amount")) else None

	def rows_of(result) -> list:
		return [
			(
				update,
				item,
				bd.Candidate(
					source=bd.SOURCE_COUPON,
					amount=_coupon_line_amount(update, item) * (1.0 + tax_rate),
					percentage=pct,
					code=code,
				),
				fnf_by_line.get(update.get("line_key")),
				_with_tax(_existing_candidate(item, code), tax_rate),
			)
			for update, item in _line_updates(result, items)
		]

	def stacked(row) -> bool:
		"""An "Add on top" line: it keeps its own discount, so that discount is no rival."""
		return bool(row[0].get("stacked"))

	def rivals(row) -> list:
		"""What the coupon competes with on the line: F&F only on a stacked line (F&F would
		replace the promotion and the coupon both), else F&F and the line's own discount."""
		return [row[3]] if stacked(row) else [row[3], row[4]]

	def stacked_own(row) -> float:
		"""The discount a stacked line keeps under the coupon, as the engine priced it.

		From the engine's ``existing_discount`` (it ran on the neutral copy, so an F&F
		line shows the promotion F&F displaced), never from the cart line, whose own
		discount on an F&F-held line is F&F itself (review I-1).
		"""
		return flt(row[0].get("existing_discount")) * (1.0 + tax_rate) if stacked(row) else 0.0

	def line_scenario(row) -> bd.Candidate:
		"""The coupon as the line would get it: on a stacked line, the promotion plus the coupon."""
		if not stacked(row):
			return row[2]
		return replace(row[2], amount=row[2].amount + stacked_own(row))

	def replaced(row):
		best = bd.pick_best(rivals(row))
		return best.source if best else None

	def judge(rows) -> list:
		return [
			bd.decide([*rivals(row), line_scenario(row)], idx=row[0].get("line_key"), item_code=row[1].get("item_code"))
			for row in rows
		]

	def won(rows, lines) -> list:
		return [row for row, line in zip(rows, lines, strict=True) if line.winner_source == bd.SOURCE_COUPON]

	rows = rows_of(result)
	decision.coupon_value = sum(_coupon_line_amount(row[0], row[1]) for row in rows)
	lines = judge(rows)
	decision.coupon_own = {row[0].get("line_key"): _coupon_line_amount(row[0], row[1]) for row in rows}
	mode = ff_config.resolve().coupon_combination.value
	if fnf_by_line and mode == ff_constants.COUPON_WHOLE_CART:
		# The coupon's lines without F&F: where it beats the line's own discount.
		beats_own = [
			row for row in rows if bd.decide([*rivals(row)[1:], line_scenario(row)]).winner_source == bd.SOURCE_COUPON
		]
		kept_rows = _whole_cart(decision, beats_own, neutral, fnf_by_line, program, pct, tax_rate, line_scenario)
	else:
		# Per line. The minimum was met on the eligible cart before F&F split it (review I-1).
		kept_rows = won(rows, lines)
		if (
			mode == ff_constants.COUPON_PER_LINE_FULL
			and kept_rows
			and len(kept_rows) < len(rows)
			and _allocated(coupon)
		):
			# The lines F&F keeps hand their share to the lines the coupon takes, up to
			# each line's room (one pass: the shares only grow, so those lines still win).
			keys = {row[0].get("line_key") for row in kept_rows}
			subset = [line if k in keys else frappe._dict(line, is_free_item=1) for k, line in enumerate(neutral)]
			spread = apply_coupon_to_items(coupon, subset, max_discount_pct=max_pct, check_minimum=False)
			if spread.get("valid"):
				respread = rows_of(spread)
				decision.coupon_own.update(
					{row[0].get("line_key"): _coupon_line_amount(row[0], row[1]) for row in respread}
				)
				relines = judge(respread)
				by_key = {line.idx: line for line in relines}
				lines = [by_key.get(line.idx, line) for line in lines]
				kept_rows = won(respread, relines)
	decision.line_decisions = lines

	if not kept_rows:
		decision.rejection = _rejection(decision)
		return decision

	decision.kept = [{**row[0], "replaces": replaced(row)} for row in kept_rows]
	decision.kept_total = sum(_coupon_line_amount(row[0], row[1]) for row in kept_rows)
	return decision


def _allocated(coupon) -> bool:
	"""A coupon whose money is a cart total split over its lines (fixed amount or capped %)."""
	return _coupon_percentage(coupon) is None or bool(flt(coupon.get("max_amount")))


def _whole_cart(decision, coupon_rows, neutral, fnf_by_line, program, pct, tax_rate, line_scenario) -> list:
	""""Better of the two for the whole cart": F&F on the cart vs the coupon on the cart.

	F&F side: F&F on each line it takes, every other line keeps its own discount. Coupon
	side: the coupon on each of its lines where it beats the line's own discount, every
	other line keeps its own discount (and gets no F&F). One unit on both sides (the
	exclusive tax too); F&F wins a tie. The coupon that wins takes all those lines.
	"""
	code = decision.coupon_code
	own = [_with_tax(_existing_candidate(line, code), tax_rate) for line in neutral]

	def money(candidate) -> float:
		return candidate.amount if candidate else 0.0

	coupon_keys = {row[0].get("line_key") for row in coupon_rows}
	fnf_total = sum(money(fnf_by_line.get(k) or own[k]) for k in range(len(neutral)))
	coupon_total = sum(money(line_scenario(row)) for row in coupon_rows) + sum(
		money(own[k]) for k in range(len(neutral)) if k not in coupon_keys
	)
	capped = any(c.percentage is None for c in fnf_by_line.values())
	decision.order_decision = bd.decide(
		[
			bd.Candidate(
				source=bd.SOURCE_FRIENDS_AND_FAMILY,
				amount=fnf_total,
				percentage=None if capped else (flt(program.discount_percentage) or None),
			),
			bd.Candidate(source=bd.SOURCE_COUPON, amount=coupon_total, percentage=pct, code=code),
		],
		granularity=bd.GRANULARITY_ORDER,
	)
	# The note names each side by its own money before tax: F&F on the lines it takes, the
	# coupon on its lines (a stacked line's promotion is not the coupon's, D-R5-07).
	decision.order_decision.shown = (
		bd.Candidate(
			source=bd.SOURCE_FRIENDS_AND_FAMILY,
			amount=sum(c.amount for c in fnf_by_line.values() if c) / (1.0 + tax_rate),
		),
		bd.Candidate(
			source=bd.SOURCE_COUPON,
			amount=sum(_coupon_line_amount(row[0], row[1]) for row in coupon_rows),
			code=code,
		),
	)
	if decision.order_decision.winner_source != bd.SOURCE_COUPON:
		return []
	decision.replaces_fnf_on_cart = True
	return coupon_rows


@ff_money.in_cart_currency
def resolve_coupon_best_discount(
	coupon, items, customer=None, posting_date=None, pos_profile=None, taxes=None, doctype=None
) -> dict:
	"""``validate_coupon`` result: the coupon only where it is the best single discount."""
	decision = decide_coupon_vs_fnf(
		{"items": items, "posting_date": posting_date, "taxes": taxes, "doctype": doctype},
		coupon,
		customer,
		pos_profile,
	)
	result = dict(decision.result)
	result["coupon_value"] = flt(decision.coupon_value, 6)
	result["best_discount"] = decision.summary
	result["replaces_friends_and_family_on_cart"] = decision.replaces_fnf_on_cart
	if not result.get("valid"):
		result["best_discount"] = bd.summarize([])
		return result

	if decision.rejection:
		result.update(
			{
				"valid": False,
				"message_en": None,
				"message_ar": None,
				"customer_message": None,
				**decision.rejection,
				"line_updates": [],
				"total_discount": 0,
			}
		)
		return result

	summary = result["best_discount"]
	result.update(
		{
			"line_updates": decision.kept,
			"eligible_item_codes": [decision.items[u.get("line_key")].get("item_code") for u in decision.kept],
			"total_discount": flt(decision.kept_total, 6),
			"message": summary["message"] if summary.get("suppressed") else result.get("message"),
			"message_en": summary.get("message_en"),
			"message_ar": summary.get("message_ar"),
			"customer_message": summary.get("customer_message"),
		}
	)
	return result
