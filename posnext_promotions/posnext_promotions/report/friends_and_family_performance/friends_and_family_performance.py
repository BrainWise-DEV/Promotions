# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family performance (PN-132): program cost and usage.

Money comes from the invoices themselves, so the totals reconcile with the
invoices that used F&F:

* Rows: Sales Invoice Item / POS Invoice Item with
  ``posnext_discount_source = "friends_and_family"`` on **submitted** invoices
  (``docstatus = 1`` — drafts and cancelled invoices never count) posted in the
  period. Consolidated Sales Invoices (merged POS Invoices) are skipped so a
  sale is not counted twice.
* ``F&F Discount`` = ``posnext_fnf_discount_amount`` × invoice conversion rate
  (company currency). ``Net Sales`` = row ``base_net_amount``. ``Gross`` =
  Net + F&F Discount (what the lines would have sold for without F&F).
* Returns / credit notes carry negative qty, amounts and F&F discount, and are
  counted on **their own** posting date, so every figure is net of returns.
  (The PN-129 monthly cap credits a return to the original month instead —
  that is a cap rule, not accounting.)

Decision statistics (F&F vs coupon / promotion, cap hits) come from the
**Friends and Family Log** (PN-131), excluding entries of cancelled invoices.
They are counted per invoice: an invoice counts once as "F&F won vs coupon"
when F&F beat a coupon on any line or at order level.

Views: Summary (by period), Invoices, Items (top items), Customers (incl. cap
hits), Comparisons (F&F vs coupon / promotion / manual).
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from posnext_promotions.promotions.friends_and_family_caps import FIELD_DISCOUNT_SOURCE, FIELD_FNF_AMOUNT

FNF = "friends_and_family"
LOG_DOCTYPE = "Friends and Family Log"
INVOICE_DOCTYPES = ("Sales Invoice", "POS Invoice")

VIEW_SUMMARY = "Summary"
VIEW_INVOICES = "Invoices"
VIEW_ITEMS = "Items"
VIEW_CUSTOMERS = "Customers"
VIEW_COMPARISONS = "Comparisons"

COMPETITORS = {
	"Coupon": ("coupon", "order_coupon"),
	"Promotion": ("promotion", "order_promotion"),
	"Manual Discount": ("manual", "order_manual"),
}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate(filters)
	rows = get_fnf_rows(filters)
	decisions = get_decision_stats(filters, rows)
	view = filters.get("view") or VIEW_SUMMARY

	builders = {
		VIEW_SUMMARY: _summary_view,
		VIEW_INVOICES: _invoices_view,
		VIEW_ITEMS: _items_view,
		VIEW_CUSTOMERS: _customers_view,
		VIEW_COMPARISONS: _comparisons_view,
	}
	columns, data, chart = builders.get(view, _summary_view)(filters, rows, decisions)
	total = _total_row(view, rows, decisions, columns)
	if total and data:
		data.append(total)
	return columns, data, None, chart, get_report_summary(rows, decisions)


def _total_row(view, rows, decisions, columns) -> dict | None:
	"""Computed totals (unique counts / averages are recomputed, not summed).

	Always covers **every** F&F row in the filters (also with Top N), so the
	total reconciles with the invoices.
	"""
	if view == VIEW_COMPARISONS:
		return None
	bucket = _blank()
	for row in rows:
		_add(bucket, row)
	values = _sales_values(bucket)
	counts = _decision_counts(decisions)
	total = {
		**values,
		"avg_discount": values["discount"] / values["orders"] if values["orders"] else 0.0,
		"cap_hits": counts["cap_hits"],
		"fnf_won_coupon": counts["won_Coupon"],
		"coupon_won": counts["lost_Coupon"],
		"fnf_won_promotion": counts["won_Promotion"],
		"promotion_won": counts["lost_Promotion"],
		"lines": len(rows),
		"is_total_row": 1,
	}
	if view == VIEW_INVOICES:
		total["orders"] = len(bucket["orders"]) + len(bucket["returns"])
	total[columns[0]["fieldname"]] = _("Total")
	return {k: v for k, v in total.items() if k in {c["fieldname"] for c in columns} or k == "is_total_row"}


def _validate(filters):
	if not filters.get("from_date") or not filters.get("to_date"):
		frappe.throw(_("From Date and To Date are required"))
	if getdate(filters.from_date) > getdate(filters.to_date):
		frappe.throw(_("From Date cannot be after To Date"))


# ---- data -----------------------------------------------------------------


def _item_groups(item_group) -> list[str] | None:
	if not item_group:
		return None
	from frappe.utils.nestedset import get_descendants_of

	return [item_group, *get_descendants_of("Item Group", item_group, ignore_permissions=True)]


def get_fnf_rows(filters) -> list[dict]:
	"""F&F invoice lines (sales and returns) in company currency."""
	rows = []
	groups = _item_groups(filters.get("item_group"))
	for doctype in INVOICE_DOCTYPES:
		if not frappe.db.has_column(f"{doctype} Item", FIELD_FNF_AMOUNT):
			continue
		conditions = [
			"p.docstatus = 1",
			f"i.`{FIELD_DISCOUNT_SOURCE}` = %(fnf)s",
			"p.posting_date between %(from_date)s and %(to_date)s",
		]
		if frappe.db.has_column(doctype, "is_consolidated"):
			conditions.append("ifnull(p.is_consolidated, 0) = 0")
		for field in ("company", "customer", "pos_profile"):
			if filters.get(field):
				conditions.append(f"p.{field} = %({field})s")
		if groups:
			conditions.append("i.item_group in %(item_groups)s")
		if filters.get("brand"):
			conditions.append("i.brand = %(brand)s")
		rows += frappe.db.sql(
			f"""
			select %(doctype)s as doctype, p.name as invoice, p.posting_date, p.customer,
				p.customer_name, p.company, p.pos_profile, ifnull(p.is_return, 0) as is_return,
				p.return_against, i.item_code, i.item_name, i.item_group, i.brand, i.qty,
				i.base_net_amount as net,
				i.`{FIELD_FNF_AMOUNT}` * ifnull(nullif(p.conversion_rate, 0), 1) as discount
			from `tab{doctype} Item` i
			join `tab{doctype}` p on p.name = i.parent
			where {" and ".join(conditions)}
			order by p.posting_date, p.name, i.idx
			""",
			{
				**filters,
				"doctype": doctype,
				"fnf": FNF,
				"item_groups": tuple(groups or ()),
			},
			as_dict=True,
		)
	for row in rows:
		row.net = flt(row.net)
		row.discount = flt(row.discount)
		row.gross = row.net + row.discount
	return rows


def _log_rows(filters) -> list[dict]:
	conditions = [
		"l.is_cancelled = 0",
		"l.posting_date between %(from_date)s and %(to_date)s",
		"l.reference_doctype in ('Sales Invoice', 'POS Invoice')",
		"l.log_type in ('Discount Applied', 'Comparison Lost', 'Cap Hit', 'Order Comparison')",
	]
	for field in ("company", "customer", "pos_profile"):
		if filters.get(field):
			conditions.append(f"l.{field} = %({field})s")
	return frappe.db.sql(
		f"""
		select l.log_type, l.reference_doctype, l.reference_name, l.posting_date, l.customer,
			l.item_code, it.item_group, it.brand, l.discount_source, l.competing_source, l.reason, l.cap_hit
		from `tab{LOG_DOCTYPE}` l
		left join `tabItem` it on it.name = l.item_code
		where {" and ".join(conditions)}
		""",
		filters,
		as_dict=True,
	)


def _competitor(source) -> str | None:
	for label, sources in COMPETITORS.items():
		if source in sources:
			return label
	return None


def get_decision_stats(filters, rows) -> dict:
	"""Per-invoice outcomes from the audit log.

	Returns ``{"invoices": {(doctype, name): {...}}, "lines": {competitor: [fnf_won, other_won]}}``.
	"""
	groups = set(_item_groups(filters.get("item_group")) or [])
	brand = filters.get("brand")
	item_filtered = bool(groups or brand)

	def item_ok(log):
		if not log.item_code:
			return False
		return (not groups or log.item_group in groups) and (not brand or log.brand == brand)

	logs = _log_rows(filters)
	# With an item filter, order-level entries count for invoices that have a matching line.
	matching = {(lg.reference_doctype, lg.reference_name) for lg in logs if item_ok(lg)}

	invoices = defaultdict(
		lambda: {"won": set(), "lost": set(), "cap_hit": set(), "customer": None, "posting_date": None}
	)
	lines = {label: [0, 0] for label in COMPETITORS}
	for log in logs:
		key = (log.reference_doctype, log.reference_name)
		if item_filtered and not (item_ok(log) or (not log.item_code and key in matching)):
			continue
		inv = invoices[key]
		inv["customer"] = log.customer
		inv["posting_date"] = log.posting_date
		if log.log_type == "Cap Hit":
			inv["cap_hit"].add(log.cap_hit or "cap")
			continue
		if log.log_type == "Order Comparison":
			fnf_won = log.discount_source == FNF
			rival = _competitor(log.competing_source if fnf_won else log.discount_source)
			if rival:
				(inv["won"] if fnf_won else inv["lost"]).add(rival)
			continue
		if log.reason == "order_discount_better":
			continue  # counted through the Order Comparison entry
		if log.log_type == "Discount Applied":
			rival = _competitor(log.competing_source)
			if rival:
				inv["won"].add(rival)
				lines[rival][0] += 1
		elif log.log_type == "Comparison Lost":
			rival = _competitor(log.discount_source)
			if rival:
				inv["lost"].add(rival)
				lines[rival][1] += 1
	return {"invoices": dict(invoices), "lines": lines}


# ---- aggregation ----------------------------------------------------------


def _blank():
	return {
		"orders": set(),
		"returns": set(),
		"customers": set(),
		"qty": 0.0,
		"gross": 0.0,
		"discount": 0.0,
		"net": 0.0,
	}


def _add(bucket, row):
	key = (row.doctype, row.invoice)
	(bucket["returns"] if row.is_return else bucket["orders"]).add(key)
	bucket["customers"].add(row.customer)
	bucket["qty"] += flt(row.qty)
	bucket["gross"] += row.gross
	bucket["discount"] += row.discount
	bucket["net"] += row.net


def _decision_counts(decisions, keys=None, predicate=None) -> dict:
	counts = {"cap_hits": 0, "cap_customers": set()}
	for label in COMPETITORS:
		counts[f"won_{label}"] = 0
		counts[f"lost_{label}"] = 0
	for key, inv in decisions["invoices"].items():
		if keys is not None and key not in keys:
			continue
		if predicate and not predicate(key, inv):
			continue
		if inv["cap_hit"]:
			counts["cap_hits"] += 1
			counts["cap_customers"].add(inv["customer"])
		for label in inv["won"]:
			counts[f"won_{label}"] += 1
		for label in inv["lost"]:
			counts[f"lost_{label}"] += 1
	return counts


def _money(fieldname, label, width=130):
	return {"fieldname": fieldname, "label": label, "fieldtype": "Currency", "options": "currency", "width": width}


def _int(fieldname, label, width=100):
	return {"fieldname": fieldname, "label": label, "fieldtype": "Int", "width": width}


def _sales_columns():
	return [
		_int("orders", _("F&F Orders")),
		_int("returns", _("Returns")),
		_money("gross", _("Gross (before F&F)")),
		_money("discount", _("F&F Discount")),
		_money("net", _("Net Sales")),
		{"fieldname": "discount_pct", "label": _("Effective Discount %"), "fieldtype": "Percent", "width": 110},
	]


def _sales_values(bucket) -> dict:
	return {
		"orders": len(bucket["orders"]),
		"returns": len(bucket["returns"]),
		"customers": len(bucket["customers"]),
		"qty": bucket["qty"],
		"gross": bucket["gross"],
		"discount": bucket["discount"],
		"net": bucket["net"],
		"discount_pct": (bucket["discount"] / bucket["gross"] * 100.0) if bucket["gross"] else 0.0,
	}


def _period_key(posting_date, periodicity) -> str:
	d = getdate(posting_date)
	if periodicity == "Daily":
		return d.isoformat()
	if periodicity == "Weekly":
		return add_days(d, -d.weekday()).isoformat()
	return d.strftime("%Y-%m")


def _summary_view(filters, rows, decisions):
	periodicity = filters.get("periodicity") or "Monthly"
	buckets = defaultdict(_blank)
	for row in rows:
		_add(buckets[_period_key(row.posting_date, periodicity)], row)
	periods = set(buckets)
	for inv in decisions["invoices"].values():
		if inv["posting_date"]:
			periods.add(_period_key(inv["posting_date"], periodicity))

	data = []
	for period in sorted(periods):
		values = _sales_values(buckets[period])
		counts = _decision_counts(
			decisions,
			predicate=lambda _k, inv, p=period: inv["posting_date"]
			and _period_key(inv["posting_date"], periodicity) == p,
		)
		data.append(
			{
				"period": period,
				**values,
				"avg_discount": values["discount"] / values["orders"] if values["orders"] else 0.0,
				"cap_hits": counts["cap_hits"],
				"fnf_won_coupon": counts["won_Coupon"],
				"coupon_won": counts["lost_Coupon"],
				"fnf_won_promotion": counts["won_Promotion"],
				"promotion_won": counts["lost_Promotion"],
			}
		)
	columns = [
		{"fieldname": "period", "label": _("Period"), "fieldtype": "Data", "width": 110},
		*_sales_columns(),
		_int("customers", _("Customers")),
		_money("avg_discount", _("Avg Discount / Order")),
		_int("cap_hits", _("Cap Hits (orders)")),
		_int("fnf_won_coupon", _("F&F Won vs Coupon")),
		_int("coupon_won", _("Coupon Won vs F&F")),
		_int("fnf_won_promotion", _("F&F Won vs Promotion")),
		_int("promotion_won", _("Promotion Won vs F&F")),
	]
	chart = {
		"data": {
			"labels": [d["period"] for d in data],
			"datasets": [
				{"name": _("F&F Discount"), "values": [flt(d["discount"], 2) for d in data]},
				{"name": _("Net Sales"), "values": [flt(d["net"], 2) for d in data]},
			],
		},
		"type": "bar",
		"fieldtype": "Currency",
	}
	return columns, data, chart


def _invoices_view(filters, rows, decisions):
	buckets = {}
	for row in rows:
		key = (row.doctype, row.invoice)
		if key not in buckets:
			buckets[key] = {"row": row, "bucket": _blank(), "lines": 0}
		_add(buckets[key]["bucket"], row)
		buckets[key]["lines"] += 1
	data = []
	for key, entry in buckets.items():
		row, values = entry["row"], _sales_values(entry["bucket"])
		inv = decisions["invoices"].get(key) or {}
		data.append(
			{
				"invoice_type": row.doctype,
				"invoice": row.invoice,
				"posting_date": row.posting_date,
				"customer": row.customer,
				"pos_profile": row.pos_profile,
				"is_return": cint(row.is_return),
				"return_against": row.return_against,
				"lines": entry["lines"],
				"qty": values["qty"],
				"gross": values["gross"],
				"discount": values["discount"],
				"net": values["net"],
				"discount_pct": values["discount_pct"],
				"cap_hit": ", ".join(sorted(inv.get("cap_hit") or [])),
			}
		)
	columns = [
		{"fieldname": "invoice_type", "label": _("Type"), "fieldtype": "Link", "options": "DocType", "width": 110},
		{"fieldname": "invoice", "label": _("Invoice"), "fieldtype": "Dynamic Link", "options": "invoice_type", "width": 170},
		{"fieldname": "posting_date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		{"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link", "options": "Customer", "width": 160},
		{"fieldname": "pos_profile", "label": _("POS Profile"), "fieldtype": "Link", "options": "POS Profile", "width": 120},
		{"fieldname": "is_return", "label": _("Return"), "fieldtype": "Check", "width": 70},
		{"fieldname": "return_against", "label": _("Return Against"), "fieldtype": "Data", "width": 150},
		_int("lines", _("F&F Lines"), 80),
		{"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 80},
		_money("gross", _("Gross (before F&F)")),
		_money("discount", _("F&F Discount")),
		_money("net", _("Net Sales")),
		{"fieldname": "discount_pct", "label": _("Effective Discount %"), "fieldtype": "Percent", "width": 110},
		{"fieldname": "cap_hit", "label": _("Cap Hit"), "fieldtype": "Data", "width": 90},
	]
	return columns, data, None


def _items_view(filters, rows, decisions):
	buckets = defaultdict(_blank)
	meta = {}
	for row in rows:
		_add(buckets[row.item_code], row)
		meta.setdefault(row.item_code, row)
	data = []
	for item_code, bucket in buckets.items():
		row = meta[item_code]
		data.append(
			{
				"item_code": item_code,
				"item_name": row.item_name,
				"item_group": row.item_group,
				"brand": row.brand,
				**_sales_values(bucket),
			}
		)
	data.sort(key=lambda d: d["discount"], reverse=True)
	limit = cint(filters.get("top_n"))
	if limit > 0:
		data = data[:limit]
	columns = [
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 150},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 170},
		{"fieldname": "item_group", "label": _("Item Group"), "fieldtype": "Link", "options": "Item Group", "width": 120},
		{"fieldname": "brand", "label": _("Brand"), "fieldtype": "Link", "options": "Brand", "width": 100},
		{"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 80},
		*_sales_columns(),
		_int("customers", _("Customers")),
	]
	top = data[:10]
	chart = {
		"data": {
			"labels": [d["item_code"] for d in top],
			"datasets": [{"name": _("F&F Discount"), "values": [flt(d["discount"], 2) for d in top]}],
		},
		"type": "bar",
		"fieldtype": "Currency",
	}
	return columns, data, chart


def _customers_view(filters, rows, decisions):
	buckets = defaultdict(_blank)
	names = {}
	for row in rows:
		_add(buckets[row.customer], row)
		names.setdefault(row.customer, row.customer_name)
	cap_by_customer = defaultdict(lambda: {"orders": 0, "types": set()})
	for inv in decisions["invoices"].values():
		if inv["cap_hit"]:
			cap_by_customer[inv["customer"]]["orders"] += 1
			cap_by_customer[inv["customer"]]["types"] |= inv["cap_hit"]
	data = []
	for customer in set(buckets) | set(cap_by_customer):
		values = _sales_values(buckets[customer])
		caps = cap_by_customer.get(customer) or {"orders": 0, "types": set()}
		data.append(
			{
				"customer": customer,
				"customer_name": names.get(customer),
				**values,
				"avg_discount": values["discount"] / values["orders"] if values["orders"] else 0.0,
				"cap_hits": caps["orders"],
				"cap_types": ", ".join(sorted(caps["types"])),
			}
		)
	if cint(filters.get("only_cap_hits")):
		data = [d for d in data if d["cap_hits"]]
	data.sort(key=lambda d: (d["cap_hits"], d["discount"]), reverse=True)
	columns = [
		{"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link", "options": "Customer", "width": 160},
		{"fieldname": "customer_name", "label": _("Customer Name"), "fieldtype": "Data", "width": 170},
		*_sales_columns(),
		_money("avg_discount", _("Avg Discount / Order")),
		_int("cap_hits", _("Cap Hits (orders)")),
		{"fieldname": "cap_types", "label": _("Caps Hit"), "fieldtype": "Data", "width": 110},
	]
	return columns, data, None


def _comparisons_view(filters, rows, decisions):
	counts = _decision_counts(decisions)
	data = []
	for label in COMPETITORS:
		won, lost = counts[f"won_{label}"], counts[f"lost_{label}"]
		line_won, line_lost = decisions["lines"][label]
		data.append(
			{
				"competitor": _(label),
				"fnf_won": won,
				"competitor_won": lost,
				"fnf_win_rate": (won / (won + lost) * 100.0) if won + lost else 0.0,
				"fnf_won_lines": line_won,
				"competitor_won_lines": line_lost,
			}
		)
	columns = [
		{"fieldname": "competitor", "label": _("Compared With"), "fieldtype": "Data", "width": 150},
		_int("fnf_won", _("F&F Won (orders)"), 130),
		_int("competitor_won", _("Competitor Won (orders)"), 160),
		{"fieldname": "fnf_win_rate", "label": _("F&F Win Rate %"), "fieldtype": "Percent", "width": 120},
		_int("fnf_won_lines", _("F&F Won (lines)"), 120),
		_int("competitor_won_lines", _("Competitor Won (lines)"), 160),
	]
	chart = {
		"data": {
			"labels": [d["competitor"] for d in data],
			"datasets": [
				{"name": _("F&F Won"), "values": [d["fnf_won"] for d in data]},
				{"name": _("Competitor Won"), "values": [d["competitor_won"] for d in data]},
			],
		},
		"type": "bar",
	}
	return columns, data, chart


def get_report_summary(rows, decisions) -> list[dict]:
	bucket = _blank()
	for row in rows:
		_add(bucket, row)
	values = _sales_values(bucket)
	counts = _decision_counts(decisions)
	currency = frappe.db.get_default("currency")
	return [
		{"label": _("F&F Orders"), "value": values["orders"], "datatype": "Int", "indicator": "Blue"},
		{"label": _("Returns"), "value": values["returns"], "datatype": "Int", "indicator": "Orange"},
		{"label": _("Customers"), "value": values["customers"], "datatype": "Int", "indicator": "Blue"},
		{"label": _("F&F Discount (net)"), "value": values["discount"], "datatype": "Currency", "currency": currency, "indicator": "Red"},
		{"label": _("Net Sales"), "value": values["net"], "datatype": "Currency", "currency": currency, "indicator": "Green"},
		{
			"label": _("Avg Discount / Order"),
			"value": values["discount"] / values["orders"] if values["orders"] else 0.0,
			"datatype": "Currency",
			"currency": currency,
		},
		{"label": _("Customers Who Hit Caps"), "value": len(counts["cap_customers"]), "datatype": "Int", "indicator": "Red"},
		{"label": _("F&F Won vs Coupon"), "value": counts["won_Coupon"], "datatype": "Int", "indicator": "Green"},
		{"label": _("Coupon Won vs F&F"), "value": counts["lost_Coupon"], "datatype": "Int", "indicator": "Orange"},
	]
