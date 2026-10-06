# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""PN-131 — Friends & Family audit log."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.promotions import best_discount as bd
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY,
	apply_friends_and_family_on_invoice,
)
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	ensure_invoice_item_fields,
)
from posnext_promotions.promotions.friends_and_family_eligibility import (
	FLAG_FIELD,
	ensure_customer_flag_field,
)
from posnext_promotions.promotions.friends_and_family_log import (
	LOG_APPLIED,
	LOG_CAP_HIT,
	LOG_COMPARISON_LOST,
	LOG_DOCTYPE,
	LOG_ELIGIBILITY_APPLIED,
	LOG_ELIGIBILITY_REMOVED,
	LOG_EXCLUDED,
	LOG_ORDER_COMPARISON,
	LOG_RETURNED,
	LOG_REVERSED,
	LOG_SETTINGS_CHANGED,
	log_invoice_cancel,
	log_invoice_submit,
	settings_diff,
)

DATE = "2026-10-05"
FF_GROUP = "_Test FF Log Customer Group"
OTHER_GROUP = "_Test FF Log Other Group"
CUSTOMER = "_Test FF Log Customer"
PLAIN = "_Test FF Log Plain Customer"
ITEM = "_Test FF Log Item"
ITEM_2 = "_Test FF Log Item 2"
EXCLUDED_ITEM = "_Test FF Log Excluded Item"
PROMO_SOURCE = "item_level_promotion"
CASHIER = "_test_ff_log_cashier@example.com"
FIELDS = [
	"name",
	"log_type",
	"customer",
	"reference_doctype",
	"reference_name",
	"item_code",
	"discount_amount",
	"discount_percentage",
	"discount_source",
	"comparison_outcome",
	"competing_source",
	"competing_amount",
	"capped",
	"cap_hit",
	"reason",
	"is_cancelled",
	"return_against",
	"message",
	"details",
	"pos_profile",
	"user",
	"posting_date",
]


class TestFriendsAndFamilyLog(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()
		ensure_invoice_item_fields()
		root_cg = frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ""}) or (
			"All Customer Groups"
		)
		for name in (FF_GROUP, OTHER_GROUP):
			if not frappe.db.exists("Customer Group", name):
				frappe.get_doc(
					{"doctype": "Customer Group", "customer_group_name": name, "parent_customer_group": root_cg}
				).insert()
		for name, group in ((CUSTOMER, FF_GROUP), (PLAIN, OTHER_GROUP)):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": group}).insert()
			frappe.db.set_value("Customer", name, {FLAG_FIELD: 0, "disabled": 0, "customer_group": group})
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or "Products"
		for code in (ITEM, ITEM_2, EXCLUDED_ITEM):
			if not frappe.db.exists("Item", code):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": code,
						"item_name": code,
						"item_group": item_group,
						"stock_uom": "Nos",
						"is_stock_item": 0,
					}
				).insert()
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}
		)

	def setUp(self):
		frappe.db.savepoint("pn131_case")
		self._configure()
		self._seq = 0

	def tearDown(self):
		frappe.db.rollback(save_point="pn131_case")

	def _configure(self, **values):
		doc = frappe.get_single("Friends and Family Settings")
		doc.update(
			{
				"enabled": 1,
				"discount_percentage": 30,
				"discount_scope": "Entire Cart",
				"start_date": None,
				"end_date": None,
				"applicable_brands": [],
				"applicable_item_groups": [],
				"applicable_items": [],
				"excluded_brands": [],
				"excluded_items": [{"item_code": EXCLUDED_ITEM}],
				"excluded_item_groups": [],
				"eligibility_basis": "Customer Group or Flag",
				"eligible_customer_groups": [{"customer_group": FF_GROUP}],
				"user_message": "Friends & Family applied",
				"max_discount_per_order": 0,
				"max_discount_per_customer_month": 0,
			}
		)
		doc.update(values)
		doc.save()
		return doc

	def _invoice(self, items, customer=CUSTOMER, **header):
		self._seq += 1
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.company,
				"customer": customer,
				"posting_date": DATE,
				"set_posting_time": 1,
				"is_pos": 1,
				"items": items,
				**header,
			}
		)
		doc.name = f"_T-PN131-{self._testMethodName[-20:]}-{self._seq}"
		return doc

	def _row(self, item_code=ITEM, qty=1, rate=100, **extra):
		return {"item_code": item_code, "qty": qty, "price_list_rate": rate, "rate": rate, **extra}

	def _submit(self, doc):
		apply_friends_and_family_on_invoice(doc)
		log_invoice_submit(doc)
		return self._logs(doc.name)

	def _logs(self, reference_name, **filters):
		return frappe.get_all(
			LOG_DOCTYPE,
			filters={"reference_name": reference_name, **filters},
			fields=FIELDS,
			order_by="creation asc, name asc",
		)

	# ---- invoice decisions -------------------------------------------------

	def test_applied_discount_has_customer_invoice_item_value_source(self):
		doc = self._invoice([self._row(qty=2)], pos_profile=None)
		logs = self._submit(doc)
		self.assertEqual(len(logs), 1)
		log = logs[0]
		self.assertEqual(log.log_type, LOG_APPLIED)
		self.assertEqual(log.customer, CUSTOMER)
		self.assertEqual(log.reference_doctype, "Sales Invoice")
		self.assertEqual(log.reference_name, doc.name)
		self.assertEqual(log.item_code, ITEM)
		self.assertEqual(flt(log.discount_amount), 60)
		self.assertEqual(flt(log.discount_percentage), 30)
		self.assertEqual(log.discount_source, DISCOUNT_SOURCE_FRIENDS_AND_FAMILY)
		self.assertEqual(log.comparison_outcome, bd.OUTCOME_FNF_ONLY)
		self.assertEqual(str(log.posting_date), DATE)
		self.assertEqual(log.user, frappe.session.user)
		details = json.loads(log.details)
		self.assertEqual(details["eligibility"], "customer_group")

	def test_promo_vs_fnf_outcome_logged_both_ways(self):
		# Rows tagged as a line promotion (no real Pricing Rule needed for totals).
		weaker = self._row(discount_percentage=10, discount_source=PROMO_SOURCE)
		weaker["rate"] = 90
		stronger = self._row(item_code=ITEM_2, discount_percentage=40, discount_source=PROMO_SOURCE)
		stronger["rate"] = 60
		logs = self._submit(self._invoice([weaker, stronger]))
		by_item = {log.item_code: log for log in logs}

		won = by_item[ITEM]
		self.assertEqual(won.log_type, LOG_APPLIED)
		self.assertEqual(won.comparison_outcome, bd.OUTCOME_FNF_BETTER)
		self.assertEqual(won.competing_source, bd.SOURCE_PROMOTION)
		self.assertEqual(flt(won.competing_amount), 10)
		self.assertEqual(flt(won.discount_amount), 30)

		lost = by_item[ITEM_2]
		self.assertEqual(lost.log_type, LOG_COMPARISON_LOST)
		self.assertEqual(lost.discount_source, bd.SOURCE_PROMOTION)
		self.assertEqual(lost.comparison_outcome, bd.OUTCOME_OTHER_BETTER)
		self.assertEqual(flt(lost.competing_amount), 40)
		self.assertEqual(flt(lost.discount_amount), 0)
		self.assertTrue(lost.message)

	def test_exclusion_logged(self):
		logs = self._submit(self._invoice([self._row(), self._row(item_code=EXCLUDED_ITEM)]))
		types = {log.item_code: log.log_type for log in logs}
		self.assertEqual(types, {ITEM: LOG_APPLIED, EXCLUDED_ITEM: LOG_EXCLUDED})
		excluded = next(log for log in logs if log.item_code == EXCLUDED_ITEM)
		self.assertEqual(excluded.reason, "excluded")

	def test_order_cap_logged_on_lines_and_invoice(self):
		self._configure(max_discount_per_order=25)
		logs = self._submit(self._invoice([self._row(qty=2)]))
		applied = [log for log in logs if log.log_type == LOG_APPLIED]
		self.assertEqual(len(applied), 1)
		self.assertEqual(applied[0].capped, 1)
		self.assertEqual(applied[0].cap_hit, "order")
		self.assertEqual(flt(applied[0].discount_amount), 25)
		summary = [log for log in logs if log.log_type == LOG_CAP_HIT]
		self.assertEqual(len(summary), 1)
		self.assertIsNone(summary[0].item_code)
		self.assertEqual(flt(summary[0].discount_amount), 25)
		self.assertIn("per-order maximum", summary[0].message)
		self.assertEqual(json.loads(summary[0].details)["fnf_uncapped_total"], 60)

	def test_monthly_cap_exhausted_logged_as_cap_hit(self):
		self._configure(max_discount_per_customer_month=10)
		from unittest.mock import patch

		with patch(
			"posnext_promotions.promotions.friends_and_family_caps.get_monthly_usage", return_value=10
		):
			logs = self._submit(self._invoice([self._row()]))
		line = next(log for log in logs if log.item_code == ITEM)
		self.assertEqual(line.log_type, LOG_CAP_HIT)
		self.assertEqual(line.reason, "cap_reached")
		self.assertEqual(line.cap_hit, "monthly")
		self.assertEqual(flt(line.discount_amount), 0)

	def test_order_level_comparison_logged(self):
		logs = self._submit(
			self._invoice([self._row(), self._row(item_code=ITEM_2)], discount_amount=90, apply_discount_on="Net Total")
		)
		order = [log for log in logs if log.log_type == LOG_ORDER_COMPARISON]
		self.assertEqual(len(order), 1)
		self.assertEqual(order[0].discount_source, bd.SOURCE_ORDER_MANUAL)
		self.assertEqual(order[0].comparison_outcome, bd.OUTCOME_OTHER_BETTER)
		self.assertEqual(flt(order[0].competing_amount), 90)
		lines = [log for log in logs if log.item_code]
		self.assertEqual({log.log_type for log in lines}, {LOG_COMPARISON_LOST})
		self.assertEqual({log.reason for log in lines}, {"order_discount_better"})
		self.assertEqual({log.discount_source for log in lines}, {bd.SOURCE_ORDER_MANUAL})

	def test_ineligible_customer_logs_nothing(self):
		self.assertEqual(self._submit(self._invoice([self._row()], customer=PLAIN)), [])

	def test_stripped_stamp_logged_as_eligibility_removed(self):
		row = self._row(discount_percentage=30, **{FIELD_DISCOUNT_SOURCE: DISCOUNT_SOURCE_FRIENDS_AND_FAMILY})
		row["rate"] = 70
		logs = self._submit(self._invoice([row], customer=PLAIN))
		self.assertEqual([log.log_type for log in logs], [LOG_ELIGIBILITY_REMOVED])
		self.assertEqual(logs[0].reason, "stripped")

	def test_consolidated_invoice_not_logged_twice(self):
		doc = self._invoice([self._row(**{FIELD_DISCOUNT_SOURCE: DISCOUNT_SOURCE_FRIENDS_AND_FAMILY, FIELD_FNF_AMOUNT: 30})])
		doc.is_consolidated = 1
		doc.flags.friends_and_family = {"eligible": True, "items": [{"applies": True, "reason": "applied"}]}
		log_invoice_submit(doc)
		log_invoice_cancel(doc)
		self.assertEqual(self._logs(doc.name), [])

	def test_fallback_without_engine_payload(self):
		doc = self._invoice([self._row(**{FIELD_DISCOUNT_SOURCE: DISCOUNT_SOURCE_FRIENDS_AND_FAMILY, FIELD_FNF_AMOUNT: 12.5})])
		log_invoice_submit(doc)
		logs = self._logs(doc.name)
		self.assertEqual([(log.log_type, flt(log.discount_amount)) for log in logs], [(LOG_APPLIED, 12.5)])

	# ---- cancel / return ----------------------------------------------------

	def test_cancel_flags_rows_and_appends_reversal(self):
		doc = self._invoice([self._row(qty=2), self._row(item_code=EXCLUDED_ITEM)])
		self._submit(doc)
		log_invoice_cancel(doc)
		logs = self._logs(doc.name)
		self.assertTrue(all(log.is_cancelled for log in logs))
		reversals = [log for log in logs if log.log_type == LOG_REVERSED]
		self.assertEqual(len(reversals), 1)
		self.assertEqual(flt(reversals[0].discount_amount), -60)
		applied = sum(flt(log.discount_amount) for log in logs)
		self.assertEqual(applied, 0)  # applied 60 + reversal -60

	def test_return_logged_with_negative_amount(self):
		row = self._row(qty=-1, discount_percentage=30, **{FIELD_DISCOUNT_SOURCE: DISCOUNT_SOURCE_FRIENDS_AND_FAMILY})
		row["rate"] = 70
		plain = self._row(item_code=ITEM_2, qty=-1)
		doc = self._invoice([row, plain], is_return=1, return_against="_T-PN131-ORIG")
		logs = self._submit(doc)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].log_type, LOG_RETURNED)
		self.assertEqual(flt(logs[0].discount_amount), -30)
		self.assertEqual(logs[0].return_against, "_T-PN131-ORIG")

	# ---- customer eligibility ----------------------------------------------

	def _customer_logs(self, customer):
		return frappe.get_all(
			LOG_DOCTYPE,
			filters={"reference_doctype": "Customer", "reference_name": customer},
			fields=FIELDS,
			order_by="creation asc, name asc",
		)

	def test_customer_flag_and_group_changes_logged(self):
		customer = frappe.get_doc("Customer", PLAIN)
		customer.set(FLAG_FIELD, 1)
		customer.save()
		customer.reload()
		customer.set(FLAG_FIELD, 0)
		customer.save()
		customer.reload()
		customer.customer_group = FF_GROUP
		customer.save()
		logs = self._customer_logs(PLAIN)
		self.assertEqual(
			[(log.log_type, log.reason) for log in logs],
			[
				(LOG_ELIGIBILITY_APPLIED, "customer_flag"),
				(LOG_ELIGIBILITY_REMOVED, "customer_flag"),
				(LOG_ELIGIBILITY_APPLIED, "customer_group"),
			],
		)
		self.assertEqual(logs[0].customer, PLAIN)
		self.assertEqual(json.loads(logs[2].details)["changes"]["customer_group"], [OTHER_GROUP, FF_GROUP])

	def test_customer_change_without_membership_change_not_logged(self):
		self._configure(eligibility_basis="Customer Group")
		customer = frappe.get_doc("Customer", PLAIN)
		customer.set(FLAG_FIELD, 1)  # flag ignored under group-only basis
		customer.save()
		self.assertEqual(self._customer_logs(PLAIN), [])

	def test_disabling_member_logged(self):
		customer = frappe.get_doc("Customer", CUSTOMER)
		customer.disabled = 1
		customer.save()
		logs = self._customer_logs(CUSTOMER)
		self.assertEqual([log.log_type for log in logs], [LOG_ELIGIBILITY_REMOVED])

	# ---- settings -----------------------------------------------------------

	def test_settings_change_logged_with_diff(self):
		before = len(frappe.get_all(LOG_DOCTYPE, filters={"log_type": LOG_SETTINGS_CHANGED}))
		self._configure(discount_percentage=25, excluded_items=[{"item_code": ITEM_2}])
		logs = frappe.get_all(
			LOG_DOCTYPE,
			filters={"log_type": LOG_SETTINGS_CHANGED},
			fields=FIELDS,
			order_by="creation desc, name desc",
		)
		self.assertEqual(len(logs), before + 1)
		log = logs[0]
		self.assertEqual(log.reference_name, "Friends and Family Settings")
		diff = json.loads(log.details)
		self.assertEqual(diff["discount_percentage"]["old"], 30)
		self.assertEqual(diff["discount_percentage"]["new"], 25)
		self.assertEqual(diff["excluded_items"]["added"], [ITEM_2])
		self.assertEqual(diff["excluded_items"]["removed"], [EXCLUDED_ITEM])
		self.assertIn("30.0 → 25.0", log.message)
		# Saving with no change adds nothing.
		self._configure(discount_percentage=25, excluded_items=[{"item_code": ITEM_2}])
		self.assertEqual(len(frappe.get_all(LOG_DOCTYPE, filters={"log_type": LOG_SETTINGS_CHANGED})), before + 1)

	def test_settings_diff_helper(self):
		doc = frappe.get_single("Friends and Family Settings")
		copy = frappe.copy_doc(doc)
		copy.user_message = "Changed"
		self.assertEqual(list(settings_diff(doc, copy)), ["user_message"])
		self.assertEqual(settings_diff(doc, doc), {})

	def test_settings_and_customer_have_version_history(self):
		self.assertTrue(frappe.get_meta("Friends and Family Settings").track_changes)
		self.assertTrue(frappe.get_meta("Customer").track_changes)

	# ---- permissions / immutability ----------------------------------------

	def test_read_only_for_cashiers(self):
		if not frappe.db.exists("User", CASHIER):
			user = frappe.get_doc(
				{"doctype": "User", "email": CASHIER, "first_name": "FF Log Cashier", "send_welcome_email": 0}
			)
			user.append("roles", {"role": "Sales User"})
			user.insert(ignore_permissions=True)
		self.assertFalse(frappe.has_permission(LOG_DOCTYPE, "read", user=CASHIER))
		perms = {p.role: p for p in frappe.get_meta(LOG_DOCTYPE).permissions}
		self.assertEqual(set(perms), {"System Manager", "Sales Manager"})
		self.assertTrue(all(not p.write and not p.create for p in perms.values()))
		self.assertTrue(frappe.get_meta(LOG_DOCTYPE).in_create)

	def test_entries_cannot_be_edited(self):
		doc = self._invoice([self._row()])
		log = frappe.get_doc(LOG_DOCTYPE, self._submit(doc)[0].name)
		log.discount_amount = 999
		log.flags.ignore_links = True  # test invoice is not in the DB
		with self.assertRaises(frappe.PermissionError):
			log.save(ignore_permissions=True)

	def test_hooks_registered(self):
		events = frappe.get_hooks("doc_events")
		for doctype in ("Sales Invoice", "POS Invoice"):
			self.assertIn(
				"posnext_promotions.promotions.friends_and_family_log.log_invoice_submit",
				frappe.as_json(events[doctype]["on_submit"]),
			)
			self.assertIn(
				"posnext_promotions.promotions.friends_and_family_log.log_invoice_cancel",
				frappe.as_json(events[doctype]["on_cancel"]),
			)
		self.assertIn("log_customer_eligibility_change", frappe.as_json(events["Customer"]["on_update"]))
		# The audit trail never blocks deleting an invoice / customer.
		self.assertIn(LOG_DOCTYPE, frappe.get_hooks("ignore_links_on_delete"))
