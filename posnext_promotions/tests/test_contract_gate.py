# Copyright (c) 2026, BrainWise and contributors
"""posnext_promotions refuses to migrate next to a pos_next older than its contract (G5)."""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

import posnext_promotions
from posnext_promotions import contract_gate

GATE = "posnext_promotions.contract_gate.before_migrate"
MSG = "Update pos_next to >= 2.7.0 before migrating (an older version is installed)."


def _importer(**mods):
	def importer(name):
		if name not in mods:
			raise ImportError(name)
		return mods[name]

	return importer


class TestContractGate(FrappeTestCase):
	def test_contract_is_5(self):
		# 5: a coupon that holds the whole cart answers replaces_friends_and_family_on_cart,
		# and pos_next 3 strips F&F from every line then (K1).
		self.assertEqual(posnext_promotions.FF_CONTRACT, 5)
		self.assertEqual(posnext_promotions.__version__, "0.5.0")
		self.assertEqual(contract_gate.REQUIREMENTS, {"pos_next": (3, "2.7.0")})

	def test_old_pos_next_reported(self):
		for module in (SimpleNamespace(), SimpleNamespace(FF_CONTRACT=2), SimpleNamespace(FF_CONTRACT="3")):
			with self.subTest(module=module):
				msgs = contract_gate.check(contract_gate.REQUIREMENTS, ["pos_next"], _importer(pos_next=module))
				self.assertEqual(msgs, [MSG])

	def test_current_pos_next_or_none_passes(self):
		imp = _importer(pos_next=SimpleNamespace(FF_CONTRACT=3))
		self.assertEqual(contract_gate.check(contract_gate.REQUIREMENTS, ["pos_next"], imp), [])
		self.assertEqual(contract_gate.check(contract_gate.REQUIREMENTS, ["frappe"], _importer()), [])

	def test_hook_throws_when_pos_next_old(self):
		with patch("frappe.get_installed_apps", return_value=["frappe", "pos_next"]):
			with self.assertRaises(frappe.ValidationError) as cm:
				contract_gate.before_migrate(importer=_importer(pos_next=SimpleNamespace(FF_CONTRACT=1)))
		self.assertEqual(str(cm.exception), MSG)

	def test_gate_registered_in_before_migrate_hooks(self):
		self.assertIn(GATE, frappe.get_hooks("before_migrate", app_name="posnext_promotions"))
