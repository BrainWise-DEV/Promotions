# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, get_datetime, nowdate

from posnext_promotions.promotions import friends_and_family_apply as ff_apply
from posnext_promotions.promotions.friends_and_family_eligibility import (
	benefit_provider,
	ensure_customer_flag_field,
)
from posnext_promotions.promotions.friends_and_family_log import LOG_DOCTYPE
from posnext_promotions.promotions.friends_and_family_testing import (
	approved_domain_rows,
	ff_settings_guard,
	give_verified_email,
)

from posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_ff_paid_freeze import (
	ITEM_A,
	ITEM_PRICES,
	PaidInvoiceFixture,
)

HOOK_PATH = "posnext_promotions.promotions.friends_and_family_eligibility.benefit_provider"
CUSTOMER = "_Test FF Provider Customer"
DOMAIN = "acme.example"


class TestFFBenefitProvider(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_customer_flag_field()
		if not frappe.db.exists("Customer", CUSTOMER):
			group = frappe.db.get_value("Customer Group", {"is_group": 0})
			frappe.get_doc({"doctype": "Customer", "customer_name": CUSTOMER, "customer_group": group}).insert()

	def test_benefit_provider_shape(self):
		with ff_settings_guard():
			settings = frappe.get_single("Friends and Family Settings")
			settings.update({"enabled": 1, "approved_domains": approved_domain_rows(DOMAIN)})
			settings.save(ignore_permissions=True)
			give_verified_email(CUSTOMER, domain=DOMAIN)
			provider = benefit_provider()
			self.assertEqual(set(provider), {"is_member", "on_verification_change", "fnf_line_rates"})
			self.assertTrue(provider["is_member"](CUSTOMER, pos_profile=None))

	def test_verification_change_logged_domain_only(self):
		address = f"staff@{DOMAIN}"
		before = frappe.db.count(LOG_DOCTYPE)
		benefit_provider()["on_verification_change"](CUSTOMER, True, DOMAIN, "otp")
		self.assertEqual(frappe.db.count(LOG_DOCTYPE), before + 1)
		row = frappe.get_all(
			LOG_DOCTYPE, filters={"customer": CUSTOMER}, fields=["*"], order_by="creation desc", limit=1
		)[0]
		blob = " ".join(str(v) for v in row.values())
		self.assertIn(DOMAIN, row["details"])
		self.assertNotIn("staff@", blob)
		self.assertNotIn(address, blob)

	def test_hook_declared(self):
		self.assertIn(HOOK_PATH, frappe.get_hooks("pos_next_customer_benefit_providers"))


class TestFFLineRates(PaidInvoiceFixture, FrappeTestCase):
	"""``fnf_line_rates`` (Ruling 120): the F&F rate POS Next may count as explained, computed
	on the server's data only, never from the row's mark or the invoice's offline keys."""

	def _rates(self, doc):
		return benefit_provider()["fnf_line_rates"](doc)

	def _forged(self, **header):
		"""A paid POS invoice whose row carries an F&F mark at 80% off."""
		return self._invoice([self._row(ITEM_A, pct=80, marked=True)], **header)

	def test_fnf_line_rates_ignores_offline_id(self):
		online = self._forged()
		offline = self._forged(offline=True, sync_flag=True)
		offline["items"][0].name = online["items"][0].name
		self.assertTrue(offline.get("offline_id"))
		expected = {online["items"][0].name: flt(ITEM_PRICES[ITEM_A] * 0.7, 2)}
		self.assertEqual({k: flt(v, 2) for k, v in self._rates(online).items()}, expected)
		self.assertEqual({k: flt(v, 2) for k, v in self._rates(offline).items()}, expected)

	def test_fnf_line_rates_ineligible_customer_empty(self):
		frappe.db.set_value(
			"Customer", self.ctx.customer, "custom_email_verified", 0, update_modified=False
		)
		self.assertEqual(self._rates(self._forged(offline=True)), {})

	def test_fnf_line_rates_not_pos_empty(self):
		self.assertEqual(self._rates(self._forged(doctype="Sales Invoice", is_pos=0)), {})

	def test_fnf_line_rates_never_judged_on_the_posting_date(self):
		"""A back-dated posting date inside an ended program window gives no F&F rate."""
		self._settings(start_date=add_days(nowdate(), -30), end_date=add_days(nowdate(), -10))
		self.assertEqual(self._rates(self._forged(posting_date=add_days(nowdate(), -20))), {})

	def test_fnf_line_rates_on_the_creation_day(self):
		"""A printed draft closed after the program ended keeps its sale day's F&F rate.

		The sale day is the ``creation`` stored in the database (Ruling 125), never the
		in-memory one: a forged ``creation`` on an unsaved doc counts for nothing.
		"""
		self._settings(start_date=add_days(nowdate(), -30), end_date=add_days(nowdate(), -1))
		stored = f"{add_days(nowdate(), -1)} 10:00:00"
		doc = self._forged(creation=stored)
		self.assertEqual(self._rates(doc), {})
		with patch.object(ff_apply, "_stored_creation", return_value=get_datetime(stored)):
			self.assertEqual(list(self._rates(doc).values()), [flt(ITEM_PRICES[ITEM_A] * 0.7)])

	def test_fnf_line_rates_uncapped(self):
		"""Caps move with other sales and belong to the ledger: the rate is the entitlement."""
		self._settings(max_discount_per_order=0.01)
		self.assertEqual(list(self._rates(self._forged()).values()), [flt(ITEM_PRICES[ITEM_A] * 0.7)])
