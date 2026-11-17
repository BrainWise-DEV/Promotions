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
		"was_successful": result.wasSuccessful(),
	}
