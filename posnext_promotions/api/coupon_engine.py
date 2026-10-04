# Copyright (c) 2026, BrainWise and contributors
"""Coupon eligibility/discount helpers. Independent of the POS Coupon DocType class."""

import math

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, today

ONE_USE_COUPON_DOCTYPES = ("Sales Invoice", "POS Invoice")

SCOPE_ALL = "All Eligible Items"
# apply_scope -> (table fieldname, child column, legacy single-value field)
SCOPE_TABLES = {
	"Item Code": ("applicable_items", "item_code", None),
	"Item Group": ("applicable_item_groups", "item_group", "applicable_item_group"),
	"Brand": ("applicable_brands", "brand", "applicable_brand"),
}


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


def get_coupon_eligible_items(coupon, items):
	"""
	Return cart items that pass exclusion rules and scope checks.

	Uses the Promotion Interaction Matrix for discount/coupon eligibility.
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

	exclude_discounted = cint(getattr(coupon, "exclude_already_discounted_items", 1))
	prepared_items = [frappe._dict(row) for row in items]
	all_rule_names: list[str] = []
	for item in prepared_items:
		all_rule_names.extend(_parse_pricing_rules(item.get("pricing_rules")))
	type_map = get_rule_promotion_types(list(set(all_rule_names)))

	if exclude_discounted:
		mark_item_discount_flags(prepared_items, type_map)

	excluded_brands = _get_excluded_brands(coupon)
	scope = CouponScope(coupon, prepared_items)
	eligible = []

	for index, raw in enumerate(prepared_items):
		item = raw if isinstance(raw, dict) else dict(raw)
		if cint(item.get("is_free_item") or 0):
			continue
		if not is_eligible_for_promotion(
			item,
			PROMOTION_TARGET_COUPON,
			rule_type_map=type_map,
			excluded_brands=excluded_brands,
			exclude_discounted=exclude_discounted,
		):
			# Allow re-evaluating lines already tagged with this same coupon
			existing_coupon = (item.get("coupon_code") or "").upper()
			this_code = (coupon.coupon_code or "").upper()
			if not (existing_coupon and this_code and existing_coupon == this_code):
				continue
		if not scope.matches(item):
			continue

		item = dict(item)
		item["_line_key"] = _item_line_key(item, index)
		item["_base_amount"] = _item_base_amount(item)
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


def _coupon_value(coupon, fieldname):
	if isinstance(coupon, dict):
		return coupon.get(fieldname)
	return getattr(coupon, fieldname, None)


def _scope_rows(coupon, fieldname, column):
	values = []
	for row in _coupon_value(coupon, fieldname) or []:
		value = row.get(column) if isinstance(row, dict) else getattr(row, column, None)
		if value and value not in values:
			values.append(value)
	return values


def get_coupon_scope(coupon):
	"""Return (apply_scope, configured values) using the scope tables, falling back to legacy single fields."""
	apply_scope = _coupon_value(coupon, "apply_scope") or SCOPE_ALL
	if apply_scope not in SCOPE_TABLES:
		return SCOPE_ALL, []

	fieldname, column, legacy_field = SCOPE_TABLES[apply_scope]
	values = _scope_rows(coupon, fieldname, column)
	if not values and legacy_field and _coupon_value(coupon, legacy_field):
		values = [_coupon_value(coupon, legacy_field)]
	return apply_scope, values


class CouponScope:
	"""Item matcher for a coupon's Item Code / Item Group / Brand scope.

	Item group, brand and template are read from the Item master so a client cannot
	widen the scope by sending a different item_group or brand on the cart line.
	"""

	def __init__(self, coupon, items=None):
		self.apply_scope, self.values = get_coupon_scope(coupon)
		self.allowed = set(self.values)
		if self.apply_scope == "Item Group":
			self.allowed = _expand_item_groups(self.values)
		self._item_master = self._load_item_master(items or [])

	@property
	def is_restricted(self):
		return self.apply_scope != SCOPE_ALL

	def _load_item_master(self, items):
		if not self.is_restricted:
			return {}
		item_codes = list({item.get("item_code") for item in items if item.get("item_code")})
		if not item_codes:
			return {}
		rows = frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "item_group", "brand", "variant_of"],
		)
		return {row.name: row for row in rows}

	def matches(self, item):
		if not self.is_restricted:
			return True
		if not self.allowed:
			return False

		item_code = item.get("item_code")
		master = self._item_master.get(item_code)
		if self.apply_scope == "Item Code":
			variant_of = master.variant_of if master else item.get("variant_of")
			return item_code in self.allowed or bool(variant_of and variant_of in self.allowed)
		if self.apply_scope == "Item Group":
			item_group = master.item_group if master else item.get("item_group")
			return bool(item_group) and item_group in self.allowed
		if self.apply_scope == "Brand":
			brand = master.brand if master else item.get("brand")
			return bool(brand) and brand in self.allowed
		return False


def _expand_item_groups(item_groups):
	from frappe.utils.nestedset import get_descendants_of

	expanded = set()
	for item_group in item_groups:
		expanded.add(item_group)
		if frappe.db.exists("Item Group", item_group):
			expanded.update(get_descendants_of("Item Group", item_group, ignore_permissions=True))
	return expanded


def validate_pos_coupon_scope(doc, method=None):
	"""POS Coupon validate hook: restricted scopes need at least one row in their table.

	Tables of the scopes not selected are cleared so stale rows never resurface.
	"""
	apply_scope = doc.get("apply_scope") or SCOPE_ALL
	for scope_name, (table, _column, legacy) in SCOPE_TABLES.items():
		if scope_name == apply_scope:
			continue
		if doc.meta.has_field(table) and doc.get(table):
			doc.set(table, [])
		if legacy and doc.meta.has_field(legacy) and doc.get(legacy):
			doc.set(legacy, None)

	if apply_scope not in SCOPE_TABLES:
		return

	fieldname, column, legacy_field = SCOPE_TABLES[apply_scope]
	if not doc.meta.has_field(fieldname):
		return

	if legacy_field and doc.get(legacy_field):
		if not doc.get(fieldname):
			doc.append(fieldname, {column: doc.get(legacy_field)})
		doc.set(legacy_field, None)

	seen = set()
	for row in list(doc.get(fieldname) or []):
		value = row.get(column)
		if value in seen:
			doc.remove(row)
			continue
		seen.add(value)

	if not doc.get(fieldname):
		frappe.throw(
			_("Add at least one row to {0} when Apply Scope is {1}").format(
				_(doc.meta.get_label(fieldname)), _(apply_scope)
			),
			title=_("Coupon Scope"),
		)


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
	scope = CouponScope(coupon, items or [])
	reasons: list[str] = []

	payable_items = [item for item in items or [] if not cint(item.get("is_free_item") or 0)]
	if scope.is_restricted and payable_items and not any(scope.matches(item) for item in payable_items):
		return _("No items in the cart match this coupon's {0} scope").format(_(scope.apply_scope))

	for item in payable_items:
		code = item.get("item_code") or _("Item")
		brand = item.get("brand")
		if brand and brand in excluded_brands:
			reasons.append(_("Item {0} is excluded (brand {1})").format(code, brand))
			continue
		if cint(getattr(coupon, "exclude_already_discounted_items", 1)) and is_coupon_broad_discounted(
			item
		):
			reasons.append(_("Item {0} is already discounted").format(code))
			continue
		state = classify_item_state(
			item,
			excluded_brands=excluded_brands,
			target=PROMOTION_TARGET_COUPON,
			exclude_discounted=cint(getattr(coupon, "exclude_already_discounted_items", 1)),
		)
		if state == "excluded_brand":
			reasons.append(_("Item {0} is excluded (brand {1})").format(code, brand or ""))

	if reasons:
		return "; ".join(reasons[:3])
	return _("No eligible items for this coupon")


def _fixed_discount_line_updates(coupon, eligible, total_discount):
	"""Split a fixed coupon discount across eligible lines by share of their base amount.

	Each line gets a per-unit discount rounded down to currency precision, so the invoice's
	per-unit rates reproduce it exactly. The rounding remainder goes to a qty-1 line when
	there is one; otherwise whole per-unit steps go to the last line and the rest (under
	one currency unit per qty) is dropped. Returns (line_updates, allocated total).
	"""
	precision = frappe.get_precision("Sales Invoice Item", "rate") or 2
	unit = 10**-precision
	subtotal = sum(flt(item["_base_amount"]) for item in eligible)

	def floor_to_unit(value):
		return max(math.floor(flt(value) / unit + 1e-9), 0) * unit

	lines = []
	for item in eligible:
		base = flt(item["_base_amount"])
		qty = flt(item.get("qty") or item.get("quantity") or 0) or 1
		share = base / subtotal if subtotal else 0
		per_unit = min(floor_to_unit(total_discount * share / qty), floor_to_unit(base / qty))
		lines.append({"item": item, "base": base, "qty": qty, "discount": flt(per_unit * qty, precision)})

	remainder = flt(total_discount - sum(line["discount"] for line in lines), precision)
	if remainder > 0 and lines:
		target = next((line for line in lines if line["qty"] == 1), lines[-1])
		room = max(target["base"] - target["discount"], 0)
		step = floor_to_unit(min(remainder, room) / target["qty"]) * target["qty"]
		target["discount"] = flt(target["discount"] + step, precision)

	line_updates = []
	for line in lines:
		item, base, qty, discount = line["item"], line["base"], line["qty"], line["discount"]
		new_amount = max(base - discount, 0)
		line_updates.append(
			{
				"line_key": item["_line_key"],
				"item_code": item.get("item_code"),
				"discount_percentage": 0,
				"discount_amount": discount,
				"rate": flt(new_amount / qty, 6),
				"amount": flt(new_amount, 6),
				"coupon_code": coupon.coupon_code,
			}
		)
	return line_updates, flt(sum(line["discount"] for line in lines), precision)


def apply_coupon_to_items(coupon, items):
	"""
	Calculate per-line coupon discounts for eligible items.

	Returns dict with valid, message, eligible_item_codes, line_updates, total_discount.
	"""
	eligible = get_coupon_eligible_items(coupon, items)
	if not eligible:
		return {
			"valid": False,
			"message": _coupon_rejection_message(coupon, items),
			"eligible_item_codes": [],
			"line_updates": [],
			"total_discount": 0,
		}

	eligible_subtotal = sum(flt(i["_base_amount"]) for i in eligible)

	if coupon.min_amount and flt(eligible_subtotal) < flt(coupon.min_amount):
		return {
			"valid": False,
			"message": _("Minimum eligible amount of {0} is required").format(
				frappe.format_value(coupon.min_amount, {"fieldtype": "Currency"})
			),
			"eligible_item_codes": [i.get("item_code") for i in eligible],
			"line_updates": [],
			"total_discount": 0,
		}

	line_updates = []
	total_discount = 0.0

	if coupon.discount_type == "Percentage":
		pct = flt(coupon.discount_percentage)
		for item in eligible:
			base = flt(item["_base_amount"])
			line_discount = flt(base) * pct / 100.0
			total_discount += line_discount
			qty = flt(item.get("qty") or item.get("quantity") or 0) or 1
			price_list_rate = flt(item.get("price_list_rate") or 0)
			if price_list_rate <= 0:
				price_list_rate = flt(item.get("rate") or 0) or (base / qty)
			new_rate = price_list_rate * (1 - pct / 100.0)
			line_updates.append(
				{
					"line_key": item["_line_key"],
					"item_code": item.get("item_code"),
					"discount_percentage": pct,
					"discount_amount": 0,
					"rate": flt(new_rate, 6),
					"amount": flt(new_rate * qty, 6),
					"coupon_code": coupon.coupon_code,
				}
			)

		# When max_amount is configured, always materialize as absolute amounts.
		# Otherwise local qty changes recalculate % and can exceed the cap before
		# the next server revalidation.
		if coupon.max_amount:
			total_discount = min(total_discount, flt(coupon.max_amount))
			line_updates, total_discount = _fixed_discount_line_updates(coupon, eligible, total_discount)
	else:
		total_discount = flt(coupon.discount_amount)
		if coupon.max_amount and total_discount > flt(coupon.max_amount):
			total_discount = flt(coupon.max_amount)
		total_discount = min(total_discount, eligible_subtotal)
		line_updates, total_discount = _fixed_discount_line_updates(coupon, eligible, total_discount)

	return {
		"valid": True,
		"message": _("Coupon applied successfully"),
		"eligible_item_codes": [i.get("item_code") for i in eligible],
		"line_updates": line_updates,
		"total_discount": flt(total_discount, 6),
		"eligible_subtotal": flt(eligible_subtotal, 6),
	}


def apply_coupon_discount(coupon, cart_total, net_total=None, items=None, tax_amount=0):
	"""Calculate discount amount based on coupon configuration (legacy cart-level helper)."""
	from posnext_promotions.api.promotion_exclusions import (
		PROMOTION_TARGET_COUPON,
		get_eligible_subtotal,
		get_excluded_subtotal,
		get_line_net_amount,
		mark_item_discount_flags,
	)

	prepared_items = [frappe._dict(row) for row in (items or [])]
	excluded_brands = _get_excluded_brands(coupon)
	if prepared_items:
		mark_item_discount_flags(prepared_items)

	exclude_discounted = cint(getattr(coupon, "exclude_already_discounted_items", 1))

	if prepared_items and exclude_discounted:
		subtotal_kwargs = {
			"exclude_discounted": True,
			"promotion_target": PROMOTION_TARGET_COUPON,
			"excluded_brands": excluded_brands,
		}
		if coupon.apply_on == "Grand Total":
			eligible_base = get_eligible_subtotal(prepared_items, **subtotal_kwargs)
			eligible_base += flt(tax_amount) if flt(tax_amount) > 0 else 0
			excluded_base = get_excluded_subtotal(prepared_items, **subtotal_kwargs)
		else:
			eligible_base = get_eligible_subtotal(prepared_items, **subtotal_kwargs)
			excluded_base = get_excluded_subtotal(prepared_items, **subtotal_kwargs)
		base_amount = eligible_base
	else:
		base_amount = cart_total if coupon.apply_on == "Grand Total" else (net_total or cart_total)
		eligible_base = base_amount
		excluded_base = 0

	scope = CouponScope(coupon, prepared_items)
	if scope.is_restricted:
		# Taxes cannot be attributed to a subset of lines, so scoped coupons use line net amounts only.
		in_scope = [item for item in prepared_items if scope.matches(item)]
		eligible_base = get_eligible_subtotal(
			in_scope,
			exclude_discounted=bool(exclude_discounted),
			promotion_target=PROMOTION_TARGET_COUPON,
			excluded_brands=excluded_brands,
		)
		payable_total = sum(
			get_line_net_amount(item) for item in prepared_items if not cint(item.get("is_free_item") or 0)
		)
		excluded_base = max(flt(payable_total) - flt(eligible_base), 0)
		base_amount = eligible_base

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


def validate_invoice_coupon_lines(doc, method=None):
	"""Sales / POS Invoice validate hook for scoped POS Coupons.

	Lines a scoped coupon discounted carry posnext_coupon_code: they must be in scope and
	together stay within the coupon's discount. An untagged line discount is a manual
	cashier discount (governed by the POS Profile), so a scoped coupon on the invoice must
	have at least one tagged line, or a header discount from the cart-level path.
	"""
	if doc.get("is_return"):
		return

	tagged = [row for row in doc.get("items") or [] if row.get("posnext_coupon_code")]
	invoice_code = (doc.get("coupon_code") or "").upper()
	if not tagged and not invoice_code:
		return
	if not frappe.db.exists("DocType", "POS Coupon"):
		return
	if not tagged:
		_validate_untagged_scoped_coupon(doc, invoice_code)
		return

	line_codes = {(row.posnext_coupon_code or "").upper() for row in tagged}
	if not invoice_code or line_codes != {invoice_code}:
		frappe.throw(
			_("Coupon discount on items does not match the coupon applied to the invoice"),
			title=_("Coupon Scope"),
		)

	coupon_name = frappe.db.get_value("POS Coupon", {"coupon_code": invoice_code}, "name")
	if not coupon_name:
		frappe.throw(_("Coupon {0} does not exist").format(invoice_code), title=_("Coupon Scope"))
	coupon = frappe.get_doc("POS Coupon", coupon_name)

	items = [_invoice_row_as_cart_item(row) for row in doc.get("items") or []]
	scope = CouponScope(coupon, items)
	for row in tagged:
		if not scope.matches(_invoice_row_as_cart_item(row)):
			frappe.throw(
				_("Row {0}: Item {1} is outside the {2} scope of coupon {3}").format(
					row.idx, row.item_code, _(scope.apply_scope), invoice_code
				),
				title=_("Coupon Scope"),
			)

	result = apply_coupon_to_items(coupon, items)
	if not result.get("valid"):
		frappe.throw(result.get("message") or _("Coupon {0} is not applicable").format(invoice_code))

	eligible_lines = {update["line_key"] for update in result.get("line_updates") or []}
	actual_discount = 0.0
	for index, row in enumerate(doc.get("items") or []):
		if not row.get("posnext_coupon_code"):
			continue
		if index not in eligible_lines:
			frappe.throw(
				_("Row {0}: Item {1} is not eligible for coupon {2}").format(
					row.idx, row.item_code, invoice_code
				),
				title=_("Coupon Scope"),
			)
		actual_discount += max(flt(row.price_list_rate) - flt(row.rate), 0) * flt(row.qty)

	precision = frappe.get_precision("Sales Invoice Item", "rate") or 2
	# Rates are rounded to the nearest currency unit (half a unit per qty), plus the total's own rounding.
	unit = 10**-precision
	tolerance = sum(abs(flt(row.qty)) for row in tagged) * unit / 2 + unit
	allowed_discount = flt(result.get("total_discount"))
	if flt(actual_discount, precision) > flt(allowed_discount + tolerance, precision):
		frappe.throw(
			_("Coupon {0} allows a discount of {1} but the items carry {2}").format(
				invoice_code,
				frappe.format_value(
					allowed_discount, {"fieldtype": "Currency"}, currency=doc.get("currency")
				),
				frappe.format_value(actual_discount, {"fieldtype": "Currency"}, currency=doc.get("currency")),
			),
			title=_("Coupon Scope"),
		)


def _validate_untagged_scoped_coupon(doc, invoice_code):
	coupon = frappe.db.get_value(
		"POS Coupon", {"coupon_code": invoice_code}, ["name", "apply_scope"], as_dict=True
	)
	if not coupon or coupon.apply_scope not in SCOPE_TABLES:
		return
	if flt(doc.get("discount_amount")) or flt(doc.get("additional_discount_percentage")):
		return
	frappe.throw(
		_(
			"Coupon {0} applies to specific items, but no item carries its discount. Please re-apply the coupon."
		).format(invoice_code),
		title=_("Coupon Scope"),
	)


def _invoice_row_as_cart_item(row):
	return {
		"item_code": row.item_code,
		"item_group": row.get("item_group"),
		"brand": row.get("brand"),
		"qty": row.qty,
		"price_list_rate": row.price_list_rate,
		"rate": row.rate,
		"amount": row.amount,
		"discount_percentage": row.discount_percentage,
		"discount_amount": row.discount_amount,
		"pricing_rules": row.get("pricing_rules"),
		"is_free_item": row.get("is_free_item"),
		"coupon_code": row.get("posnext_coupon_code"),
	}
