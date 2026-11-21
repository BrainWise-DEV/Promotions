# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Task 6c — a paid invoice is never re-priced by Friends & Family (online cases).

Paid = ``flags.pos_payment_collected`` (the POS took the money) or offline sync
(see ``test_ff_offline_sync``). Desk invoices with prefilled payments are not paid
in this sense and are still priced. Also: the loyalty re-check after F&F and the
Settings kill switch.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_compatibility import _pos_rows
from posnext_promotions.promotions import ff_config, ff_ledger, ff_paid
from posnext_promotions.promotions import friends_and_family_apply as ffa
from posnext_promotions.promotions.friends_and_family_apply import (
	DISCOUNT_SOURCE_FRIENDS_AND_FAMILY as FNF,
	apply_friends_and_family_on_invoice,
	is_paid,
)
from posnext_promotions.promotions.friends_and_family_caps import (
	FIELD_DISCOUNT_SOURCE,
	FIELD_FNF_AMOUNT,
	FIELD_FNF_PERCENTAGE,
	ensure_invoice_item_fields,
)
from posnext_promotions.promotions.friends_and_family_log import (
	LOG_DOCTYPE,
	LOG_PAID_DISAGREEMENT,
	log_invoice_submit,
)
from posnext_promotions.promotions.friends_and_family_testing import (
	approved_domain_rows,
	ff_settings_guard,
	give_verified_email,
)
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	ITEM_C,
	ITEM_PRICES,
	_cart_payload,
	_ctx,
	_line,
	_make_rule,
)

PREFIX = "_T-FFP-"
RULE_PREFIX = "_PNXT_FFP_"


class PaidInvoiceFixture:
	"""Shared fixture for the paid-invoice modules (mixin: not collected on its own)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_invoice_item_fields()
		cls.ctx = _ctx()
		give_verified_email(cls.ctx.customer)

	def setUp(self):
		frappe.set_user("Administrator")
		self._stack = contextlib.ExitStack()
		self._stack.enter_context(ff_settings_guard())
		frappe.db.savepoint("ff_paid")
		# Again per test: a deadlock (1213) rolls back the whole transaction, setUpClass data too.
		give_verified_email(self.ctx.customer)
		for name in frappe.get_all("Pricing Rule", filters={"title": ["like", f"{RULE_PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("Pricing Rule", name, force=True, ignore_permissions=True)
		self._seq = 0
		self._settings()

	def tearDown(self):
		frappe.db.rollback(save_point="ff_paid")
		self._stack.close()
		ff_config.clear_cache()

	# ---- helpers ------------------------------------------------------------

	def _settings(self, **values):
		doc = frappe.get_single("Friends and Family Settings")
		doc.update(
			{
				"enabled": 1,
				"approved_domains": approved_domain_rows(),
				"discount_percentage": 30,
				"discount_scope": "Entire Cart",
				"start_date": None,
				"end_date": None,
				"applicable_brands": [],
				"applicable_item_groups": [],
				"applicable_items": [],
				"excluded_brands": [],
				"excluded_items": [],
				"excluded_item_groups": [],
				"max_discount_per_order": 0,
				"max_discount_per_customer_month": 0,
				"on_cap_breach_at_submit": "Allow and log",
			}
		)
		doc.update(values)
		doc.save()
		ff_config.clear_cache()
		return doc

	def _name(self):
		self._seq += 1
		return f"{PREFIX}{frappe.generate_hash(length=8)}-{self._seq}"

	def _row(self, item_code=ITEM_A, qty=1, pct=0, marked=False, rules=""):
		plr = ITEM_PRICES[item_code]
		rate = flt(plr * (1 - pct / 100.0), 2)
		row = frappe._dict(
			{
				"name": self._name(),
				"item_code": item_code,
				"qty": qty,
				"price_list_rate": plr,
				"rate": rate,
				"discount_percentage": pct,
				"discount_amount": flt(plr - rate, 2),
				"pricing_rules": rules,
				"is_free_item": 0,
			}
		)
		if marked:
			row[FIELD_DISCOUNT_SOURCE] = FNF
			row[FIELD_FNF_AMOUNT] = flt((plr - rate) * qty, 2)
			row[FIELD_FNF_PERCENTAGE] = pct
		return row

	def _invoice(self, rows, paid=True, offline=False, sync_flag=False, **header):
		for position, row in enumerate(rows, start=1):
			row.idx = position
		flags = frappe._dict()
		if paid:
			flags[ff_paid.PAYMENT_COLLECTED_FLAG] = True
		if sync_flag:
			flags[ff_paid.OFFLINE_SYNC_FLAG] = True
		doc = frappe._dict(
			{
				"doctype": "POS Invoice",
				"name": self._name(),
				"is_pos": 1,
				"company": self.ctx.company,
				"currency": self.ctx.currency,
				"conversion_rate": 1,
				"customer": self.ctx.customer,
				"pos_profile": self.ctx.pos_profile,
				"selling_price_list": self.ctx.price_list,
				"posting_date": nowdate(),
				"additional_discount_percentage": 0,
				"discount_amount": 0,
				"items": rows,
				"flags": flags,
				**header,
			}
		)
		if offline:
			doc.offline_id = f"OFF-{frappe.generate_hash(length=10)}"
		return doc

	@staticmethod
	def _prices(doc):
		return [
			(flt(r.rate), flt(r.discount_percentage), flt(r.discount_amount), flt(r.price_list_rate))
			for r in doc["items"]
		]

	def _cap_with_remaining(self, remaining, **values):
		"""Monthly cap = this customer's usage so far (the dev DB may hold some) + ``remaining``.

		Some usage is recorded first (rolled back with the test): on a fresh site the
		usage is 0, and a cap of 0 + 0 would mean "no cap" rather than "used up".
		"""
		period = ff_ledger.period_key(nowdate())
		invoice = self._name()
		ff_ledger.record_rows(
			[
				{
					"customer": self.ctx.customer,
					"company": self.ctx.company,
					"invoice_doctype": "POS Invoice",
					"invoice": invoice,
					"invoice_item": f"{invoice}-1",
					"posting_date": nowdate(),
					"period_key": period,
					"amount": 10,
					"base_amount": 10,
				}
			]
		)
		used = ff_ledger.get_usage(self.ctx.customer, self.ctx.company, period)
		return self._settings(max_discount_per_customer_month=flt(used) + remaining, **values)

	@staticmethod
	def _entries(doc):
		return (doc.flags.get(ff_paid.DISAGREEMENTS_FLAG) or {}).get("entries") or []

	@staticmethod
	def _logs(doc, log_type):
		return frappe.get_all(
			LOG_DOCTYPE,
			filters={"reference_name": doc.name, "log_type": log_type},
			fields=["reason", "item_code", "discount_amount", "fnf_uncapped_amount", "details"],
			order_by="creation asc, name asc",
		)

	def _ledger_amount(self, doc):
		rows = frappe.get_all(ff_ledger.LEDGER_DOCTYPE, filters={"invoice": doc.name}, fields=["amount"])
		return sum(flt(r.amount) for r in rows)

	def _gift(self, trigger=ITEM_B):
		"""Free ITEM_C with ``trigger`` (a Free Item gift: the gift policy decides it)."""
		return _make_rule(
			f"{RULE_PREFIX}GIFT",
			apply_on="Item Code",
			items=[{"item_code": trigger}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=0,
			free_item=ITEM_C,
			min_qty=1,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
		)

	def _free_row(self, rules, item_code=ITEM_C):
		row = self._row(item_code, rules=rules)
		row.update({"price_list_rate": 0, "rate": 0, "discount_amount": 0, "is_free_item": 1})
		return row

	def _gift_invoice(self, **kwargs):
		"""Paid ITEM_B with F&F 30 % (marked) + its free ITEM_C gift."""
		rule = self._gift()
		rows = [self._row(ITEM_B, pct=30, marked=True, rules=rule), self._free_row(rule)]
		return rule, self._invoice(rows, **kwargs)

	def _bxgy(self):
		return _make_rule(
			f"{RULE_PREFIX}BXGY",
			apply_on="Item Code",
			items=[{"item_code": ITEM_A}],
			price_or_product_discount="Product",
			rate_or_discount="Discount Percentage",
			same_item=1,
			min_qty=2,
			free_qty=1,
			free_item_uom="Nos",
			free_item_rate=0,
		)


class TestFFPaidFreeze(PaidInvoiceFixture, FrappeTestCase):
	# ---- is_paid ------------------------------------------------------------

	def test_is_paid_flags(self):
		self.assertFalse(is_paid(self._invoice([self._row()], paid=False)))
		self.assertTrue(is_paid(self._invoice([self._row()])))
		self.assertTrue(is_paid(self._invoice([self._row()], paid=False, offline=True)))
		self.assertTrue(is_paid(self._invoice([self._row()], paid=False, sync_flag=True)))
		# Prefilled payments alone do not make an invoice paid.
		self.assertFalse(is_paid(self._invoice([self._row()], paid=False, payments=[{"amount": 50}])))

	# ---- unpaid: today's behaviour --------------------------------------------

	def test_unpaid_online_invoice_still_repriced(self):
		doc = self._invoice([self._row()], paid=False)
		apply_friends_and_family_on_invoice(doc)
		row = doc["items"][0]
		self.assertEqual(flt(row.rate), 35)
		self.assertEqual(row.get(FIELD_DISCOUNT_SOURCE), FNF)

	def test_desk_invoice_with_payments_still_repriced(self):
		doc = self._invoice([self._row()], paid=False, payments=[frappe._dict(mode_of_payment="Cash", amount=50)])
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc["items"][0].rate), 35)
		self.assertEqual(doc["items"][0].get(FIELD_DISCOUNT_SOURCE), FNF)

	# ---- paid online ------------------------------------------------------------

	def test_online_paid_over_discount_throws(self):
		doc = self._invoice([self._row(pct=40, marked=True)])
		before = self._prices(doc)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertIn("Row 1", str(caught.exception))
		self.assertEqual(self._prices(doc), before)

	def test_online_paid_matching_fnf_kept_without_disagreement(self):
		doc = self._invoice([self._row(pct=30, marked=True), self._row(ITEM_B, qty=2, pct=30, marked=True)])
		before = self._prices(doc)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(self._entries(doc), [])
		ff_ledger.on_submit(doc)
		self.assertEqual(self._ledger_amount(doc), 15 + 48)

	def test_online_paid_under_discount_kept_and_logged(self):
		doc = self._invoice([self._row()])
		before = self._prices(doc)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual([e["reason"] for e in self._entries(doc)], [ff_paid.UNDER_DISCOUNT])
		log_invoice_submit(doc)
		logs = self._logs(doc, LOG_PAID_DISAGREEMENT)
		self.assertEqual([(log.reason, log.item_code) for log in logs], [(ff_paid.UNDER_DISCOUNT, ITEM_A)])
		self.assertEqual(flt(logs[0].fnf_uncapped_amount), 15)

	def test_online_paid_cap_breach_follows_policy(self):
		# Allow and log: saved as paid, cap breach logged, ledger records what the lines carry.
		self._cap_with_remaining(20)
		doc = self._invoice([self._row(qty=2, pct=30, marked=True)])
		before = self._prices(doc)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual([e["reason"] for e in self._entries(doc)], [ff_paid.CAP_EXCEEDED])
		ff_ledger.on_submit(doc)
		log_invoice_submit(doc)
		self.assertEqual(self._ledger_amount(doc), 30)
		self.assertEqual([log.reason for log in self._logs(doc, LOG_PAID_DISAGREEMENT)], [ff_paid.CAP_EXCEEDED])
		# One invoice-level Cap Hit row: the ledger's breach row, not a second one from the log.
		self.assertEqual([log.reason for log in self._logs(doc, "Cap Hit") if not log.item_code], [ff_ledger.REASON_BREACH])

		# Block submit: validate still saves (drafts), submit raises.
		self._cap_with_remaining(20, on_cap_breach_at_submit="Block submit")
		blocked = self._invoice([self._row(qty=2, pct=30, marked=True)])
		apply_friends_and_family_on_invoice(blocked)
		with self.assertRaises(frappe.ValidationError):
			ff_ledger.on_submit(blocked)
		self.assertEqual(self._ledger_amount(blocked), 0)

	def test_paid_invoice_compatibility_disagreement_throws(self):
		rule = self._bxgy()
		doc = self._invoice([self._row(qty=2, pct=30, marked=True, rules=rule)])
		before = self._prices(doc)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertIn("Row 1", str(caught.exception))
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(doc["items"][0].get(FIELD_DISCOUNT_SOURCE), FNF)  # not stripped

	def test_paid_bxgy_fnf_accepted_when_gift_allowed(self):
		# Task 8s: Allow Free Gift with F&F covers Buy X Get Y, so the line may carry F&F.
		self._settings(allow_free_gift_with_ff=1)
		rule = self._bxgy()
		doc = self._invoice(
			[self._row(qty=2, pct=30, marked=True, rules=rule), self._free_row(rule, item_code=ITEM_A)]
		)
		before = self._prices(doc)
		apply_friends_and_family_on_invoice(doc)  # no compatibility throw
		self.assertEqual(self._prices(doc), before)
		self.assertEqual(len(doc["items"]), 2)
		self.assertEqual(self._entries(doc), [])
		self.assertEqual(doc["items"][0].get(FIELD_DISCOUNT_SOURCE), FNF)

	def test_paid_online_forbidden_gift_logged_not_removed(self):
		self._settings(allow_free_gift_with_ff=0)
		rule, doc = self._gift_invoice()
		before = self._prices(doc)
		apply_friends_and_family_on_invoice(doc)  # never throws for a gift
		self.assertEqual(self._prices(doc), before)
		self.assertEqual([r.is_free_item for r in doc["items"]], [0, 1])
		self.assertEqual(doc["items"][0].pricing_rules, rule)
		gift = [e for e in self._entries(doc) if e["reason"] == ff_paid.GIFT_WITH_FNF]
		self.assertEqual([(e["item_code"], e["rules"]) for e in gift], [(ITEM_C, [rule])])
		log_invoice_submit(doc)
		self.assertIn(ff_paid.GIFT_WITH_FNF, [log.reason for log in self._logs(doc, LOG_PAID_DISAGREEMENT)])

	def test_paid_allowed_gift_not_logged(self):
		self._settings(allow_free_gift_with_ff=1)
		_rule, doc = self._gift_invoice()
		apply_friends_and_family_on_invoice(doc)
		self.assertNotIn(ff_paid.GIFT_WITH_FNF, [e["reason"] for e in self._entries(doc)])
		self.assertEqual(len(doc["items"]), 2)

	def test_paid_header_discount_not_rewritten(self):
		doc = self._invoice([self._row(ITEM_B, qty=10)], coupon_code="FFP-COUPON", discount_amount=500)
		with patch.object(ffa, "_pos_coupon_value", return_value=(50.0, None)):
			with self.assertRaises(frappe.ValidationError) as caught:
				apply_friends_and_family_on_invoice(doc)
		self.assertIn("FFP-COUPON", str(caught.exception))
		self.assertEqual(flt(doc.discount_amount), 500)
		self.assertEqual(doc.coupon_code, "FFP-COUPON")

	def test_paid_header_stacked_on_fnf_kept_and_logged(self):
		doc = self._invoice([self._row(pct=30, marked=True)], discount_amount=5)
		before = self._prices(doc)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.discount_amount), 5)
		self.assertEqual(self._prices(doc), before)
		log_invoice_submit(doc)
		self.assertEqual([log.reason for log in self._logs(doc, LOG_PAID_DISAGREEMENT)], [ff_paid.HEADER_DISCOUNT])

	def test_pos_paid_document_save_then_submit(self):
		"""Real Sales Invoice from the POS payload: insert, reload, submit (as pos_next does)."""
		paid = self._pos_invoice([(ITEM_A, 1), (ITEM_B, 2)])
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.ctx.company,
				"customer": self.ctx.customer,
				"is_pos": 1,
				"pos_profile": self.ctx.pos_profile,
				"selling_price_list": self.ctx.price_list,
				"currency": self.ctx.currency,
				"update_stock": 0,
				# pos_next turns ERPNext pricing off and clears the rows' pricing_rules.
				"ignore_pricing_rule": 1,
				"items": [
					{**{k: v for k, v in row.items() if k not in ("name", "idx")}, "pricing_rules": ""}
					for row in paid["items"]
				],
			}
		)
		doc.flags.ignore_pricing_rule = True
		doc.set_missing_values()
		doc.update_stock = 0
		doc.taxes_and_charges = None
		doc.taxes = []
		doc.calculate_taxes_and_totals()
		sent = [(flt(r.rate), flt(r.price_list_rate)) for r in doc.items]
		self.assertEqual(sent, [(35, 50), (56, 80)])
		grand_total = flt(doc.grand_total)
		doc.payments = []
		doc.append("payments", {"mode_of_payment": self.ctx.mode_of_payment, "amount": doc.rounded_total or grand_total})
		doc.flags[ff_paid.PAYMENT_COLLECTED_FLAG] = True
		doc.insert(set_name=self._name())

		doc = frappe.get_doc("Sales Invoice", doc.name)
		doc.flags[ff_paid.PAYMENT_COLLECTED_FLAG] = True
		doc.submit()
		self.assertEqual([(flt(r.rate), flt(r.price_list_rate)) for r in doc.items], sent)
		self.assertEqual(flt(doc.grand_total), grand_total)
		saved = frappe.get_all(
			"Sales Invoice Item",
			filters={"parent": doc.name},
			fields=[FIELD_DISCOUNT_SOURCE, FIELD_FNF_AMOUNT],
			order_by="idx",
		)
		self.assertEqual([(r[FIELD_DISCOUNT_SOURCE], flt(r[FIELD_FNF_AMOUNT])) for r in saved], [(FNF, 15), (FNF, 48)])
		self.assertEqual(self._ledger_amount(doc), 63)
		self.assertEqual(self._logs(doc, LOG_PAID_DISAGREEMENT), [])

	# ---- the POS's real payload --------------------------------------------------

	def _pos_invoice(self, lines):
		payload = _cart_payload(self.ctx, [_line(self.ctx, code, qty=qty) for code, qty in lines])
		resp = apply_offers(invoice_data=json.dumps(payload), selected_offers=json.dumps([]))
		rows = []
		for raw in _pos_rows(resp):
			raw.pop("_cart_source", None)
			unit = raw.pop("_cart_unit_discount", 0)
			raw["discount_amount"] = unit  # invoice rows hold it per unit
			raw["name"] = self._name()
			rows.append(frappe._dict(raw))
		return self._invoice(rows)

	def test_pos_payload_without_markers_recognised_as_fnf(self):
		# The POS sends no F&F marker: a row whose discount is the program's is F&F.
		doc = self._pos_invoice([(ITEM_A, 1), (ITEM_B, 2)])
		before = self._prices(doc)
		self.assertTrue(all(not r.get(FIELD_DISCOUNT_SOURCE) for r in doc["items"]))
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertEqual([r.get(FIELD_DISCOUNT_SOURCE) for r in doc["items"]], [FNF, FNF])
		self.assertEqual([flt(r.get(FIELD_FNF_AMOUNT)) for r in doc["items"]], [15, 48])
		self.assertEqual(self._entries(doc), [])
		ff_ledger.on_submit(doc)
		self.assertEqual(self._ledger_amount(doc), 63)

	def test_pos_payload_capped_line_recognised(self):
		self._cap_with_remaining(20)
		doc = self._pos_invoice([(ITEM_B, 1)])  # cart capped: 20 of the 24
		self.assertEqual(flt(doc["items"][0].rate), 60)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc["items"][0].rate), 60)
		self.assertEqual(doc["items"][0].get(FIELD_DISCOUNT_SOURCE), FNF)
		self.assertEqual(self._entries(doc), [])
		ff_ledger.on_submit(doc)
		self.assertEqual(self._ledger_amount(doc), 20)

	def test_unmarked_bigger_discount_is_not_fnf(self):
		# A 40% manual discount beats F&F on the unpaid path too: kept, not F&F, no throw.
		doc = self._invoice([self._row(pct=40)])
		apply_friends_and_family_on_invoice(doc)
		self.assertFalse(doc["items"][0].get(FIELD_DISCOUNT_SOURCE))
		self.assertEqual(flt(doc["items"][0].rate), 30)
		self.assertEqual(self._entries(doc), [])

	# ---- loyalty redemption -------------------------------------------------------

	def test_loyalty_amount_above_total_after_ff_throws(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.ctx.company,
				"customer": self.ctx.customer,
				"is_pos": 1,
				"pos_profile": self.ctx.pos_profile,
				"selling_price_list": self.ctx.price_list,
				"update_stock": 0,
				"items": [{"item_code": ITEM_A, "qty": 20, "price_list_rate": 50, "rate": 50}],
			}
		)
		doc.set_missing_values()
		doc.update_stock = 0
		doc.taxes = []
		doc.calculate_taxes_and_totals()
		self.assertEqual(flt(doc.grand_total), 1000)
		doc.redeem_loyalty_points = 1
		doc.loyalty_amount = 900  # fits the total before F&F
		self._settings(discount_percentage=20)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc.grand_total), 800)
		self.assertIn("Loyalty redemption", str(caught.exception))

	def _loyalty_invoice(self, **header):
		return self._invoice([self._row()], paid=False, redeem_loyalty_points=1, **header)

	def test_loyalty_within_total_passes(self):
		doc = self._loyalty_invoice(loyalty_amount=10, grand_total=35, rounded_total=35)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(doc["items"][0].rate), 35)

	def test_loyalty_rounded_total_accepted_as_erpnext_does(self):
		# ERPNext checks against rounded_total unless rounding is disabled.
		doc = self._loyalty_invoice(
			loyalty_amount=800, grand_total=799.60, rounded_total=800, disable_rounded_total=0
		)
		apply_friends_and_family_on_invoice(doc)  # no throw

	def test_loyalty_rounding_disabled_uses_grand_total(self):
		doc = self._loyalty_invoice(
			loyalty_amount=800, grand_total=799.60, rounded_total=800, disable_rounded_total=1
		)
		with self.assertRaises(frappe.ValidationError):
			apply_friends_and_family_on_invoice(doc)

	def test_stale_loyalty_amount_without_redeem_box_not_thrown(self):
		doc = self._invoice(
			[self._row()], paid=False, redeem_loyalty_points=0, loyalty_amount=900, grand_total=35, rounded_total=35
		)
		apply_friends_and_family_on_invoice(doc)  # no throw
		self.assertEqual(flt(doc["items"][0].rate), 35)

	def test_paid_over_redemption_still_thrown(self):
		doc = self._invoice(
			[self._row(pct=30, marked=True)],
			redeem_loyalty_points=1,
			loyalty_amount=40,
			grand_total=35,
			rounded_total=35,
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			apply_friends_and_family_on_invoice(doc)
		self.assertIn("Loyalty redemption", str(caught.exception))

	# ---- stale markers (Ruling 48) -------------------------------------------------

	def test_marked_full_price_row_loses_stale_markers(self):
		row = self._row()  # full price, but the client says F&F
		row[FIELD_DISCOUNT_SOURCE] = FNF
		row[FIELD_FNF_AMOUNT] = 15
		row[FIELD_FNF_PERCENTAGE] = 30
		row.discount_source = FNF
		doc = self._invoice([row])
		before = self._prices(doc)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(self._prices(doc), before)
		self.assertFalse(row.get(FIELD_DISCOUNT_SOURCE))
		self.assertFalse(flt(row.get(FIELD_FNF_AMOUNT)))
		self.assertFalse(flt(row.get(FIELD_FNF_PERCENTAGE)))
		self.assertFalse(row.get("discount_source"))
		ff_ledger.on_submit(doc)
		log_invoice_submit(doc)
		self.assertEqual(self._logs(doc, "Discount Applied"), [])
		self.assertEqual([log.reason for log in self._logs(doc, LOG_PAID_DISAGREEMENT)], [ff_paid.UNDER_DISCOUNT])
		self.assertEqual(self._ledger_amount(doc), 0)

	# ---- cap audit log on a paid sale (D-R2-02) ------------------------------------

	def _cap_logs(self, doc):
		return frappe.get_all(
			LOG_DOCTYPE,
			filters={"reference_name": doc.name},
			fields=["log_type", "item_code", "discount_amount", "fnf_uncapped_amount", "capped", "cap_hit", "reason"],
			order_by="creation asc, name asc",
		)

	def test_paid_capped_sale_logs_the_cap(self):
		# D-R2-02: per-order cap 50, ITEM_B ×3 (F&F would be 72). The cart caps it; the paid
		# invoice's log must say so, from the server's own capped computation.
		self._settings(max_discount_per_order=50)
		doc = self._pos_invoice([(ITEM_B, 3)])
		for row in doc["items"]:  # the markers the POS sends (fnfSubmissionMarkers)
			row[FIELD_DISCOUNT_SOURCE] = FNF
			row[FIELD_FNF_AMOUNT] = flt((row.price_list_rate - row.rate) * row.qty, 2)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(self._entries(doc), [])
		log_invoice_submit(doc)
		logs = self._cap_logs(doc)
		[applied] = [log for log in logs if log.log_type == "Discount Applied"]
		self.assertEqual(applied.capped, 1)
		self.assertEqual(applied.cap_hit, "order")
		self.assertAlmostEqual(flt(applied.fnf_uncapped_amount), 72)
		self.assertAlmostEqual(flt(applied.discount_amount), flt(doc["items"][0].get(FIELD_FNF_AMOUNT)))
		[summary] = [log for log in logs if log.log_type == "Cap Hit"]
		self.assertIsNone(summary.item_code)
		self.assertEqual(summary.capped, 1)
		self.assertEqual(summary.cap_hit, "order")
		self.assertAlmostEqual(flt(summary.fnf_uncapped_amount), 72)
		self.assertAlmostEqual(flt(summary.discount_amount), 50, delta=3 * 0.01)  # 49.98: per-unit rounding

	def test_paid_line_dropped_by_cap_logs_cap_hit(self):
		# The 00077 case: the cap leaves a line no F&F; that line is a Cap Hit row.
		self._cap_with_remaining(0)
		doc = self._invoice([self._row(ITEM_B)])  # full price: the cap is used up
		apply_friends_and_family_on_invoice(doc)
		log_invoice_submit(doc)
		lines = [log for log in self._cap_logs(doc) if log.item_code]
		self.assertEqual([(log.log_type, log.reason, log.cap_hit) for log in lines], [("Cap Hit", "cap_reached", "monthly")])
		self.assertAlmostEqual(flt(lines[0].discount_amount), 0)

	def test_paid_cap_hit_row_follows_what_the_ledger_wrote(self):
		# Review M-1: ITEM_A ×7 carries 50.03 at a per-order cap of 50. Validate allows
		# qty × 0.01 (no cap_exceeded), the ledger one unit (breach row). One Cap Hit row only.
		self._settings(max_discount_per_order=50)
		row = self._row(ITEM_A, qty=7, marked=True)
		row.rate = 50 - 50.03 / 7
		row.discount_amount = 50.03 / 7
		row[FIELD_DISCOUNT_SOURCE] = FNF
		row[FIELD_FNF_AMOUNT] = 50.03
		doc = self._invoice([row])
		apply_friends_and_family_on_invoice(doc)
		self.assertNotIn(ff_paid.CAP_EXCEEDED, [e["reason"] for e in self._entries(doc)])
		ff_ledger.on_submit(doc)
		log_invoice_submit(doc)
		summary = [log for log in self._cap_logs(doc) if log.log_type == "Cap Hit" and not log.item_code]
		self.assertEqual([log.reason for log in summary], [ff_ledger.REASON_BREACH])

	def test_paid_uncapped_sale_logs_no_cap(self):
		doc = self._pos_invoice([(ITEM_B, 1)])
		apply_friends_and_family_on_invoice(doc)
		log_invoice_submit(doc)
		logs = self._cap_logs(doc)
		self.assertEqual([log.log_type for log in logs], ["Discount Applied"])
		self.assertEqual(logs[0].capped, 0)
		self.assertIsNone(logs[0].cap_hit)

	# ---- kill switch ------------------------------------------------------------------

	def test_disable_during_open_cart(self):
		priced = self._invoice([self._row()], paid=False)
		apply_friends_and_family_on_invoice(priced)  # cart priced with F&F
		self.assertEqual(flt(priced["items"][0].rate), 35)
		self._settings(enabled=0)

		unpaid = self._invoice([frappe._dict(priced["items"][0])], paid=False)
		apply_friends_and_family_on_invoice(unpaid)
		self.assertEqual(flt(unpaid["items"][0].rate), 50)
		self.assertFalse(unpaid["items"][0].get(FIELD_DISCOUNT_SOURCE))

		paid = self._invoice([frappe._dict(priced["items"][0])])
		before = self._prices(paid)
		with self.assertRaises(frappe.ValidationError):
			apply_friends_and_family_on_invoice(paid)
		self.assertEqual(self._prices(paid), before)

	def test_disable_with_blank_percentage_saves(self):
		doc = self._settings(enabled=0, discount_percentage=None)
		self.assertEqual(flt(doc.discount_percentage), 0)
		self.assertEqual(frappe.db.get_single_value("Friends and Family Settings", "enabled"), 0)

