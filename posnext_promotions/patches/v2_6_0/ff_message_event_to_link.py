"""Friends and Family Message.event: Select -> Link to Friends and Family Message Event.

Creates the standard events, then a custom event for any other existing value so
every message keeps a valid link. Add-only.
"""

import frappe


def execute():
	from posnext_promotions.promotions import friends_and_family_messages as ffm

	if not frappe.db.table_exists(ffm.EVENT_DOCTYPE) or not frappe.db.table_exists(ffm.MESSAGE_DOCTYPE):
		return
	ffm.ensure_standard_events()
	for code in frappe.get_all(ffm.MESSAGE_DOCTYPE, pluck="event", distinct=True):
		if code and not frappe.db.exists(ffm.EVENT_DOCTYPE, code):
			frappe.get_doc(
				{"doctype": ffm.EVENT_DOCTYPE, "event_code": code, "event_name": code.replace("_", " ").title(), "enabled": 1}
			).insert(ignore_permissions=True)
	for name, code in frappe.get_all(ffm.MESSAGE_DOCTYPE, fields=["name", "event"], as_list=True):
		label = frappe.db.get_value(ffm.EVENT_DOCTYPE, code, "event_name")
		frappe.db.set_value(ffm.MESSAGE_DOCTYPE, name, "event_name", label, update_modified=False)
