# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family demo / QA seed data (PN-133).

Creates (idempotently) everything the manual QA script in
``docs/friends_and_family_acceptance.md`` needs on a **development** site:

* Customer groups ``<prefix> Staff Group`` (eligible) and ``<prefix> Public Group``.
* Customers ``<prefix> Staff`` (eligible by group), ``<prefix> Flagged``
  (eligible by the Friends & Family Member flag) and ``<prefix> Public``.
* Brands ``<prefix> Brand A`` (covered) and ``<prefix> Brand X`` (excluded).
* Item group ``<prefix> Products`` with priced items (Item Price in the
  selling price list):

  ======================  ======  =======  ==========================
  Item                    Price   Brand    Purpose
  ======================  ======  =======  ==========================
  ``<prefix> Laptop``     1000    A        covered, caps
  ``<prefix> Phone``       500    A        covered
  ``<prefix> Cable``       100    A        promotion 10% (F&F wins)
  ``<prefix> Charger``     200    A        promotion 40% (promo wins)
  ``<prefix> Mouse``       100    A        promotion 30% (tie → F&F)
  ``<prefix> Gift Card``   300    A        excluded item
  ``<prefix> Headset``     400    X        excluded brand
  ======================  ======  =======  ==========================

* Pricing Rules (item-level promotions) for Cable / Charger / Mouse.
* POS Coupons ``<CODE>15`` (15%) and ``<CODE>40`` (40%) when the POS Coupon
  DocType (POS Next) is installed.
* Friends and Family Settings: enabled, 30%, Entire Cart, eligibility
  "Customer Group or Flag", excluded item Gift Card, excluded brand X, no caps.

Run on a dev site only::

    bench --site <dev-site> execute posnext_promotions.promotions.friends_and_family_demo.setup_demo_data

It refuses to run unless ``developer_mode`` is on. Re-running resets the demo
records to the values above (Settings included) without duplicating anything.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt

from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
	ensure_customer_flag_field,
)

DEFAULT_PREFIX = "FF Demo"
DISCOUNT_PERCENTAGE = 30

ITEMS = (
	# key, label, price, brand key, promotion %
	("laptop", "Laptop", 1000, "a", None),
	("phone", "Phone", 500, "a", None),
	("cable", "Cable", 100, "a", 10),
	("charger", "Charger", 200, "a", 40),
	("mouse", "Mouse", 100, "a", 30),
	("gift_card", "Gift Card", 300, "a", None),
	("headset", "Headset", 400, "x", None),
)
COUPONS = (("15", 15), ("40", 40))


@frappe.whitelist()
def setup_demo_data(company: str | None = None, price_list: str | None = None, prefix: str | None = None) -> dict:
	"""Seed the F&F demo data on a development site (System Manager, developer_mode only)."""
	if not cint_conf("developer_mode"):
		frappe.throw(
			_("Friends & Family demo data can only be created when developer_mode is enabled"),
			frappe.PermissionError,
		)
	frappe.only_for("System Manager")
	result = build_demo_data(prefix=prefix or DEFAULT_PREFIX, company=company, price_list=price_list)
	frappe.db.commit()  # nosemgrep — explicit seed command
	return result


def cint_conf(key: str) -> int:
	try:
		return int(frappe.conf.get(key) or 0)
	except (TypeError, ValueError):
		return 0


def _root(doctype: str, parent_field: str, fallback: str) -> str:
	return frappe.db.get_value(doctype, {"is_group": 1, parent_field: ["in", ["", None]]}) or fallback


def _ensure(doctype: str, name: str, values: dict) -> str:
	if frappe.db.exists(doctype, name):
		doc = frappe.get_doc(doctype, name)
		changed = {k: v for k, v in values.items() if cstr(doc.get(k)) != cstr(v)}
		if changed:
			doc.update(changed)
			doc.save(ignore_permissions=True)
		return name
	return frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True).name


def _code_prefix(prefix: str) -> str:
	return "".join(ch for ch in prefix.upper() if ch.isalnum())[:10] or "FFDEMO"


def build_demo_data(prefix: str = DEFAULT_PREFIX, company: str | None = None, price_list: str | None = None) -> dict:
	"""Idempotently create the demo records (no developer_mode check — used by the acceptance tests)."""
	ensure_customer_flag_field()
	company = company or frappe.defaults.get_global_default("company") or frappe.db.get_value("Company", {})
	price_list = price_list or frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"

	cg_root = _root("Customer Group", "parent_customer_group", "All Customer Groups")
	staff_group = _ensure(
		"Customer Group",
		f"{prefix} Staff Group",
		{"customer_group_name": f"{prefix} Staff Group", "parent_customer_group": cg_root, "is_group": 0},
	)
	public_group = _ensure(
		"Customer Group",
		f"{prefix} Public Group",
		{"customer_group_name": f"{prefix} Public Group", "parent_customer_group": cg_root, "is_group": 0},
	)
	customers = {}
	for key, group, flag in (("staff", staff_group, 0), ("flagged", public_group, 1), ("public", public_group, 0)):
		name = f"{prefix} {key.title()}"
		if not frappe.db.exists("Customer", name):
			frappe.get_doc(
				{"doctype": "Customer", "customer_name": name, "customer_group": group, "customer_type": "Individual"}
			).insert(ignore_permissions=True)
		# Direct write: the flag guard (manager-only) is about cashiers, not this seed.
		frappe.db.set_value("Customer", name, {"customer_group": group, FLAG_FIELD: flag, "disabled": 0})
		customers[key] = name

	brands = {key: _ensure("Brand", f"{prefix} Brand {key.upper()}", {"brand": f"{prefix} Brand {key.upper()}"}) for key in ("a", "x")}
	ig_root = _root("Item Group", "parent_item_group", "All Item Groups")
	item_group = _ensure(
		"Item Group",
		f"{prefix} Products",
		{"item_group_name": f"{prefix} Products", "parent_item_group": ig_root, "is_group": 0},
	)

	items, pricing_rules = {}, {}
	for key, label, price, brand_key, promo in ITEMS:
		code = f"{prefix} {label}"
		_ensure(
			"Item",
			code,
			{
				"item_code": code,
				"item_name": code,
				"item_group": item_group,
				"brand": brands[brand_key],
				"stock_uom": "Nos",
				"is_stock_item": 0,
			},
		)
		price_name = frappe.db.get_value("Item Price", {"item_code": code, "price_list": price_list})
		if price_name:
			frappe.db.set_value("Item Price", price_name, "price_list_rate", price)
		else:
			frappe.get_doc(
				{"doctype": "Item Price", "item_code": code, "price_list": price_list, "price_list_rate": price}
			).insert(ignore_permissions=True)
		items[key] = code
		if promo:
			pricing_rules[key] = _ensure_pricing_rule(f"{prefix} {label} {promo}%", code, promo, company)

	coupons = _ensure_coupons(prefix, company)

	settings = frappe.get_single("Friends and Family Settings")
	settings.update(
		{
			"enabled": 1,
			"discount_percentage": DISCOUNT_PERCENTAGE,
			"discount_scope": "Entire Cart",
			"start_date": None,
			"end_date": None,
			"eligibility_basis": "Customer Group or Flag",
			"eligible_customer_groups": [{"customer_group": staff_group}],
			"applicable_brands": [],
			"applicable_item_groups": [],
			"applicable_items": [],
			"excluded_items": [{"item_code": items["gift_card"]}],
			"excluded_item_groups": [],
			"excluded_brands": [{"brand": brands["x"]}],
			"max_discount_per_order": 0,
			"max_discount_per_customer_month": 0,
			"user_message": "Friends & Family discount applied / تم تطبيق خصم الأصدقاء والعائلة",
		}
	)
	settings.save(ignore_permissions=True)

	return {
		"company": company,
		"price_list": price_list,
		"customer_groups": {"staff": staff_group, "public": public_group},
		"customers": customers,
		"brands": brands,
		"item_group": item_group,
		"items": items,
		"prices": {key: price for key, _l, price, _b, _p in ITEMS},
		"pricing_rules": pricing_rules,
		"coupons": coupons,
	}


def _ensure_pricing_rule(title: str, item_code: str, percentage: float, company: str) -> str:
	name = frappe.db.get_value("Pricing Rule", {"title": title, "company": company})
	if name:
		doc = frappe.get_doc("Pricing Rule", name)
		if flt(doc.discount_percentage) != flt(percentage) or doc.disable:
			doc.discount_percentage = percentage
			doc.disable = 0
			doc.save(ignore_permissions=True)
		return name
	return (
		frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": title,
				"apply_on": "Item Code",
				"items": [{"item_code": item_code}],
				"selling": 1,
				"company": company,
				"price_or_product_discount": "Price",
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": percentage,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _ensure_coupons(prefix: str, company: str) -> dict:
	if not frappe.db.exists("DocType", "POS Coupon"):
		return {}
	coupons = {}
	for suffix, pct in COUPONS:
		code = f"{_code_prefix(prefix)}{suffix}"
		name = frappe.db.get_value("POS Coupon", {"coupon_code": code})
		values = {
			"coupon_name": f"{prefix} Coupon {pct}%",
			"coupon_type": "Promotional",
			"coupon_code": code,
			"company": company,
			"discount_type": "Percentage",
			"discount_percentage": pct,
			"apply_on": "Net Total",
			"apply_scope": "All Eligible Items",
			"exclude_already_discounted_items": 1,
			"disabled": 0,
		}
		if name:
			doc = frappe.get_doc("POS Coupon", name)
			doc.update(values)
			doc.save(ignore_permissions=True)
		else:
			name = frappe.get_doc({"doctype": "POS Coupon", **values}).insert(ignore_permissions=True).name
		coupons[suffix] = code
	return coupons
