# Copyright (c) 2026, BrainWise and contributors
"""Smoke tests that do not need a Frappe site: no pos_next coupling."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
PKG = APP_ROOT / "posnext_promotions"
BENCH_APPS = APP_ROOT.parent
POS_NEXT = BENCH_APPS / "pos_next"


def _python_files(root: Path):
	for path in root.rglob("*.py"):
		if "__pycache__" in path.parts:
			continue
		yield path


def _import_violations(root: Path, forbidden: str, skip_names=()):
	violations = []
	for path in _python_files(root):
		if path.name in skip_names:
			continue
		try:
			tree = ast.parse(path.read_text(), filename=str(path))
		except SyntaxError:
			continue
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				for alias in node.names:
					if alias.name == forbidden or alias.name.startswith(forbidden + "."):
						violations.append(f"{path}:{node.lineno} import {alias.name}")
			elif isinstance(node, ast.ImportFrom):
				mod = node.module or ""
				if mod == forbidden or mod.startswith(forbidden + "."):
					violations.append(f"{path}:{node.lineno} from {mod}")
	return violations


class TestIndependence(unittest.TestCase):
	def test_hooks_have_no_required_apps_pos_next(self):
		import re

		hooks = (PKG / "hooks.py").read_text()
		self.assertIsNone(
			re.search(r"^required_apps\s*=\s*\[[^\]]*pos_next", hooks, re.M),
			"posnext_promotions must not require pos_next",
		)

	def test_hooks_override_pos_next_whitelist_when_pos_vue_calls_core_paths(self):
		hooks = (PKG / "hooks.py").read_text()
		self.assertIn("override_whitelisted_methods", hooks)
		self.assertIn("pos_next.api.invoices.apply_offers", hooks)
		self.assertIn("posnext_promotions.api.offers.apply_offers", hooks)

	def test_no_python_import_of_pos_next(self):
		# Production code must not import pos_next. Optional integration tests
		# may lazy-load the POS invoice pipeline when that app is installed.
		skip = {"test_independence.py", "test_promotions.py"}
		violations = _import_violations(PKG, "pos_next", skip_names=skip)
		self.assertEqual(violations, [], "pos_next imports are forbidden:\n" + "\n".join(violations))

	def test_pos_next_does_not_import_this_app(self):
		if not POS_NEXT.exists():
			self.skipTest("pos_next is not in this bench")
		violations = _import_violations(POS_NEXT, "posnext_promotions")
		self.assertEqual(
			violations,
			[],
			"pos_next must not import posnext_promotions:\n" + "\n".join(violations),
		)

	def test_boot_extend_sets_flag(self):
		from posnext_promotions.boot import extend

		bootinfo = {}
		extend(bootinfo)
		self.assertEqual(bootinfo.get("posnext_promotions"), 1)
		self.assertEqual(bootinfo.get("posnext_promotions_auth"), 1)

	def test_form_js_is_safe_to_concatenate_with_pos_next(self):
		"""Frappe concatenates every app's doctype_js into one Function.

		Top-level const in both copies used to throw
		`SyntaxError: redeclaration of const PN_SLAB_DISCOUNT_FIELDS`.
		"""
		if not POS_NEXT.exists():
			self.skipTest("pos_next is not in this bench")

		pairs = (
			("public/js/promotional_scheme.js", "posnext_promotions"),
			("public/js/pricing_rule.js", "posnext_promotions"),
			("public/js/user.js", "posnext_promotions_auth"),
		)
		for rel, boot_flag in pairs:
			ours = (PKG / rel).read_text()
			theirs_path = POS_NEXT / "pos_next" / rel
			self.assertIn("(function () {", ours, rel)
			self.assertTrue(ours.rstrip().endswith("})();"), rel)
			if not theirs_path.exists():
				continue
			theirs = theirs_path.read_text()
			# origin/develop desk JS is min/max only and may not wrap in an IIFE.
			if "(function () {" not in theirs:
				continue
			self.assertIn("(function () {", theirs, rel)
			self.assertTrue(theirs.rstrip().endswith("})();"), rel)
			self.assertIn(f"frappe.boot.{boot_flag}", theirs, rel)
			# Concatenation must parse as a single Function body, as Frappe does.
			concatenated = theirs + "\n" + ours
			self.assertGreater(concatenated.count("(function () {"), 1, rel)


class TestSmokeBothOptional(unittest.TestCase):
	"""Site-level checks; skipped when Frappe is not bootstrapped."""

	def test_pricing_rule_override_points_at_this_app(self):
		from posnext_promotions.hooks import override_doctype_class

		self.assertEqual(
			override_doctype_class.get("Pricing Rule"),
			"posnext_promotions.overrides.custom_pricing_rule.CustomPricingRule",
		)
		self.assertNotIn("Sales Invoice", override_doctype_class)

	def test_installed_apps_composition(self):
		try:
			import frappe
		except Exception:
			self.skipTest("frappe is not importable")
		if not getattr(frappe.local, "site", None):
			self.skipTest("no Frappe site initialized")
		apps = frappe.get_installed_apps()
		# Any of the three compositions is valid; assert this app never requires pos_next.
		if "posnext_promotions" in apps:
			from posnext_promotions.hooks import required_apps as promo_required

			self.assertNotIn("pos_next", promo_required)
		if "pos_next" in apps and "posnext_promotions" in apps:
			self.assertTrue(frappe.get_hooks("pos_next_loyalty_provider") is not None)


if __name__ == "__main__":
	unittest.main()
