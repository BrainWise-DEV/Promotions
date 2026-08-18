# Copyright (c) 2026, BrainWise and contributors
"""Merge packed_items from free product-bundle lines into the paid bundle line."""

import frappe
from frappe.utils import cint, flt


def _find_paid_bundle_row_for_free(si_doc, free_row):
	candidates = []
	for row in si_doc.get("items"):
		if row.name == free_row.name or cint(row.is_free_item):
			continue
		if row.item_code != free_row.item_code:
			continue
		if (row.warehouse or "") != (free_row.warehouse or ""):
			continue
		candidates.append(row)
	if not candidates:
		return None
	free_idx = free_row.idx or 0
	before = [r for r in candidates if (r.idx or 0) < free_idx]
	if before:
		return max(before, key=lambda r: r.idx or 0)
	return candidates[0]


def _find_matching_packed_item_for_merge(si_doc, paid_row, component_item_code, warehouse):
	w = warehouse or ""
	matches = []
	for pi in si_doc.get("packed_items"):
		if pi.parent_detail_docname != paid_row.name:
			continue
		if pi.parent_item != paid_row.item_code:
			continue
		if pi.item_code != component_item_code:
			continue
		matches.append(pi)
	if not matches:
		return None
	for pi in matches:
		if (pi.warehouse or "") == w:
			return pi
	return matches[0]


def combine_packed_qty_for_free_product_bundles(doc, method=None):
	"""Sales Invoice validate/on_update hook — does not subclass Sales Invoice."""
	if doc.get("is_return") or not doc.get("packed_items"):
		return

	def has_product_bundle(item_code):
		return bool(frappe.db.exists("Product Bundle", {"new_item_code": item_code, "disabled": 0}))

	free_bundle_rows = [
		row
		for row in doc.get("items")
		if row.item_code and cint(row.is_free_item) and has_product_bundle(row.item_code)
	]
	if not free_bundle_rows:
		return

	for free_row in free_bundle_rows:
		paid_row = _find_paid_bundle_row_for_free(doc, free_row)
		if not paid_row:
			continue

		to_remove = []
		for pi in list(doc.get("packed_items")):
			if pi.parent_detail_docname != free_row.name or pi.parent_item != free_row.item_code:
				continue
			tgt = _find_matching_packed_item_for_merge(doc, paid_row, pi.item_code, pi.warehouse)
			if tgt:
				prec = tgt.precision("qty")
				tgt.qty = flt(flt(tgt.qty) + flt(pi.qty), prec)
				to_remove.append(pi)

		for pi in to_remove:
			doc.remove(pi)
