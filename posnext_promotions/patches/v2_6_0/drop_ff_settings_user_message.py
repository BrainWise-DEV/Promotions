"""Drop the stored value of the removed Friends and Family Settings.user_message.

The field was replaced by Friends and Family Message (event ``ff_activated``).
Singles keep orphan rows after a field is removed, so delete it. Idempotent.
"""

import frappe


def execute():
	frappe.db.delete("Singles", {"doctype": "Friends and Family Settings", "field": "user_message"})
