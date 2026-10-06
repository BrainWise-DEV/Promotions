// Copyright (c) 2026, BrainWise and contributors
// For license information, please see license.txt

frappe.ui.form.on("Friends and Family Settings", {
	refresh(frm) {
		// PN-131: quick access to the F&F audit trail and this form's change history.
		if (frappe.model.can_read("Friends and Family Log")) {
			frm.add_custom_button(
				__("Audit Log"),
				() => frappe.set_route("List", "Friends and Family Log"),
				__("View")
			);
			frm.add_custom_button(
				__("Settings Changes"),
				() =>
					frappe.set_route("List", "Friends and Family Log", {
						log_type: "Settings Changed",
					}),
				__("View")
			);
		}
		if (frappe.model.can_read("Version")) {
			frm.add_custom_button(
				__("Version History"),
				() =>
					frappe.set_route("List", "Version", {
						ref_doctype: frm.doctype,
						docname: frm.doc.name,
					}),
				__("View")
			);
		}
	},
});
