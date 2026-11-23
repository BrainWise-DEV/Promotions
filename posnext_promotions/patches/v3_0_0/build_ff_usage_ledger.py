"""Build the Friends and Family Usage ledger for the current month.

Adds the ``ff_usage_line`` unique key first: post_model_sync patches run before
``after_migrate``, and the backfill relies on the key to stay idempotent.
Earlier months are not needed: caps only read the current period.
"""

from frappe.utils import nowdate

from posnext_promotions.promotions import ff_ledger


def execute():
	ff_ledger.ensure_ledger_unique_key()
	counts = ff_ledger.backfill(ff_ledger.period_key(nowdate()))
	print(f"Friends and Family Usage backfill: {counts['invoices']} invoices, {counts['rows']} rows")
