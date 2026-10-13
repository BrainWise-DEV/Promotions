// Copyright (c) 2026, BrainWise and contributors
// For license information, please see license.txt

frappe.ui.form.on("Friends and Family Message Event", {
	refresh(frm) {
		if (frm.doc.is_standard) {
			frm.set_intro(__("Standard event fired by the Friends & Family engine."), "blue");
		}
		if (!frm.is_new()) {
			frm.add_custom_button(__("Messages"), () =>
				frappe.set_route("List", "Friends and Family Message", { event: frm.doc.name })
			);
		}
	},
});
