# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Dev-site test runner.

`bench run-tests` wipes dev data, so run a test module with:

	bench --site <site> execute posnext_promotions.dev_tests.run --kwargs '{"module": "<dotted.module>"}'
"""

import importlib
import unittest


def run(module: str) -> dict:
	"""Run every test in ``module`` via unittest and return a summary dict."""
	suite = unittest.TestLoader().loadTestsFromModule(importlib.import_module(module))
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"tests_run": result.testsRun,
		"failures": [str(f[0]) for f in result.failures],
		"errors": [str(e[0]) for e in result.errors],
		"skipped": [f"{t}: {reason}" for t, reason in result.skipped],
		"was_successful": result.wasSuccessful(),
	}


def run_ci(modules: list[str]) -> dict:
	"""Run ``modules`` one by one; raise ``AssertionError`` naming the failed ones (so bench exits non-zero).

	CI entry point:

		bench --site test_site execute posnext_promotions.dev_tests.run_ci --kwargs '{"modules": ["a.b", "c.d"]}'
	"""
	if not modules:
		raise AssertionError("run_ci was given no test modules")
	results, failed = {}, []
	for module in modules:
		try:
			results[module] = run(module)
		except Exception as e:  # an import error must fail this module, not stop the others
			results[module] = {"tests_run": 0, "failures": [], "errors": [repr(e)], "skipped": [], "was_successful": False}
		r = results[module]
		if r["skipped"]:
			print(f"SKIPPED in {module}: {len(r['skipped'])} of {r['tests_run']}: " + "; ".join(r["skipped"]))
		# A module that ran nothing, or skipped every test, proves nothing.
		if not r["was_successful"] or r["tests_run"] - len(r["skipped"]) <= 0:
			failed.append(module)
	if failed:
		raise AssertionError(f"{len(failed)} of {len(modules)} test modules failed or ran no tests: " + ", ".join(failed))
	return results
