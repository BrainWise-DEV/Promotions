# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Item scope for Friends & Family: what the Item master says, never the client line.

A cart or invoice line's ``item_group``, ``brand`` and ``variant_of`` decide which
scope and exclusion rows match it, so they are always overwritten from Item
(:func:`item_scope_fields`, one query for all lines, cached for the request).

Variants follow their template in F&F matching only: a scope or exclusion row on
template ``T`` matches a variant of ``T`` (:func:`scope_keys`), and a variant with a
blank group or brand is matched on the template's (:func:`scope_item_group`,
:func:`scope_brand`). ``item_group`` / ``brand`` on the line stay the Item's own
values, as ERPNext's Pricing Rules and the stored invoice rows see them.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr
from frappe.utils.caching import request_cache

from posnext_promotions.promotions.scope import ITEM_GROUP_CACHE_PREFIX

# Overwritten from Item on every line. ``template_*``: the template's values (F&F only).
SCOPE_FIELDS = ("item_group", "brand", "variant_of", "template_item_group", "template_brand")


def item_scope_fields(item_codes) -> dict[str, dict]:
	"""``{code: {item_group, brand, variant_of, template_item_group, template_brand, item_name, stock_uom}}``.

	``item_group`` / ``brand`` are the Item's own; ``template_*`` are its template's
	(None when not a variant). Unknown codes are absent.
	"""
	codes = tuple(sorted({cstr(code) for code in item_codes or [] if cstr(code)}))
	return _load_scope_fields(codes) if codes else {}


@request_cache
def _load_scope_fields(codes: tuple) -> dict[str, dict]:
	# request_cache keys on the arguments: ``codes`` is a sorted tuple so it hashes.
	item = frappe.qb.DocType("Item")
	template = frappe.qb.DocType("Item").as_("template")
	rows = (
		frappe.qb.from_(item)
		.left_join(template)
		.on(template.name == item.variant_of)
		.select(
			item.name,
			item.item_name,
			item.stock_uom,
			item.variant_of,
			item.item_group,
			item.brand,
			template.item_group.as_("template_item_group"),
			template.brand.as_("template_brand"),
		)
		.where(item.name.isin(codes))
	).run(as_dict=True)
	return {
		row.name: {
			"item_group": row.item_group or None,
			"brand": row.brand or None,
			"variant_of": row.variant_of or None,
			"template_item_group": row.template_item_group or None,
			"template_brand": row.template_brand or None,
			"item_name": row.item_name,
			"stock_uom": row.stock_uom,
		}
		for row in rows
	}


def scope_keys(item_code, variant_of=None) -> tuple[str, ...]:
	"""The codes a scope or exclusion row may name for this line: the item and its template.

	``variant_of`` comes from :func:`item_scope_fields` (lines carry it once
	enriched), so matching a line costs no query.
	"""
	return tuple(code for code in (cstr(item_code), cstr(variant_of)) if code)


def scope_item_group(item) -> str:
	"""The group F&F matches a line on: its own, else its template's."""
	return cstr(item.get("item_group") or item.get("template_item_group"))


def scope_brand(item) -> str:
	"""The brand F&F matches a line on: its own, else its template's."""
	return cstr(item.get("brand") or item.get("template_brand"))


def clear_item_group_cache(doc=None, method=None, *args) -> None:
	"""Item Group ``on_update`` / ``on_trash`` / ``after_rename``: forget every expanded tree.

	Every prefix key goes, not only ``doc``'s: adding a child moves its ancestors'
	``lft`` / ``rgt`` too. Cleared again after commit (as ``frappe.clear_document_cache``
	does): another worker may cache the old tree before this transaction commits.
	The program built for this request holds expanded groups, so cached
	eligibility goes as well.
	"""
	from posnext_promotions.promotions.friends_and_family_eligibility import clear_cache

	_delete_item_group_trees()
	frappe.db.after_commit.add(_delete_item_group_trees)
	clear_cache()


def _delete_item_group_trees() -> None:
	frappe.cache().delete_keys(ITEM_GROUP_CACHE_PREFIX)
