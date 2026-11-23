# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Task 6f — scope integrity, fewer queries, access checks.

* Variants follow their template's scope and exclusion rows.
* Scope fields (group, brand, template) always come from Item, never the client.
* Item-group trees are forgotten when an Item Group changes.
* Rule flags, eligibility, Allow F&F and usage are read once per request.
* One invoice event writes its log rows with one bulk insert.
* ``apply_offers`` checks Customer read; the program payload is only for sellers.
* A coupon that wins on the invoice leaves a rounded header discount.
* ``period_key_index`` survives the Usage schema sync.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from posnext_promotions.api.apply_offers import apply_offers
from posnext_promotions.api.friends_and_family import get_friends_and_family_program
from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_coupon_integrity import (
	_exclusive_tax,
)
from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_paid_freeze import (
	PaidInvoiceFixture,
)
from posnext_promotions.promotions import ff_classifier, ff_ledger, ff_paid, ff_scope
from posnext_promotions.promotions import friends_and_family_eligibility as eligibility_module
from posnext_promotions.promotions.friends_and_family import PROFILE_ALLOW_FIELD
from posnext_promotions.promotions.friends_and_family_apply import (
	REASON_EXCLUDED,
	REASON_OUT_OF_SCOPE,
	apply_friends_and_family_on_invoice,
	compute_friends_and_family_discounts,
	promotion_competes_with_ff,
)
from posnext_promotions.promotions.friends_and_family_log import LOG_DOCTYPE, log_invoice_submit
from posnext_promotions.promotions.friends_and_family_testing import ensure_named_pricing_rule
from posnext_promotions.test_promotions import (
	ITEM_A,
	ITEM_B,
	_cart_payload,
	_line,
	_make_pos_coupon,
	_make_rule,
)

TEMPLATE = "_PNXT_FFSP_TEMPLATE"
VARIANT = "_PNXT_FFSP_VARIANT"
OTHER_ITEM = "_PNXT_FFSP_OTHER"
CHILD_ITEM = "_PNXT_FFSP_CHILD_ITEM"
BRAND = "_PNXT_FFSP_BRAND"
PROMO_GROUP = "_PNXT_FFSP_PROMO_GROUP"
OTHER_GROUP = "_PNXT_FFSP_OTHER_GROUP"
CHILD_GROUP = "_PNXT_FFSP_CHILD_GROUP"
RULE = "_PNXT_FFSP_RULE"
NOBODY = "ffsp-nobody@example.com"
ACCOUNTANT = "ffsp-accountant@example.com"
POS_USER = "ffsp-posuser@example.com"


def _clear_request_cache():
	if hasattr(frappe.local, "request_cache"):
		frappe.local.request_cache.clear()


class TestFFScopePerf(PaidInvoiceFixture, FrappeTestCase):
	def setUp(self):
		super().setUp()
		_clear_request_cache()
		self.addCleanup(frappe.set_user, "Administrator")
		if not frappe.db.exists("Brand", BRAND):
			frappe.get_doc({"doctype": "Brand", "brand": BRAND}).insert(ignore_permissions=True)
		root = (
			frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""}) or "All Item Groups"
		)
		for group in (PROMO_GROUP, OTHER_GROUP):
			self._group(group, root, is_group=1)
		for code in (TEMPLATE, VARIANT, OTHER_ITEM):
			self._item(code, OTHER_GROUP)
		frappe.db.set_value("Item", TEMPLATE, "brand", BRAND)
		frappe.db.set_value("Item", VARIANT, {"variant_of": TEMPLATE, "brand": None})

	def tearDown(self):
		super().tearDown()
		# Redis is not rolled back with the test transaction.
		ff_scope.clear_item_group_cache()
		_clear_request_cache()

	# ---- helpers ------------------------------------------------------------

	@staticmethod
	def _group(name, parent, is_group=0):
		if not frappe.db.exists("Item Group", name):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": name,
					"parent_item_group": parent,
					"is_group": is_group,
				}
			).insert(ignore_permissions=True)

	@staticmethod
	def _item(code, group):
		if not frappe.db.exists("Item", code):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": group,
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			)
			item.flags.from_integration = True
			item.insert(ignore_permissions=True)

	def _compute(self, *lines):
		return compute_friends_and_family_discounts(
			customer=self.ctx.customer,
			items=[frappe._dict(line) for line in lines],
			pos_profile=self.ctx.pos_profile,
		)

	@staticmethod
	def _cart_line(item_code, **extra):
		return {"item_code": item_code, "qty": 1, "price_list_rate": 100, "rate": 100, **extra}

	@staticmethod
	def _user(email, roles=()):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		if roles:
			frappe.get_doc("User", email).add_roles(*roles)
		return email

	# ---- scope: variants follow their template --------------------------------

	def test_variant_matches_template_scope(self):
		self._settings(discount_scope="Item Code", applicable_items=[{"item_code": TEMPLATE}])
		payload = self._compute(self._cart_line(VARIANT), self._cart_line(OTHER_ITEM))
		variant, other = payload["items"]
		self.assertTrue(variant["applies"])
		self.assertEqual(flt(variant["discount_percentage"]), 30)
		self.assertFalse(other["applies"])
		self.assertEqual(other["reason"], REASON_OUT_OF_SCOPE)

	def test_variant_excluded_via_template_exclusion(self):
		self._settings(excluded_items=[{"item_code": TEMPLATE}])
		payload = self._compute(self._cart_line(VARIANT), self._cart_line(OTHER_ITEM))
		variant, other = payload["items"]
		self.assertFalse(variant["applies"])
		self.assertEqual(variant["reason"], REASON_EXCLUDED)
		self.assertTrue(other["applies"])

	def test_variant_takes_template_brand_when_blank(self):
		self._settings(discount_scope="Brand", applicable_brands=[{"brand": BRAND}])
		payload = self._compute(self._cart_line(VARIANT), self._cart_line(OTHER_ITEM))
		self.assertTrue(payload["items"][0]["applies"])
		self.assertFalse(payload["items"][1]["applies"])
		# The stored invoice row keeps the variant's own (blank) brand.
		row = self._row(ITEM_A)
		row.update({"item_code": VARIANT})
		doc = self._invoice([row], paid=False)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(row.discount_percentage), 30)
		self.assertIsNone(row.brand)

	def test_template_brand_not_sent_to_erpnext_pricing(self):
		# ERPNext matches a Brand rule on the variant's own brand: blank, so no discount.
		self._settings(enabled=0)
		rule = _make_rule(
			"_PNXT_FFSP_BRAND10",
			apply_on="Brand",
			brands=[{"brand": BRAND}],
			price_or_product_discount="Price",
			rate_or_discount="Discount Percentage",
			discount_percentage=10,
		)
		lines = [_line(self.ctx, ITEM_A, price_list_rate=100) for _ in range(2)]
		lines[0]["item_code"], lines[1]["item_code"] = TEMPLATE, VARIANT
		resp = apply_offers(
			invoice_data=json.dumps(_cart_payload(self.ctx, lines)), selected_offers=json.dumps([rule])
		)
		by_code = {row["item_code"]: row for row in resp["items"]}
		self.assertEqual(flt(by_code[TEMPLATE].get("discount_percentage")), 10)
		self.assertEqual(flt(by_code[VARIANT].get("discount_percentage")), 0)
		self.assertIsNone(by_code[VARIANT].get("brand"))

	def test_scope_fields_from_item_not_client(self):
		self._settings(discount_scope="Item Group", applicable_item_groups=[{"item_group": PROMO_GROUP}])
		forged = {"item_group": PROMO_GROUP, "brand": BRAND, "variant_of": None}
		payload = self._compute(self._cart_line(OTHER_ITEM, **forged))
		self.assertFalse(payload["items"][0]["applies"])
		self.assertEqual(payload["items"][0]["reason"], REASON_OUT_OF_SCOPE)

		# The invoice overwrites the forged group too, and the template a client hid.
		self._settings(excluded_items=[{"item_code": TEMPLATE}])
		row = self._row(ITEM_A)
		row.update({"item_code": VARIANT, "item_group": PROMO_GROUP, "variant_of": None})
		doc = self._invoice([row], paid=False)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(row.item_group, OTHER_GROUP)
		self.assertEqual(row.variant_of, TEMPLATE)
		self.assertEqual(flt(row.discount_percentage), 0)

	def test_apply_offers_overwrites_client_scope_fields(self):
		self._settings(discount_scope="Item Group", applicable_item_groups=[{"item_group": PROMO_GROUP}])
		line = _line(self.ctx, ITEM_A)
		line.update({"item_group": PROMO_GROUP, "brand": BRAND})
		resp = apply_offers(invoice_data=json.dumps(_cart_payload(self.ctx, [line])))
		row = resp["items"][0]
		self.assertNotEqual(row.get("item_group"), PROMO_GROUP)
		self.assertIsNone(row.get("brand"))
		self.assertEqual(flt(row.get("discount_percentage")), 0)

	def test_item_group_cache_cleared_on_update(self):
		self._settings(discount_scope="Item Group", applicable_item_groups=[{"item_group": PROMO_GROUP}])
		# Warm the program and the expanded tree (Redis, 300 s).
		self.assertFalse(self._compute(self._cart_line(OTHER_ITEM))["items"][0]["applies"])
		frappe.local.cache = {}  # a later request: only Redis remembers the tree
		self._group(CHILD_GROUP, PROMO_GROUP)
		self._item(CHILD_ITEM, CHILD_GROUP)
		self.assertTrue(self._compute(self._cart_line(CHILD_ITEM))["items"][0]["applies"])

	def test_item_group_cache_cleared_again_after_commit_and_on_rename(self):
		hooks = frappe.get_hooks("doc_events")["Item Group"]
		self.assertIn("posnext_promotions.promotions.ff_scope.clear_item_group_cache", hooks["after_rename"])
		with patch.object(frappe.db, "after_commit") as after_commit:
			ff_scope.clear_item_group_cache()
		after_commit.add.assert_called_once_with(ff_scope._delete_item_group_trees)
		key = f"item_group_descendants:{PROMO_GROUP}"
		frappe.cache().set_value(key, [PROMO_GROUP], expires_in_sec=300)
		after_commit.add.call_args.args[0]()  # what the commit runs
		self.assertIsNone(frappe.cache().get_value(key, expires=True))

	# ---- fewer queries ----------------------------------------------------------

	def test_pricing_rule_flags_use_cached_value(self):
		rule = ensure_named_pricing_rule(RULE)
		kind = ff_classifier.classify_promotion(rule)
		self.assertTrue(promotion_competes_with_ff({"pricing_rules": rule}))  # warms the document cache
		boom = AssertionError("database read")
		with (
			patch("frappe.get_doc", side_effect=boom),
			patch.object(frappe.db, "get_value", side_effect=boom),
		):
			for _ in range(3):
				self.assertEqual(ff_classifier.classify_promotion(rule), kind)
				self.assertTrue(promotion_competes_with_ff({"pricing_rules": rule}))

	def test_eligibility_computed_once_per_validate(self):
		rows = [self._row(ITEM_A if i % 2 else ITEM_B) for i in range(10)]
		doc = self._invoice(rows, paid=False)
		_clear_request_cache()
		spy = patch.object(
			eligibility_module, "_compute_eligibility", wraps=eligibility_module._compute_eligibility
		)
		with spy as compute:
			apply_friends_and_family_on_invoice(doc)
		self.assertEqual(compute.call_count, 1)
		self.assertTrue(all(flt(r.discount_percentage) == 30 for r in rows))

	def test_profile_allows_ff_once_per_request(self):
		if not frappe.get_meta("POS Profile").has_field(PROFILE_ALLOW_FIELD):
			self.skipTest("POS Profile.allow_friends_family missing")
		real = frappe.db.get_value
		calls = []

		def spy(doctype, *args, **kwargs):
			if doctype == "POS Profile" and PROFILE_ALLOW_FIELD in args:
				calls.append(args)
			return real(doctype, *args, **kwargs)

		_clear_request_cache()
		with patch.object(frappe.db, "get_value", side_effect=spy):
			for _ in range(3):
				self.assertTrue(self._compute(self._cart_line(OTHER_ITEM))["items"][0]["applies"])
		self.assertEqual(len(calls), 1)

	def test_monthly_usage_once_per_request_until_ledger_writes(self):
		self._cap_with_remaining(1000)
		_clear_request_cache()
		with patch.object(ff_ledger, "get_usage", wraps=ff_ledger.get_usage) as usage:
			for _ in range(3):
				self._compute(self._cart_line(OTHER_ITEM))
			self.assertEqual(usage.call_count, 1)
			ff_ledger._forget_usage()  # what a counter write does
			self._compute(self._cart_line(OTHER_ITEM))
			self.assertEqual(usage.call_count, 2)

	def test_log_rows_bulk_inserted(self):
		rows = [self._row(ITEM_A) for _ in range(5)]
		doc = self._invoice(rows)
		doc.flags[ff_paid.DISAGREEMENTS_FLAG] = {
			"offline": False,
			"entries": [
				{"invoice_row": row.name, "idx": row.idx, "reason": ff_paid.OVER_DISCOUNT, "carried": 5}
				for row in rows
			],
		}
		with patch.object(frappe.db, "bulk_insert", wraps=frappe.db.bulk_insert) as bulk:
			log_invoice_submit(doc)
		self.assertEqual(bulk.call_count, 1)
		logs = frappe.get_all(
			LOG_DOCTYPE,
			filters={"reference_name": doc.name},
			fields=["name", "item_code", "reason", "owner", "creation", "modified", "log_type"],
		)
		self.assertEqual(len(logs), 5)
		self.assertEqual(len({log.name for log in logs}), 5)
		self.assertTrue(all(log.name.startswith("FFL-") for log in logs))
		self.assertTrue(all(log.owner and log.creation and log.modified for log in logs))
		self.assertEqual({log.reason for log in logs}, {ff_paid.OVER_DISCOUNT})
		# A later single insert continues the same series.
		from posnext_promotions.promotions.friends_and_family_log import write_log

		single = write_log("Excluded", reference_doctype=doc.doctype, reference_name=doc.name)
		self.assertNotIn(single.name, {log.name for log in logs})

	# ---- access -------------------------------------------------------------------

	def test_apply_offers_requires_customer_read(self):
		self._user(NOBODY)
		payload = json.dumps(_cart_payload(self.ctx, [_line(self.ctx, ITEM_A)]))
		frappe.set_user(NOBODY)
		self.assertFalse(frappe.has_permission("Customer", "read", doc=self.ctx.customer))
		with self.assertRaises(frappe.PermissionError):
			apply_offers(invoice_data=payload)

	def test_ff_program_payload_requires_seller(self):
		self._user(NOBODY)
		self._user(ACCOUNTANT, ["Accounts User"])
		self._user(POS_USER)
		frappe.get_doc(
			{
				"doctype": "POS Profile User",
				"parent": self.ctx.pos_profile,
				"parenttype": "POS Profile",
				"parentfield": "applicable_for_users",
				"user": POS_USER,
			}
		).db_insert()

		frappe.set_user(NOBODY)
		self.assertFalse(frappe.has_permission("Sales Invoice", "create"))
		with self.assertRaises(frappe.PermissionError):
			get_friends_and_family_program()

		frappe.set_user(ACCOUNTANT)
		self.assertTrue(frappe.has_permission("Sales Invoice", "create"))
		self.assertTrue(get_friends_and_family_program()["active"])

		frappe.set_user(POS_USER)
		self.assertFalse(frappe.has_permission("Sales Invoice", "create"))
		self.assertTrue(get_friends_and_family_program()["active"])
		self.assertTrue(get_friends_and_family_program(pos_profile=self.ctx.pos_profile)["active"])
		# Only the requested profile counts, and only while it is enabled.
		with self.assertRaises(frappe.PermissionError):
			get_friends_and_family_program(pos_profile="_PNXT_FFSP_OTHER_PROFILE")
		frappe.set_user("Administrator")
		frappe.db.set_value("POS Profile", self.ctx.pos_profile, "disabled", 1)
		frappe.set_user(POS_USER)
		with self.assertRaises(frappe.PermissionError):
			get_friends_and_family_program()

	# ---- header discount ----------------------------------------------------------

	def test_coupon_header_discount_rounded_on_invoice(self):
		self._settings(discount_percentage=10)
		_make_pos_coupon("FFSP-GT11", discount_percentage=11, apply_on="Grand Total")
		row = self._row(ITEM_A)
		row.update({"price_list_rate": 100, "rate": 100, "discount_amount": 0})
		doc = self._invoice(
			[row],
			paid=False,
			coupon_code="FFSP-GT11",
			discount_amount=12.1,
			taxes=[frappe._dict(t) for t in _exclusive_tax()],
		)
		apply_friends_and_family_on_invoice(doc)
		self.assertEqual(flt(row.discount_percentage), 0)  # the coupon won
		self.assertEqual(doc.discount_amount, 12.1)  # not 12.100000000000001

	# ---- schema -------------------------------------------------------------------

	def test_period_key_index_kept_by_schema_sync(self):
		from frappe.database.mariadb.schema import MariaDBTable

		table_name = f"tab{ff_ledger.LEDGER_DOCTYPE}"
		self.assertTrue(frappe.db.has_index(table_name, ff_ledger.PERIOD_ONLY_INDEX))
		# Without search_index on the DocField, Frappe's sync drops the index it sees on the column.
		self.assertTrue(frappe.get_meta(ff_ledger.LEDGER_DOCTYPE).get_field("period_key").search_index)
		table = MariaDBTable(ff_ledger.LEDGER_DOCTYPE)
		# A fresh install's CREATE TABLE names it after the column (renamed by the ledger).
		self.assertIn("index `period_key`(`period_key`)", table.get_index_definitions())
		table.validate()
		with patch.object(frappe.db, "sql_ddl") as ddl:
			table.sync()
		statements = " ".join(str(call.args[0]) for call in ddl.call_args_list)
		self.assertNotIn(ff_ledger.PERIOD_ONLY_INDEX, statements)

	def test_search_index_named_the_same_on_fresh_and_altered_tables(self):
		table = f"tab{ff_ledger.LEDGER_DOCTYPE}"
		cases = (
			# (has <column>_index, has <column> (CREATE name), expected DDL)
			(True, False, None),
			(False, True, f"alter table `{table}` rename index `period_key` to `period_key_index`"),
			(False, False, f"alter table `{table}` add index `period_key_index` (`period_key`)"),
		)
		for has_altered, has_created, expected in cases:
			with self.subTest(has_altered=has_altered, has_created=has_created):
				names = {"period_key_index": has_altered, "period_key": has_created}
				with (
					patch.object(frappe.db, "has_index", side_effect=lambda _t, name: names[name]),
					patch.object(frappe.db, "sql_ddl") as ddl,
				):
					ff_ledger._ensure_search_index(table, "period_key")
				self.assertEqual([c.args[0] for c in ddl.call_args_list], [expected] if expected else [])
