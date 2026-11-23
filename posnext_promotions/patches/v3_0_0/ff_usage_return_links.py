"""Fill ``original_row`` / ``qty`` on Friends and Family Usage return rows (Ruling 38).

Returns now read earlier returns of the same original row from the ledger.
"""

from posnext_promotions.promotions import ff_ledger


def execute():
	count = ff_ledger.backfill_return_links()
	print(f"Friends and Family Usage: {count} return rows linked to their original row")
