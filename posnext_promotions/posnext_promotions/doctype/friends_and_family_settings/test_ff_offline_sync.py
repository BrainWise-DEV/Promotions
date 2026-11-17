# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Task 6c — an offline-synced invoice is never re-priced and never throws.

The customer has left: every disagreement with the entitlement becomes one
``Offline Disagreement`` log row at submit; the usage ledger records the F&F the
lines carry.
"""

from __future__ import annotations

from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_paid_freeze import (
	PaidInvoiceFixture,
)
from posnext_promotions.promotions import ff_ledger, ff_paid
from posnext_promotions.promotions.friends_and_family_caps import FIELD_DISCOUNT_SOURCE, FIELD_FNF_AMOUNT
from posnext_promotions.promotions.friends_and_family_apply import apply_friends_and_family_on_invoice
from posnext_promotions.promotions.friends_and_family_log import (
	LOG_OFFLINE_DISAGREEMENT,
	LOG_PAID_DISAGREEMENT,
	log_invoice_submit,
)
from posnext_promotions.test_promotions import ITEM_A, ITEM_B


class TestFFOfflineSync(PaidInvoiceFixture, FrappeTestCase):
	def _sync(self, doc):
		"""Validate + submit hooks, as the offline sync runs them."""
		apply_friends_and_family_on_invoice(doc)
		ff_ledger.on_submit(doc)
		log_invoice_submit(doc)

	def _reasons(self, doc):
		return [log.reason for log in self._logs(doc, LOG_OFFLINE_DISAGREEMENT)]

	def test_offline_invoice_without_fnf_not_repriced(self):
		doc = self._invoice([self._row()], paid=False, offline=True)
		before = self._prices(doc)
		self._sync(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(self._reasons(doc), [ff_paid.MISSING_FNF])
		self.assertEqual(self._logs(doc, LOG_PAID_DISAGREEMENT), [])
		self.assertEqual(self._ledger_amount(doc), 0)

	def test_offline_invoice_with_fnf_over_cap_kept(self):
		self._cap_with_remaining(5)  # cap nearly used
		doc = self._invoice([self._row(qty=2, pct=30, marked=True)], paid=False, offline=True)
		before = self._prices(doc)
		self._sync(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(self._reasons(doc), [ff_paid.CAP_EXCEEDED])
		self.assertEqual(self._ledger_amount(doc), 30)

	def test_offline_forbidden_gift_logged_not_removed(self):
		self._settings(allow_free_gift_with_ff=0)
		rule, doc = self._gift_invoice(paid=False, offline=True)
		before = self._prices(doc)
		self._sync(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(len(doc.get("items")), 2)
		self.assertIn(ff_paid.GIFT_WITH_FNF, self._reasons(doc))
		self.assertEqual(self._logs(doc, LOG_PAID_DISAGREEMENT), [])

	def test_offline_cap_breach_never_blocks(self):
		self._cap_with_remaining(5, on_cap_breach_at_submit="Block submit")
		doc = self._invoice([self._row(qty=2, pct=30, marked=True)], paid=False, offline=True)
		self._sync(doc)  # no throw: the sale already happened
		self.assertEqual(self._reasons(doc), [ff_paid.CAP_EXCEEDED])
		self.assertEqual(self._ledger_amount(doc), 30)

	def test_offline_over_discount_logged_not_thrown(self):
		doc = self._invoice([self._row(pct=40, marked=True)], paid=False, offline=True)
		before = self._prices(doc)
		self._sync(doc)
		self.assertEqual(self._prices(doc), before)
		logs = self._logs(doc, LOG_OFFLINE_DISAGREEMENT)
		self.assertEqual([(log.reason, log.item_code) for log in logs], [(ff_paid.OVER_DISCOUNT, ITEM_A)])
		self.assertEqual(flt(logs[0].discount_amount), 20)
		self.assertEqual(flt(logs[0].fnf_uncapped_amount), 15)
		self.assertEqual(self._ledger_amount(doc), 20)

	def test_offline_program_inactive_logged(self):
		self._settings(enabled=0)
		doc = self._invoice([self._row(pct=30, marked=True)], paid=False, offline=True)
		before = self._prices(doc)
		self._sync(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(self._reasons(doc), [ff_paid.PROGRAM_INACTIVE])

	def test_offline_compatibility_logged_not_stripped(self):
		rule = self._bxgy()
		doc = self._invoice([self._row(qty=2, pct=30, marked=True, rules=rule)], paid=False, offline=True)
		before = self._prices(doc)
		self._sync(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(self._reasons(doc), [ff_paid.COMPATIBILITY])

	def test_offline_header_discount_logged(self):
		doc = self._invoice([self._row(pct=30, marked=True)], paid=False, offline=True, discount_amount=5)
		self._sync(doc)
		self.assertEqual(flt(doc.discount_amount), 5)
		self.assertEqual(self._reasons(doc), [ff_paid.HEADER_DISCOUNT])

	def test_offline_sync_flag_without_offline_id(self):
		doc = self._invoice([self._row(ITEM_B)], paid=False, sync_flag=True)
		before = self._prices(doc)
		self._sync(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(self._reasons(doc), [ff_paid.MISSING_FNF])

	def test_offline_matching_fnf_no_disagreement(self):
		doc = self._invoice([self._row(pct=30, marked=True)], paid=False, offline=True)
		self._sync(doc)
		self.assertEqual(self._reasons(doc), [])
		self.assertEqual(self._ledger_amount(doc), 15)

	def test_offline_marked_full_price_row_logs_no_discount_applied(self):
		row = self._row()
		row[FIELD_DISCOUNT_SOURCE] = "friends_and_family"
		row[FIELD_FNF_AMOUNT] = 15
		doc = self._invoice([row], paid=False, offline=True)
		self._sync(doc)
		self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))
		self.assertEqual(self._logs(doc, "Discount Applied"), [])
		self.assertEqual(self._reasons(doc), [ff_paid.MISSING_FNF])
