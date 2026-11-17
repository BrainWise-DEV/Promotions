# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family usage ledger.

One **Friends and Family Usage** row per invoice line that received F&F,
written on submit and flagged ``is_cancelled`` on cancel: the audit trail.
One **Friends and Family Usage Total** row per (customer, company, period): the
running sum of those rows' ``base_amount`` (that company's currency) that are
not cancelled, and the row submits lock. Usage is shared across programs.
Caps compare per company: companies may keep different currencies, so their
amounts are never added together.

Concurrency
-----------
Validate prices the invoice without a lock: fast, and possibly stale. Submit
is the authority. ``on_submit`` upserts the counter row and reads it ``FOR
UPDATE`` by its primary key, which takes a record lock only, never a gap lock,
so submits for different customers never wait on or deadlock with each other.
It then checks the caps, inserts its ledger rows and adds them to the counter,
all in the invoice's transaction. Two submits for one customer and period run
one after the other. A locking read sees the latest committed value; a plain
read would return the snapshot this transaction took during validate (MariaDB
REPEATABLE READ).

Amounts come from the persisted row after ERPNext recalculated it:
``(price_list_rate - rate) x qty`` on lines tagged F&F. Nothing is taken from
a client-sent discount field.

Returns credit the period of their original invoice. Consolidated Sales
Invoices are skipped (their POS Invoices were recorded), and so are Sales
Invoices that are not POS (``is_pos = 0``).
"""

from __future__ import annotations

import calendar
import datetime
import hashlib

import frappe
from frappe import _
from frappe.query_builder.functions import Coalesce, IfNull, Sum
from frappe.utils import cint, cstr, flt, fmt_money, getdate, now_datetime
from pypika.terms import ExistsCriterion, Values

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions import ff_config
from posnext_promotions.promotions import ff_constants as const
from posnext_promotions.promotions.friends_and_family_caps import (
	CAP_MONTHLY,
	CAP_ORDER,
	FIELD_DISCOUNT_SOURCE,
	INVOICE_DOCTYPES,
)

LEDGER_DOCTYPE = "Friends and Family Usage"
TOTAL_DOCTYPE = "Friends and Family Usage Total"
UNIQUE_KEY = "ff_usage_line"
UNIQUE_KEY_FIELDS = ("invoice_doctype", "invoice", "invoice_item")
PERIOD_INDEX = "customer_period_key_index"
PERIOD_ONLY_INDEX = "period_key_index"
TOTAL_UNIQUE_KEY = "ff_usage_total_key"
TOTAL_UNIQUE_KEY_FIELDS = ("customer", "company", "period_key")
FNF_SOURCE = "friends_and_family"
REASON_BREACH = "cap_breach_at_submit"

def _ledger():
	return frappe.qb.DocType(LEDGER_DOCTYPE)


def _totals():
	return frappe.qb.DocType(TOTAL_DOCTYPE)


# ---- schema -----------------------------------------------------------------


def ensure_ledger_unique_key() -> None:
	"""Add the ledger's ``ff_usage_line`` unique key, (customer, period_key) and period_key indexes, and the
	counter's (customer, period_key) unique key, when missing. Idempotent.

	Checks this site's schema with ``SHOW INDEX``. ``frappe.db.add_unique`` checks
	``information_schema`` by table name only, so another site's database on the
	same server could make it skip. DDL commits the open transaction.
	"""
	# Raw SQL here only: frappe.qb cannot express DDL or SHOW INDEX.
	if frappe.db.table_exists(LEDGER_DOCTYPE, cached=False):
		table = f"tab{LEDGER_DOCTYPE}"
		if not frappe.db.has_index(table, UNIQUE_KEY):
			cols = ", ".join(f"`{f}`" for f in UNIQUE_KEY_FIELDS)
			frappe.db.sql_ddl(f"alter table `{table}` add unique index `{UNIQUE_KEY}` ({cols})")
		if not frappe.db.has_index(table, PERIOD_INDEX):
			frappe.db.sql_ddl(f"alter table `{table}` add index `{PERIOD_INDEX}` (`customer`, `period_key`)")
		if not frappe.db.has_index(table, PERIOD_ONLY_INDEX):
			# The rebuild's locking SUM per period locks only that period's rows.
			frappe.db.sql_ddl(f"alter table `{table}` add index `{PERIOD_ONLY_INDEX}` (`period_key`)")
	if frappe.db.table_exists(TOTAL_DOCTYPE, cached=False):
		table = f"tab{TOTAL_DOCTYPE}"
		# SHOW INDEX lists a key's columns in order; Column_name is the 5th field.
		cols = [r[4] for r in frappe.db.sql(f"show index from `{table}` where Key_name = %s", TOTAL_UNIQUE_KEY)]
		if cols and list(cols) != list(TOTAL_UNIQUE_KEY_FIELDS):
			# Pre-company key from an earlier dev build: replace it.
			frappe.db.sql_ddl(f"alter table `{table}` drop index `{TOTAL_UNIQUE_KEY}`")
			cols = []
		if not cols:
			fields = ", ".join(f"`{f}`" for f in TOTAL_UNIQUE_KEY_FIELDS)
			frappe.db.sql_ddl(f"alter table `{table}` add unique index `{TOTAL_UNIQUE_KEY}` ({fields})")


# ---- periods ----------------------------------------------------------------


def period_key(on_date) -> str:
	"""Cap period of a date. Month only in Phase 1: ``YYYY-MM``."""
	return getdate(on_date).strftime("%Y-%m")


def period_bounds(key: str) -> tuple[datetime.date, datetime.date]:
	year, month = (int(p) for p in cstr(key).split("-"))
	return datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1])


# ---- usage ------------------------------------------------------------------


def total_name(customer: str, company: str, period_key: str) -> str:
	"""Deterministic primary key of the counter row, so locking it is an exact record lock."""
	digest = hashlib.sha1(f"{customer}\n{company}\n{period_key}".encode()).hexdigest()
	return f"{period_key}-{digest}"


def _upsert_total(name: str, customer: str, company: str, period_key: str, base_amount: float, overwrite: bool):
	"""Insert the counter row, or on its primary key either leave it (lock only) or set its value.

	``ON DUPLICATE KEY UPDATE`` on the primary key takes an exclusive record lock on
	an existing row, so two submits never share an S lock and then race to upgrade it.
	"""
	T = _totals()
	now = now_datetime()
	user = frappe.session.user
	query = (
		frappe.qb.into(T)
		.columns(
			T.name, T.creation, T.modified, T.owner, T.modified_by, T.docstatus,
			T.customer, T.company, T.period_key, T.base_amount,
		)
		.insert(name, now, now, user, user, 0, customer, company, period_key, flt(base_amount))
	)
	if overwrite:
		query = query.on_duplicate_key_update(T.base_amount, Values(T.base_amount)).on_duplicate_key_update(
			T.modified, Values(T.modified)
		)
	else:
		query = query.on_duplicate_key_update(T.name, T.name)
	query.run()


def lock_usage(customer: str, company: str, period_key: str) -> float:
	"""Lock the customer's counter for ``company`` and ``period_key``; return its value (company currency).

	Upserts the row and reads it ``FOR UPDATE`` by primary key: record locks only,
	no gap locks. Held until the transaction ends.

	No retry: a lock-wait timeout may roll back more than the statement
	(``innodb_rollback_on_timeout``) and the invoice already holds its GL / stock
	locks, so the submit fails fast and the cashier retries. A deadlock
	propagates as is.
	"""
	if not company:
		frappe.throw(_("Friends & Family usage needs the invoice's company."))
	name = total_name(customer, company, period_key)
	T = _totals()
	try:
		_upsert_total(name, customer, company, period_key, 0.0, overwrite=False)
		value = frappe.qb.from_(T).select(T.base_amount).where(T.name == name).for_update().run()[0][0]
	except frappe.QueryTimeoutError:
		frappe.throw(
			_("Friends & Family usage is busy for customer {0}, please retry.").format(customer),
			title=_("Please retry"),
		)
	return flt(value)


def _add_to_total(customer: str, company: str, period_key: str, delta: float) -> None:
	"""Add ``delta`` to a counter this transaction already holds (see :func:`lock_usage`).

	Stores the raw sum, which can be negative (a return of an invoice the ledger
	never recorded), so the counter always equals the ledger sum whatever the
	order of events. Reads clamp at 0 (:func:`get_usage`, :func:`on_submit`).
	"""
	if not delta:
		return
	T = _totals()
	(
		frappe.qb.update(T)
		.set(T.base_amount, T.base_amount + flt(delta))
		.set(T.modified, now_datetime())
		.where(T.name == total_name(customer, company, period_key))
	).run()


def _recorded_for_invoice(doctype: str, invoice: str) -> float:
	"""This invoice's own counted rows (plain read: only this invoice's submit writes them)."""
	if not invoice:
		return 0.0
	L = _ledger()
	total = (
		frappe.qb.from_(L)
		.select(Sum(L.base_amount))
		.where((L.invoice_doctype == doctype) & (L.invoice == invoice) & (L.is_cancelled == 0))
	).run()[0][0]
	return flt(total)


def get_usage(customer, company, period_key, exclude_invoice: str | None = None) -> float:
	"""F&F already granted to ``customer`` by ``company`` in ``period_key`` (its currency), net of returns.

	A plain read of the counter (validate: fast, may be stale; submit re-checks
	under :func:`lock_usage`). ``exclude_invoice`` drops that invoice's own rows.
	"""
	customer = cstr(customer).strip()
	if not customer or not period_key:
		return 0.0
	if not company:
		frappe.throw(_("Friends & Family usage needs the invoice's company."))
	used = flt(frappe.db.get_value(TOTAL_DOCTYPE, total_name(customer, company, period_key), "base_amount"))
	if exclude_invoice:
		L = _ledger()
		used -= flt(
			(
				frappe.qb.from_(L)
				.select(Sum(L.base_amount))
				.where(
					(L.invoice == exclude_invoice)
					& (L.customer == customer)
					& (L.company == company)
					& (L.period_key == period_key)
					& (L.is_cancelled == 0)
				)
			).run()[0][0]
		)
	return max(0.0, used)  # the counter may be negative (see _add_to_total); usage never is


# ---- rows -------------------------------------------------------------------


def is_tracked(doc) -> bool:
	"""POS Invoices, and Sales Invoices with ``is_pos``. Consolidated invoices never."""
	if cint(doc.get("is_consolidated")):
		return False
	doctype = cstr(doc.get("doctype"))
	if doctype == "POS Invoice":
		return True
	return doctype == "Sales Invoice" and bool(cint(doc.get("is_pos")))


def line_fnf_amount(row) -> float:
	"""Saving the row gives, from its persisted prices (negative on a return row)."""
	if cstr(row.get(FIELD_DISCOUNT_SOURCE)) != FNF_SOURCE:
		return 0.0
	price_list_rate = flt(row.get("price_list_rate"))
	rate = flt(row.get("rate"))
	if price_list_rate <= rate:
		return 0.0
	return (price_list_rate - rate) * flt(row.get("qty"))


def _ledger_posting_date(doc):
	"""Posting date that picks the period: a return uses its original invoice's."""
	if cint(doc.get("is_return")) and doc.get("return_against"):
		original = frappe.db.get_value(doc.get("doctype"), doc.get("return_against"), "posting_date")
		if original:
			return original
	return doc.get("posting_date")


def _precision(fieldname: str, currency: str | None) -> int:
	from frappe.model.meta import get_field_precision

	return get_field_precision(frappe.get_meta(LEDGER_DOCTYPE).get_field(fieldname), currency=currency)


def _company_currency(company) -> str | None:
	return frappe.get_cached_value("Company", company, "default_currency") if company else None


def ledger_rows(doc) -> list[dict]:
	"""Ledger values for each F&F line of ``doc`` (empty when the invoice is not tracked)."""
	if not is_tracked(doc) or not doc.get("customer"):
		return []
	company = doc.get("company")
	company_currency = _company_currency(company)
	currency = doc.get("currency") or company_currency
	conversion_rate = flt(doc.get("conversion_rate")) or 1.0
	amount_precision = _precision("amount", currency)
	base_precision = _precision("base_amount", company_currency)
	period = period_key(_ledger_posting_date(doc))
	rows = []
	for row in doc.get("items") or []:
		amount = flt(line_fnf_amount(row), amount_precision)
		if not amount or not row.get("name"):
			continue
		rows.append(
			{
				"customer": doc.get("customer"),
				"company": company,
				# "program" stays blank in Phase 1 (one program); Phase 3 fills it.
				"invoice_doctype": doc.get("doctype"),
				"invoice": doc.get("name"),
				"invoice_item": row.get("name"),
				"posting_date": getdate(doc.get("posting_date")),
				"period_key": period,
				"currency": currency,
				"amount": amount,
				"base_amount": flt(amount * conversion_rate, base_precision),
				"is_return": cint(doc.get("is_return")),
				"is_cancelled": 0,
			}
		)
	return rows


def insert_row(values: dict) -> bool:
	"""Insert one ledger row. ``False`` when the line is already recorded (unique key)."""
	now = now_datetime()
	user = frappe.session.user
	record = {
		"name": frappe.generate_hash(length=12),
		"creation": now,
		"modified": now,
		"owner": user,
		"modified_by": user,
		"docstatus": 0,
		**values,
	}
	L = _ledger()
	try:
		frappe.qb.into(L).columns(*(L[c] for c in record)).insert(*record.values()).run()
	except Exception as e:
		if frappe.db.is_duplicate_entry(e) and not frappe.db.is_primary_key_violation(e):
			return False
		raise
	return True


def record_rows(rows: list[dict]) -> list[dict]:
	"""Insert ledger rows and add the counted ones to their counters. Returns the rows inserted."""
	inserted = [values for values in rows if insert_row(values)]
	deltas: dict[tuple, float] = {}
	for values in inserted:
		if not cint(values.get("is_cancelled")):
			key = (values["customer"], values["company"], values["period_key"])
			deltas[key] = deltas.get(key, 0.0) + flt(values["base_amount"])
	for (customer, company, period), delta in sorted(deltas.items()):
		lock_usage(customer, company, period)
		_add_to_total(customer, company, period, delta)
	return inserted


# ---- caps at submit ---------------------------------------------------------


def _money(value, currency) -> str:
	return fmt_money(value, currency=currency)


def _over(value: float, cap: float, unit: float) -> bool:
	"""``value`` exceeds ``cap`` by more than one precision unit (per-line rounding)."""
	return flt(value) > flt(cap) + unit


def _check_caps(doc, rows, used: float) -> dict | None:
	"""The cap this invoice breaks at submit, or ``None``. ``used`` = locked counter, this invoice excluded."""
	from posnext_promotions.promotions.friends_and_family import get_active_program

	program = get_active_program(doc.get("posting_date"))
	if program is None or not program.has_caps:
		return None
	total = bd.money(sum(flt(r["base_amount"]) for r in rows))
	if total <= 0:
		return None
	currency = _company_currency(doc.get("company"))
	unit = 10 ** -_precision("base_amount", currency)
	per_order = flt(program.max_discount_per_order)
	if per_order > 0 and _over(total, per_order, unit):
		return {
			"cap_hit": CAP_ORDER,
			"cap": per_order,
			"used": 0.0,
			"invoice_total": total,
			"currency": currency,
			"message": _("Friends & Family discount {0} on this invoice exceeds the per-order cap of {1}.").format(
				_money(total, currency), _money(per_order, currency)
			),
		}
	monthly = flt(program.max_discount_per_customer_month)
	if monthly <= 0 or not _over(used + total, monthly, unit):
		return None
	period = rows[0]["period_key"]
	return {
		"cap_hit": CAP_MONTHLY,
		"cap": monthly,
		"used": bd.money(used),
		"invoice_total": total,
		"period_key": period,
		"currency": currency,
		"message": _(
			"Friends & Family discount {0} on this invoice exceeds the customer's remaining monthly allowance: "
			"{1} of the {2} cap already used in {3}."
		).format(_money(total, currency), _money(used, currency), _money(monthly, currency), period),
	}


def _log_breach(doc, breach: dict) -> None:
	from posnext_promotions.promotions.friends_and_family_log import LOG_CAP_HIT, write_log

	write_log(
		LOG_CAP_HIT,
		customer=doc.get("customer"),
		reference_doctype=doc.get("doctype"),
		reference_name=doc.get("name"),
		posting_date=doc.get("posting_date"),
		pos_profile=doc.get("pos_profile"),
		company=doc.get("company"),
		discount_source=FNF_SOURCE,
		discount_amount=breach["invoice_total"],
		capped=1,
		cap_hit=breach["cap_hit"],
		reason=REASON_BREACH,
		message=breach["message"],
		details=breach,
	)


# ---- hooks ------------------------------------------------------------------


def on_submit(doc, method=None):
	"""Sales Invoice / POS Invoice ``on_submit``: lock the counter, check the caps, record usage."""
	rows = ledger_rows(doc)
	if not rows:
		return
	customer, company, period = rows[0]["customer"], rows[0]["company"], rows[0]["period_key"]
	used = lock_usage(customer, company, period) - _recorded_for_invoice(doc.get("doctype"), doc.get("name"))
	breach = None if cint(doc.get("is_return")) else _check_caps(doc, rows, max(0.0, used))  # clamp on read only
	block = const.CAP_BREACH_POLICIES[1]
	if breach and ff_config.resolve().on_cap_breach_at_submit.value == block:
		frappe.throw(breach["message"], title=_("Friends & Family cap exceeded"))
	record_rows(rows)
	if breach:
		_log_breach(doc, breach)


def on_cancel(doc, method=None):
	"""Sales Invoice / POS Invoice ``on_cancel``: the invoice's rows stop counting.

	Each row is flipped by primary key with ``is_cancelled = 0`` in the WHERE, and
	only rows whose flip happened (rowcount) are subtracted, so a second call
	subtracts nothing.
	"""
	if not doc.get("name"):
		return
	L = _ledger()
	rows = (
		frappe.qb.from_(L)
		.select(L.name, L.customer, L.company, L.period_key, L.base_amount)
		.where((L.invoice_doctype == doc.get("doctype")) & (L.invoice == doc.get("name")) & (L.is_cancelled == 0))
	).run(as_dict=True)
	if not rows:
		return
	# Same lock order as submit: counters first, then ledger rows.
	for key in sorted({(row.customer, row.company, row.period_key) for row in rows}):
		lock_usage(*key)
	deltas: dict[tuple, float] = {}
	now = now_datetime()
	for row in rows:
		(
			frappe.qb.update(L)
			.set(L.is_cancelled, 1)
			.set(L.modified, now)
			.where((L.name == row.name) & (L.is_cancelled == 0))
		).run()
		if frappe.db._cursor.rowcount:
			key = (row.customer, row.company, row.period_key)
			deltas[key] = deltas.get(key, 0.0) - flt(row.base_amount)
	for (customer, company, period), delta in sorted(deltas.items()):
		_add_to_total(customer, company, period, delta)


# ---- backfill ---------------------------------------------------------------


def _backfill_candidates(doctype: str, start, end, after: str, limit: int) -> list[str]:
	Inv = frappe.qb.DocType(doctype)
	Orig = frappe.qb.DocType(doctype).as_("o")
	Line = frappe.qb.DocType(f"{doctype} Item")
	has_fnf_line = ExistsCriterion(
		frappe.qb.from_(Line)
		.select(Line.name)
		.where((Line.parent == Inv.name) & (Line.parenttype == doctype) & (Line[FIELD_DISCOUNT_SOURCE] == FNF_SOURCE))
	)
	in_period = ((IfNull(Inv.is_return, 0) == 0) & Inv.posting_date[start:end]) | (
		(Inv.is_return == 1) & Coalesce(Orig.posting_date, Inv.posting_date)[start:end]
	)
	query = (
		frappe.qb.from_(Inv)
		.left_join(Orig)
		.on(Orig.name == Inv.return_against)
		.select(Inv.name)
		.where((Inv.docstatus == 1) & (Inv.name > after) & in_period & has_fnf_line)
		.orderby(Inv.name)
		.limit(limit)
	)
	if doctype != "POS Invoice":
		query = query.where(IfNull(Inv.is_pos, 0) == 1)
	if frappe.db.has_column(doctype, "is_consolidated"):
		query = query.where(IfNull(Inv.is_consolidated, 0) == 0)
	return query.run(pluck=True)


def _row_exists(values: dict) -> bool:
	return bool(frappe.db.exists(LEDGER_DOCTYPE, {f: values[f] for f in UNIQUE_KEY_FIELDS}))


def rebuild_totals(period_key: str) -> None:
	"""Recompute every counter of ``period_key`` from the ledger (idempotent: sets, never adds).

	Safe while tills submit: the period's counters are locked first (``FOR UPDATE``
	on the primary-key prefix ``"<period>-"`` of :func:`total_name`, the same lock
	order as submit), then the ledger is summed with a locking read, which sees
	committed rows rather than this transaction's snapshot. A submit either
	finishes before the rebuild takes its counter, or waits until it commits.
	"""
	T = _totals()
	L = _ledger()
	existing = (
		frappe.qb.from_(T).select(T.name).where(T.name.like(f"{period_key}-%")).for_update()
	).run(pluck=True)
	sums = (
		frappe.qb.from_(L)
		.select(L.customer, L.company, Sum(L.base_amount))
		.where((L.period_key == period_key) & (L.is_cancelled == 0))
		.groupby(L.customer, L.company)
		.for_update()
	).run()
	written = set()
	for customer, company, total in sums:
		name = total_name(customer, company, period_key)
		_upsert_total(name, customer, company, period_key, flt(total), overwrite=True)
		written.add(name)
	stale = [name for name in existing if name not in written]
	if stale:
		(
			frappe.qb.update(T).set(T.base_amount, 0).set(T.modified, now_datetime()).where(T.name.isin(stale))
		).run()


def backfill(period_key: str, batch_size: int = 500, dry_run: bool = False) -> dict:
	"""Record submitted F&F invoices of ``period_key`` that have no ledger rows yet, then
	rebuild that period's counters from the ledger.

	Pages by invoice name and commits after each batch. Idempotent: the unique
	key (ensured first) skips lines already recorded, and counters are
	recomputed, not added to. ``dry_run`` writes nothing and returns what it
	would write. Returns ``{"invoices": n, "rows": m}``.
	"""
	if not dry_run:
		ensure_ledger_unique_key()
	start, end = period_bounds(period_key)
	batch_size = max(1, cint(batch_size))
	counts = {"invoices": 0, "rows": 0}
	for doctype in INVOICE_DOCTYPES:
		if not frappe.db.table_exists(doctype) or not frappe.db.has_column(f"{doctype} Item", FIELD_DISCOUNT_SOURCE):
			continue
		after = ""
		while True:
			names = _backfill_candidates(doctype, start, end, after, batch_size)
			if not names:
				break
			for name in names:
				rows = ledger_rows(frappe.get_doc(doctype, name))
				if dry_run:
					written = sum(1 for values in rows if not _row_exists(values))
				else:
					written = sum(1 for values in rows if insert_row(values))
				if written:
					counts["invoices"] += 1
					counts["rows"] += written
			after = names[-1]
			if not dry_run:
				frappe.db.commit()
	if not dry_run:
		# Batch job: commit so the rebuild starts a fresh transaction (its reads lock anyway).
		frappe.db.commit()
		rebuild_totals(period_key)
		frappe.db.commit()
	return counts
