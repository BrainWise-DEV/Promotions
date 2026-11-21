"""Version gate for the Friends & Family contract with pos_next.

Runs as a ``before_migrate`` hook, so it is enforced on every migrate. pos_next gates
the other way (``pos_next.integrations.contract_gate``): a new pos_next refuses an old
posnext_promotions, and this refuses an old pos_next. ``check`` is a deliberate copy of
pos_next's, so the gate works whatever pos_next is installed (and without importing it).
"""

import importlib

import frappe

# app -> (minimum FF_CONTRACT, release that provides it)
REQUIREMENTS = {"pos_next": (2, "2.6.0")}


def check(app_requirements, installed_apps, importer=importlib.import_module):
	messages = []
	for app, (required, release) in app_requirements.items():
		if app not in installed_apps:
			continue
		have = getattr(importer(app), "FF_CONTRACT", None)
		if not isinstance(have, int) or isinstance(have, bool) or have < required:
			messages.append(f"Update {app} to >= {release} before migrating (an older version is installed).")
	return messages


def before_migrate(importer=importlib.import_module):
	messages = check(REQUIREMENTS, frappe.get_installed_apps(), importer)
	if messages:
		frappe.throw("\n".join(messages), title="Update required")
