# Copyright (c) 2026, BrainWise and contributors
"""Coupon eligibility/discount helpers. Independent of the POS Coupon DocType class."""

import math

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, today

# What a coupon does on a line that already carries a discount (promotion or manual)
PROMOTION_CONFLICT_SKIP = "Skip the line"
PROMOTION_CONFLICT_ADD = "Add on top"
PROMOTION_CONFLICT_BETTER = "Better of the two"
PROMOTION_CONFLICT_OPTIONS = (PROMOTION_CONFLICT_SKIP, PROMOTION_CONFLICT_ADD, PROMOTION_CONFLICT_BETTER)


def get_promotion_conflict_mode(coupon) -> str:
	"""The coupon's own choice for already-discounted lines; blank means skip them."""
	mode = (getattr(coupon, "promotion_conflict", None) or "").strip()
	return mode if mode in PROMOTION_CONFLICT_OPTIONS else PROMOTION_CONFLICT_SKIP


def max_discount_allowed(pos_profile) -> float:
	"""POS Settings **Max Discount Percentage Allowed** of ``pos_profile`` (0 = no limit).

	An "Add on top" coupon never takes a line past it: the promotion plus the coupon
	stay within this share of the line's list price.
	"""
	if not pos_profile or not frappe.db.table_exists("POS Settings"):
		return 0.0
	if not frappe.get_meta("POS Settings").has_field("max_discount_allowed"):
		return 0.0
	return flt(frappe.db.get_value("POS Settings", {"pos_profile": pos_profile}, "max_discount_allowed"))


def with_item_master_scope(items) -> list:
	"""Copies of ``items`` whose ``brand`` / ``item_group`` come from the Item master.

	Coupon scope and brand exclusions never trust the client line. These are the
	Item's own values with no template fallback (that fallback is F&F scope only);
	an unknown item code gets none.
	"""
	from posnext_promotions.promotions import ff_scope

	rows = [frappe._dict(row) for row in items or []]
	catalog = ff_scope.item_scope_fields(row.get("item_code") for row in rows)
	for row in rows:
		master = catalog.get(cstr(row.get("item_code"))) or {}
		row.brand = master.get("brand")
		row.item_group = master.get("item_group")
	return rows


def _line_existing_discount(item, amount_basis: str = "line") -> float:
	"""Discount a line already carries before the coupon (the payload sends pre-coupon values).

	``amount_basis="unit"`` is a saved invoice row (Ruling 96): its ``discount_amount`` is
	per unit and the client wrote it, so the line is valued from what it charges against
	the server-set ``price_list_rate`` (:func:`best_discount.line_discount_value`), never
	from that field. Cart payloads (``"line"``) keep their line totals.
	"""
	if cint(item.get("is_free_item") or 0):
		return 0.0
	base = _item_base_amount(item)
	if amount_basis == "unit":
		from posnext_promotions.promotions import best_discount as bd

		return min(max(bd.line_discount_value(item, bd.AMOUNT_BASIS_UNIT), 0.0), base)
	amount = flt(item.get("discount_amount") or 0)
	if amount <= 0 and flt(item.get("discount_percentage") or 0) > 0:
		amount = base * flt(item.get("discount_percentage")) / 100.0
	if amount <= 0 and flt(item.get("price_list_rate") or 0) > flt(item.get("rate") or 0) > 0:
		amount = (flt(item.get("price_list_rate")) - flt(item.get("rate"))) * flt(item.get("qty") or 0)
	return min(max(amount, 0.0), base)

ONE_USE_COUPON_DOCTYPES = ("Sales Invoice", "POS Invoice")


def check_coupon_code(coupon_code, customer=None, company=None):
	"""Validate and return coupon details"""
	res = {"coupon": None}

	if not frappe.db.exists("POS Coupon", {"coupon_code": coupon_code.upper()}):
		res["msg"] = _("Sorry, this coupon code does not exist")
		return res

	coupon = frappe.get_doc("POS Coupon", {"coupon_code": coupon_code.upper()})

	# Check if coupon is disabled
	if coupon.disabled:
		res["msg"] = _("Sorry, this coupon has been disabled")
		return res

	# Check validity dates
	if coupon.valid_from:
		if coupon.valid_from > getdate(today()):
			res["msg"] = _("Sorry, this coupon code's validity has not started")
			return res

	if coupon.valid_upto:
		if coupon.valid_upto < getdate(today()):
			res["msg"] = _("Sorry, this coupon code has expired")
			return res

	# Check usage limits
	if coupon.used and coupon.maximum_use and coupon.used >= coupon.maximum_use:
		res["msg"] = _("Sorry, this coupon code has been fully redeemed")
		return res

	# Check company
	if company and coupon.company != company:
		res["msg"] = _("Sorry, this coupon is not valid for this company")
		return res

	# Check customer (for Gift Cards)
	if coupon.coupon_type == "Gift Card" and coupon.customer:
		if not customer or coupon.customer != customer:
			res["msg"] = _("Sorry, this gift card is assigned to a specific customer")
			return res

	# Per-customer usage limit
	per_customer_limit = _get_per_customer_use_limit(coupon)
	if per_customer_limit and customer:
		used_count = _get_customer_coupon_usage_count(customer, coupon.coupon_code)
		if used_count >= per_customer_limit:
			res["msg"] = _("Sorry, you have already used this coupon code the maximum number of times")
			return res

	# All validations passed
	res["coupon"] = coupon
	res["valid"] = True

	return res


def _get_per_customer_use_limit(coupon):
	"""Resolve max uses per customer. one_use implies 1 when maximum_use_per_customer is unset."""
	limit = cint(getattr(coupon, "maximum_use_per_customer", 0) or 0)
	if limit > 0:
		return limit
	if cint(getattr(coupon, "one_use", 0) or 0):
		return 1
	return 0


def _get_customer_coupon_usage_count(customer, coupon_code):
	"""Count submitted coupon usage across POSNext's actual sales doctypes."""
	used_count = 0

	for doctype in ONE_USE_COUPON_DOCTYPES:
		if not frappe.db.table_exists(doctype):
			continue

		meta = frappe.get_meta(doctype)
		if not meta.has_field("coupon_code"):
			continue

		used_count += frappe.db.count(
			doctype,
			filters={
				"customer": customer,
				"coupon_code": coupon_code,
				"docstatus": 1,
			},
		)

	return used_count


def get_coupon_eligible_items(coupon, items, amount_basis: str = "line"):
	"""
	Return cart items that pass the Promotion Interaction Matrix for coupons.

	Coupon is blocked only when the line has:
	1. Auto Discount
	2. Item Level Discount (including Accumulative)
	3. Manual cashier discount
	4. Excluded brands from coupon config

	Other promotional types (GWP, Gift Pool, etc.) remain coupon-eligible.
	``exclude_already_discounted_items`` on the coupon is ignored — matrix rules
	are mandatory for coupons.
	"""
	if not items:
		return []

	from posnext_promotions.api.promotion_exclusions import (
		PROMOTION_TARGET_COUPON,
		get_rule_promotion_types,
		is_eligible_for_promotion,
		mark_item_discount_flags,
		_parse_pricing_rules,
	)

	from posnext_promotions.api.promotion_exclusions import (
		ITEM_STATE_EXCLUDED_BRAND,
		classify_item_state,
	)

	mode = get_promotion_conflict_mode(coupon)
	prepared_items = with_item_master_scope(items)
	all_rule_names: list[str] = []
	for item in prepared_items:
		all_rule_names.extend(_parse_pricing_rules(item.get("pricing_rules")))
	type_map = get_rule_promotion_types(list(set(all_rule_names)))

	mark_item_discount_flags(prepared_items, type_map)

	excluded_brands = _get_excluded_brands(coupon)
	eligible = []

	for index, raw in enumerate(prepared_items):
		item = raw if isinstance(raw, dict) else dict(raw)
		if cint(item.get("is_free_item") or 0):
			continue
		existing_discount = _line_existing_discount(item, amount_basis)
		if mode != PROMOTION_CONFLICT_SKIP:
			# The coupon's setting, not the matrix, decides lines that already carry a discount
			allowed = (
				classify_item_state(
					item,
					rule_type_map=type_map,
					excluded_brands=excluded_brands,
					target=PROMOTION_TARGET_COUPON,
					exclude_discounted=False,
				)
				!= ITEM_STATE_EXCLUDED_BRAND
			)
		else:
			allowed = existing_discount <= 0 and is_eligible_for_promotion(
				item,
				PROMOTION_TARGET_COUPON,
				rule_type_map=type_map,
				excluded_brands=excluded_brands,
				exclude_discounted=True,
			)
		if not allowed:
			# Allow re-evaluating lines already tagged with this same coupon
			existing_coupon = (item.get("coupon_code") or "").upper()
			this_code = (coupon.coupon_code or "").upper()
			if not (existing_coupon and this_code and existing_coupon == this_code):
				continue
		if not _item_matches_scope(coupon, item):
			continue

		item = dict(item)
		item["_line_key"] = _item_line_key(item, index)
		item["_base_amount"] = _item_base_amount(item)
		item["_existing_discount"] = existing_discount if mode != PROMOTION_CONFLICT_SKIP else 0.0
		if flt(item["_base_amount"]) <= 0:
			continue
		eligible.append(item)

	return eligible


def _get_excluded_brands(coupon):
	excluded = set()
	rows = getattr(coupon, "excluded_brands", None)
	if rows is None and hasattr(coupon, "get"):
		rows = coupon.get("excluded_brands")
	for row in rows or []:
		brand = row.get("brand") if isinstance(row, dict) else getattr(row, "brand", None)
		if brand:
			excluded.add(brand)
	return excluded


def _get_scope_values(coupon, table_field, column):
	rows = getattr(coupon, table_field, None)
	if rows is None and hasattr(coupon, "get"):
		rows = coupon.get(table_field)
	values = set()
	for row in rows or []:
		value = row.get(column) if isinstance(row, dict) else getattr(row, column, None)
		if value:
			values.add(value)
	return values


def _item_matches_scope(coupon, item):
	apply_scope = getattr(coupon, "apply_scope", None) or "All Eligible Items"
	if apply_scope == "Item Code":
		return bool(item.get("item_code")) and item.get("item_code") in _get_scope_values(
			coupon, "applicable_items", "item_code"
		)
	if apply_scope == "Brand":
		brands = _get_scope_values(coupon, "applicable_brands", "brand")
		if getattr(coupon, "applicable_brand", None):
			brands.add(coupon.applicable_brand)
		return bool(item.get("brand")) and item.get("brand") in brands
	if apply_scope == "Item Group":
		groups = _get_scope_values(coupon, "applicable_item_groups", "item_group")
		if getattr(coupon, "applicable_item_group", None):
			groups.add(coupon.applicable_item_group)
		return _item_group_in(item.get("item_group"), groups)
	return True


def _item_group_in(item_group, groups):
	"""True when ``item_group`` is one of ``groups`` or sits below one in the Item Group tree.

	Item Group is a nested-set tree, so a coupon on "All Item Groups" (the root) or on a
	parent group covers every item filed under its child groups.
	"""
	if not item_group or not groups:
		return False
	if item_group in groups:
		return True
	try:
		item_lft = frappe.db.get_value("Item Group", item_group, "lft")
		if item_lft is None:
			return False
		for group in groups:
			bounds = frappe.db.get_value("Item Group", group, ["lft", "rgt"])
			if bounds and bounds[0] <= item_lft < bounds[1]:
				return True
	except Exception:
		return False
	return False


def _item_line_key(item, index):
	"""Stable 0-based cart position for matching line_updates on the client."""
	return index


def _item_base_amount(item):
	"""Undiscounted line amount used for coupon eligible subtotal."""
	qty = flt(item.get("qty") or item.get("quantity") or 0)
	price_list_rate = flt(item.get("price_list_rate") or 0)
	if price_list_rate > 0 and qty > 0:
		return price_list_rate * qty
	amount = flt(item.get("amount") or 0)
	if amount > 0:
		return amount
	rate = flt(item.get("rate") or 0)
	return rate * qty


def _coupon_rejection_message(coupon, items) -> str:
	"""Explain why no cart lines qualify for this coupon."""
	from posnext_promotions.api.promotion_exclusions import (
		PROMOTION_TARGET_COUPON,
		classify_item_state,
		is_coupon_broad_discounted,
	)

	excluded_brands = _get_excluded_brands(coupon)
	reasons: list[str] = []

	for item in with_item_master_scope(items):
		if cint(item.get("is_free_item") or 0):
			continue
		code = item.get("item_code") or _("Item")
		brand = item.get("brand")
		if brand and brand in excluded_brands:
			reasons.append(_("Item {0} is excluded (brand {1})").format(code, brand))
			continue
		if is_coupon_broad_discounted(item):
			reasons.append(
				_("Item {0} already has Auto Discount or Item Level Discount").format(code)
			)
			continue
		state = classify_item_state(
			item,
			excluded_brands=excluded_brands,
			target=PROMOTION_TARGET_COUPON,
			exclude_discounted=True,
		)
		if state == "excluded_brand":
			reasons.append(_("Item {0} is excluded (brand {1})").format(code, brand or ""))

	if reasons:
		return "; ".join(reasons[:3])
	scope = getattr(coupon, "apply_scope", None) or "All Eligible Items"
	if scope in ("Item Code", "Item Group", "Brand"):
		return _("No items in the cart match this coupon's {0} scope").format(_(scope))
	return _("No eligible items for this coupon")


def _whole_cents_per_unit(lines, parts, limits) -> tuple[list[float], float]:
	"""Coupon shares a saved row can hold as they are; and the most the cut can lose.

	An invoice row keeps its discount per unit at the currency's precision, and the POS
	cuts each fixed coupon line down to that (``couponLines.fixedCouponLineDiscount``), so
	40 split 22.2222 + 17.7778 was sold as 22.22 + 17.77 (D-R5-06). Each share is cut
	the same way here, and what the cut lost goes, whole, to one line: the one that lost
	the most (to a hundredth of a cent; the lowest line on a tie) among those whose units
	split it evenly and that have room for it (``limits``). When no line can take all of
	it, it goes in whole units a unit, in the same order, as far as it goes.

	The shares never add up to more than ``parts``. They add up to all of it when some
	line's quantity divides what the cut lost; otherwise up to that remainder is not
	given (3 x 50 cannot hold 40: 13.33 x 3 = 39.99), as the POS sold it before.

	The second value is the most the cut can lose over these lines, a smallest unit per
	unit sold. Which line gets the lost money, and how much is lost, depend on sub-cent
	figures the cart and the invoice see differently (the cart's promotion is to the
	cent, the server's exact), so the invoice check allows any one line that much more
	and holds the lines together to the split (review I1).
	"""
	from posnext_promotions.promotions import ff_money

	scale = 10 ** ff_money.precision_for(ff_money.current_currency() or ff_money.default_currency())
	qtys = [flt(item.get("qty") or item.get("quantity") or 0) or 1 for item in lines]
	units = [math.floor(max(part, 0) / qty * scale + 1e-6) for part, qty in zip(parts, qtys, strict=True)]
	left = math.floor(sum(parts) * scale - sum(u * q for u, q in zip(units, qtys, strict=True)) + 1e-6)
	order = sorted(range(len(parts)), key=lambda k: (-round(parts[k] * scale - units[k] * qtys[k], 2), k))

	def room(k):
		"""Whole units a unit line ``k`` may still take."""
		return math.floor((flt(limits[k]) * scale - units[k] * qtys[k]) / qtys[k] + 1e-6)

	def fits(k, amount):
		per_unit = amount / qtys[k]
		return abs(per_unit - round(per_unit)) < 1e-6 and round(per_unit) <= room(k)

	whole = next((k for k in order if left > 0 and fits(k, left)), None)
	if whole is not None:
		units[whole] += round(left / qtys[whole])
	else:
		for k in order:
			step = min(math.floor(left / qtys[k] + 1e-6), room(k))
			if step > 0:
				units[k] += step
				left -= step * qtys[k]
	return [flt(u * q / scale, 6) for u, q in zip(units, qtys, strict=True)], flt(sum(qtys) / scale, 6)


def apply_coupon_to_items(
	coupon,
	items,
	amount_basis: str = "line",
	max_discount_pct: float = 0,
	check_minimum: bool = True,
	whole_cents: bool = True,
):
	"""
	Calculate per-line coupon discounts for matrix-eligible items only.

	Lines with Auto Discount, Item Level Discount, or manual discount never
	reach this path (see get_coupon_eligible_items). Other promotional types
	remain eligible.

	``amount_basis``: ``"line"`` for cart payloads, ``"unit"`` for saved invoice rows
	(see :func:`_line_existing_discount`).

	``max_discount_pct`` (POS Settings Max Discount, :func:`max_discount_allowed`; 0 = no
	limit): an "Add on top" coupon tops a discounted line up to that share of its list
	price at most, and leaves a line already there alone. A fixed or capped coupon is
	then spread by what each line may still take (its headroom under the limit, not its
	room after the promotion), so a promoted line near the limit takes a small share and
	the other lines take the rest.

	``check_minimum``: False re-spreads a coupon whose minimum was already met on the
	whole eligible cart over some of its lines (F&F "coupon fully used", K1).

	``whole_cents``: a fixed or capped coupon's line shares are whole cents a unit, as a
	row holds them (:func:`_whole_cents_per_unit`). False keeps the exact split, for a
	coupon valued as an invoice header (``best_discount_coupon.coupon_value_on``).

	Returns dict with valid, message, eligible_item_codes, line_updates, total_discount.
	"""
	eligible = get_coupon_eligible_items(coupon, items, amount_basis)
	if not eligible:
		return {
			"valid": False,
			"message": _coupon_rejection_message(coupon, items),
			"eligible_item_codes": [],
			"line_updates": [],
			"total_discount": 0,
		}

	eligible_subtotal = sum(flt(i["_base_amount"]) for i in eligible)

	if check_minimum and coupon.min_amount and flt(eligible_subtotal) < flt(coupon.min_amount):
		return {
			"valid": False,
			"message": _("Minimum eligible amount of {0} is required").format(
				frappe.format_value(coupon.min_amount, {"fieldtype": "Currency"})
			),
			"eligible_item_codes": [i.get("item_code") for i in eligible],
			"line_updates": [],
			"total_discount": 0,
		}

	mode = get_promotion_conflict_mode(coupon)
	is_percentage = coupon.discount_type == "Percentage"
	pct = flt(coupon.discount_percentage) if is_percentage else 0.0

	def room(item):
		"""What the coupon may discount on the line: what is left after the promotion when stacking."""
		existing = flt(item["_existing_discount"]) if mode == PROMOTION_CONFLICT_ADD else 0.0
		return max(flt(item["_base_amount"]) - existing, 0.0)

	def most(item):
		"""Most the coupon may add to the line: up to the POS max discount when stacking."""
		existing = flt(item["_existing_discount"]) if mode == PROMOTION_CONFLICT_ADD else 0.0
		if existing <= 0 or flt(max_discount_pct) <= 0:
			return room(item)
		return max(min(room(item), flt(item["_base_amount"]) * flt(max_discount_pct) / 100.0 - existing), 0.0)

	def coupon_parts(lines):
		"""Coupon share per line before the cap: % of its room, or a fixed amount split by room."""
		if is_percentage:
			return [min(room(item) * pct / 100.0, most(item)) for item in lines]
		rooms = [most(item) for item in lines]
		total = flt(coupon.discount_amount)
		if coupon.max_amount and total > flt(coupon.max_amount):
			total = flt(coupon.max_amount)
		total = min(total, sum(rooms))
		parts, allocated = [], 0.0
		for index, value in enumerate(rooms):
			share = total - allocated if index == len(rooms) - 1 else flt(total * value / sum(rooms), 6)
			allocated += share
			parts.append(share)
		return parts

	def better_than_promotion(item, part):
		return flt(item["_existing_discount"]) <= 0 or part > flt(item["_existing_discount"])

	# A stacked line already at the POS max discount takes nothing more.
	chosen = [item for item in eligible if flt(most(item), 6) > 0]
	kept = [item for item in eligible if flt(most(item), 6) <= 0]
	parts = coupon_parts(chosen)
	if mode == PROMOTION_CONFLICT_BETTER:
		kept = [item for item, part in zip(chosen, parts) if not better_than_promotion(item, part)]
		chosen = [item for item in chosen if item not in kept]
		parts = coupon_parts(chosen)

	if is_percentage and coupon.max_amount and sum(parts) > flt(coupon.max_amount):
		scale = flt(coupon.max_amount) / sum(parts)
		parts = [part * scale for part in parts]
		if mode == PROMOTION_CONFLICT_BETTER:
			# ponytail: one re-check after the cap; a line dropped here does not re-spread its share
			lost = [item for item, part in zip(chosen, parts) if not better_than_promotion(item, part)]
			kept += lost
			parts = [part for item, part in zip(chosen, parts) if item not in lost]
			chosen = [item for item in chosen if item not in lost]

	slack = 0.0
	exact = list(parts)  # before the cut: what the invoice holds the coupon's rows to (R1-M1)
	if whole_cents and chosen and not (is_percentage and not coupon.max_amount):
		# A fixed or capped coupon: whole cents a unit, up to the coupon (D-R5-06). A
		# capped-% line takes at most the coupon's % of it: the POS clamps it there too.
		limits = [
			min(most(item), room(item) * pct / 100.0) if is_percentage else most(item) for item in chosen
		]
		parts, slack = _whole_cents_per_unit(chosen, parts, limits)
	elif whole_cents and chosen and mode == PROMOTION_CONFLICT_BETTER:
		# An uncapped % stays a % on a plain line; on a line it takes from a promotion ("Better
		# of the two") it becomes a fixed amount, cut to whole cents a unit as the row holds it
		# (review M3). A stacked ("Add on top") line is not cut: the POS prices it at the
		# server's unit rate, and its room depends on the promotion, which the cart holds to
		# the cent and the server exact, so the two cuts could differ by a cent a unit (R1-I1).
		parts = [
			_whole_cents_per_unit([item], [part], [part])[0][0]
			if flt(item["_existing_discount"]) > 0
			else part
			for item, part in zip(chosen, parts, strict=True)
		]

	kept_codes = [item.get("item_code") for item in kept]
	if not chosen:
		return {
			"valid": False,
			"message": (
				_("These items already have the maximum discount allowed")
				if mode == PROMOTION_CONFLICT_ADD
				else _("The promotion already gives more on these items")
			)
			if kept
			else _coupon_rejection_message(coupon, items),
			"eligible_item_codes": [],
			"line_updates": [],
			"total_discount": 0,
			"kept_promotion_items": kept_codes,
		}

	line_updates = []
	for item, part, exact_part in zip(chosen, parts, exact, strict=True):
		base = flt(item["_base_amount"])
		qty = flt(item.get("qty") or item.get("quantity") or 0) or 1
		existing = flt(item["_existing_discount"])
		stacked = mode == PROMOTION_CONFLICT_ADD and existing > 0
		line_discount = existing + part if stacked else part
		if is_percentage and not coupon.max_amount and existing <= 0:
			# Plain uncapped line: keep the coupon as a percentage, so quantity changes scale it
			price_list_rate = flt(item.get("price_list_rate") or 0) or flt(item.get("rate") or 0) or base / qty
			new_rate = price_list_rate * (1.0 - pct / 100.0)
			discount_percentage, discount_amount = flt(pct, 6), 0
		else:
			new_rate = max(base - line_discount, 0) / qty
			discount_percentage, discount_amount = 0, flt(line_discount, 6)
		update = {
			"line_key": item["_line_key"],
			"item_code": item.get("item_code"),
			"discount_percentage": discount_percentage,
			"discount_amount": discount_amount,
			"rate": flt(new_rate, 6),
			"amount": flt(new_rate * qty, 6),
			"coupon_code": coupon.coupon_code,
			"coupon_only_discount": flt(part, 6),
			# The line's own discount before the coupon (a "Better of the two" order coupon tops it up).
			"existing_discount": flt(existing, 6),
			"pre_coupon_discount_fraction": flt(existing / base, 6) if stacked and base else 0,
			# The most the whole-cents cut can lose over this split's lines (review I1).
			"whole_cents_slack": slack,
			# The share before that cut (review R1-M1).
			"coupon_exact_share": flt(exact_part, 6),
		}
		if stacked:
			update["stacked"] = True
		line_updates.append(update)
	total_discount = sum(parts)
	eligible = chosen

	return {
		"valid": True,
		"message": _("Coupon applied successfully"),
		"eligible_item_codes": [i.get("item_code") for i in eligible],
		"line_updates": line_updates,
		"total_discount": flt(total_discount, 6),
		"eligible_subtotal": flt(eligible_subtotal, 6),
		"kept_promotion_items": kept_codes,
	}


def exclusive_tax_rate(taxes=None, pos_profile=None) -> float:
	"""Fraction the exclusive "On Net Total" tax rows add on top of the line amounts.

	Taxes with ``included_in_print_rate`` are already inside the line amounts, so they
	add nothing to a "Grand Total" coupon's base. ``taxes`` are the document's (or the
	cart's) tax rows; ``None`` (a cart that sent none) reads the POS Profile's tax
	template. Other charge types (Actual, On Previous Row …) are not added.
	"""
	if taxes is None:
		template = frappe.db.get_value("POS Profile", pos_profile, "taxes_and_charges") if pos_profile else None
		taxes = (
			frappe.get_all(
				"Sales Taxes and Charges",
				filters={"parent": template, "parenttype": "Sales Taxes and Charges Template"},
				fields=["charge_type", "rate", "included_in_print_rate"],
			)
			if template
			else []
		)
	return (
		sum(
			flt(row.get("rate"))
			for row in taxes or []
			if row.get("charge_type") == "On Net Total" and not cint(row.get("included_in_print_rate"))
		)
		/ 100.0
	)


def apply_coupon_discount(coupon, cart_total, net_total=None, items=None, tax_amount=0, tax_rate=0):
	"""Calculate discount amount based on coupon configuration (legacy cart-level helper).

	With ``items`` the base is the coupon's in-scope, matrix-eligible lines (same scope
	rule as :func:`apply_coupon_to_items`); a "Grand Total" coupon adds ``tax_rate``
	(:func:`exclusive_tax_rate`) of that base, plus any ``tax_amount`` passed in.
	"""
	from posnext_promotions.api.promotion_exclusions import (
		PROMOTION_TARGET_COUPON,
		get_eligible_subtotal,
		get_excluded_subtotal,
		get_line_net_amount,
		mark_item_discount_flags,
	)

	prepared_items = with_item_master_scope(items)
	excluded_brands = _get_excluded_brands(coupon)
	if prepared_items:
		mark_item_discount_flags(prepared_items)

	# Matrix always applies for coupons — never fall back to full cart total
	# when exclude_already_discounted_items is unchecked.
	if prepared_items:
		subtotal_kwargs = {
			"exclude_discounted": True,
			"promotion_target": PROMOTION_TARGET_COUPON,
			"excluded_brands": excluded_brands,
		}
		in_scope = [item for item in prepared_items if _item_matches_scope(coupon, item)]
		out_of_scope = sum(
			get_line_net_amount(item)
			for item in prepared_items
			if not cint(item.get("is_free_item")) and not _item_matches_scope(coupon, item)
		)
		eligible_base = get_eligible_subtotal(in_scope, **subtotal_kwargs)
		excluded_base = get_excluded_subtotal(in_scope, **subtotal_kwargs) + out_of_scope
		if coupon.apply_on == "Grand Total":
			eligible_base += eligible_base * flt(tax_rate)
			eligible_base += flt(tax_amount) if flt(tax_amount) > 0 else 0
		base_amount = eligible_base
	else:
		base_amount = cart_total if coupon.apply_on == "Grand Total" else (net_total or cart_total)
		eligible_base = base_amount
		excluded_base = 0

	# Check minimum amount
	if coupon.min_amount and flt(base_amount) < flt(coupon.min_amount):
		return {
			"valid": False,
			"message": _("Minimum cart amount of {0} is required").format(
				frappe.format_value(coupon.min_amount, {"fieldtype": "Currency"})
			),
			"discount": 0,
			"eligible_subtotal": eligible_base,
			"excluded_subtotal": excluded_base,
		}

	# Calculate discount
	discount = 0
	if coupon.discount_type == "Percentage":
		discount = flt(base_amount) * flt(coupon.discount_percentage) / 100
	elif coupon.discount_type == "Amount":
		discount = flt(coupon.discount_amount)

	# Apply maximum discount limit
	if coupon.max_amount and flt(discount) > flt(coupon.max_amount):
		discount = flt(coupon.max_amount)

	# Ensure discount doesn't exceed cart total
	if discount > base_amount:
		discount = base_amount

	return {
		"valid": True,
		"discount": discount,
		"discount_type": coupon.discount_type,
		"discount_percentage": coupon.discount_percentage if coupon.discount_type == "Percentage" else None,
		"apply_on": coupon.apply_on,
		"eligible_subtotal": eligible_base,
		"excluded_subtotal": excluded_base,
	}


def increment_coupon_usage(coupon_code):
	"""Increment the usage counter for a coupon"""
	try:
		coupon = frappe.get_doc("POS Coupon", {"coupon_code": coupon_code.upper()})
		coupon.used = (coupon.used or 0) + 1
		coupon.db_set("used", coupon.used)
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(
			title="Coupon Usage Increment Failed",
			message=f"Failed to increment usage for coupon {coupon_code}: {e!s}",
		)


def decrement_coupon_usage(coupon_code):
	"""Decrement the usage counter for a coupon (for cancelled invoices)"""
	try:
		coupon = frappe.get_doc("POS Coupon", {"coupon_code": coupon_code.upper()})
		if coupon.used and coupon.used > 0:
			coupon.used = coupon.used - 1
			coupon.db_set("used", coupon.used)
			frappe.db.commit()
	except Exception as e:
		frappe.log_error(
			title="Coupon Usage Decrement Failed",
			message=f"Failed to decrement usage for coupon {coupon_code}: {e!s}",
		)
