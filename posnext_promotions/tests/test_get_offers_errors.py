# Copyright (c) 2026, BrainWise and contributors
"""D-R4-01 review I-2 (promotions half): a failed offer fetch is never answered as "no offers".

The POS clears its offline offer cache when ``get_offers`` answers an empty list and
keeps it when the request fails. ``get_offers`` caught every error, logged it and
answered ``[]``, so one lock timeout or bad Pricing Rule wiped every till's offline
offers. It now logs and re-raises; a real empty result is still ``[]``.

Run:
	PYTHONPATH=<worktree> bench --site nexus.local execute pos_next.dev_tests.run \
		--kwargs "{'module': 'posnext_promotions.tests.test_get_offers_errors'}"
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from posnext_promotions.api import offers
from posnext_promotions.tests.test_offline_coupon_overshoot import _shared_ctx

MARK = "_PNXT_K1_FORCED_OFFERS_ERROR"


def _delete_marked_error_logs():
	for name in frappe.get_all("Error Log", filters={"method": ["like", f"%{MARK}%"]}, pluck="name"):
		frappe.delete_doc("Error Log", name, force=True, ignore_permissions=True)


class TestGetOffersErrors(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ctx = _shared_ctx()
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()
		_delete_marked_error_logs()
		frappe.db.commit()

	def test_internal_error_is_raised_not_answered_as_no_offers(self):
		with (
			patch.object(offers, "_get_promotional_scheme_offers", side_effect=frappe.QueryTimeoutError(MARK)),
			self.assertRaises(frappe.QueryTimeoutError),
		):
			offers.get_offers(self.ctx.pos_profile)
		# Still logged under "Offers API" for the support desk.
		self.assertTrue(frappe.get_all("Error Log", filters={"method": ["like", f"%{MARK}%"]}, limit=1))

	def test_real_empty_result_is_still_an_empty_list(self):
		with (
			patch.object(offers, "_get_promotional_scheme_offers", return_value=[]),
			patch.object(offers, "_get_standalone_pricing_rule_offers", return_value=[]),
		):
			self.assertEqual(offers.get_offers(self.ctx.pos_profile), [])
