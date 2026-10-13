// Copyright (c) 2026, BrainWise and contributors
// For license information, please see license.txt

// Table field -> source doctype + link fieldname (PN-124 bulk pickers).
const FF_PICKER_TABLES = {
	applicable_brands: { doctype: "Brand", field: "brand", label: __("Brands") },
	excluded_brands: { doctype: "Brand", field: "brand", label: __("Brands") },
	applicable_item_groups: { doctype: "Item Group", field: "item_group", label: __("Item Groups") },
	excluded_item_groups: { doctype: "Item Group", field: "item_group", label: __("Item Groups") },
	applicable_items: { doctype: "Item", field: "item_code", label: __("Items") },
	excluded_items: { doctype: "Item", field: "item_code", label: __("Items") },
};
const FF_CONFIRM_THRESHOLD = 50;

function ff_existing_values(frm, table) {
	const field = FF_PICKER_TABLES[table].field;
	return new Set((frm.doc[table] || []).map((r) => r[field]).filter(Boolean));
}

function ff_append_values(frm, table, values) {
	const cfg = FF_PICKER_TABLES[table];
	const existing = ff_existing_values(frm, table);
	const fresh = [...new Set(values || [])].filter((v) => v && !existing.has(v));
	if (!fresh.length) {
		frappe.show_alert({ message: __("Nothing to add — all already listed"), indicator: "orange" });
		return;
	}
	const add = () => {
		// Drop a single trailing empty row the grid may have created.
		(frm.doc[table] || [])
			.filter((r) => !r[cfg.field])
			.forEach((r) => frappe.model.clear_doc(r.doctype, r.name));
		fresh.forEach((v) => {
			const row = frm.add_child(table);
			row[cfg.field] = v;
		});
		frm.refresh_field(table);
		frm.dirty();
		frappe.show_alert({
			message: __("Added {0} rows ({1} already listed)", [fresh.length, values.length - fresh.length]),
			indicator: "green",
		});
	};
	if (fresh.length > FF_CONFIRM_THRESHOLD) {
		frappe.confirm(__("This will add {0} rows to {1}. Continue?", [fresh.length, __(frappe.meta.get_label(frm.doctype, table))]), add);
	} else {
		add();
	}
}

function ff_get_all(frm, table) {
	const cfg = FF_PICKER_TABLES[table];
	const run = (leaf_only) =>
		frappe
			.call({
				method: "posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.friends_and_family_settings.get_all_for_table",
				args: { source_doctype: cfg.doctype, leaf_only: leaf_only ? 1 : 0 },
				freeze: true,
			})
			.then((r) => ff_append_values(frm, table, r.message || []));
	if (cfg.doctype === "Item Group") {
		frappe.prompt(
			[
				{
					fieldname: "leaf_only",
					fieldtype: "Check",
					label: __("Leaf groups only (child groups are already covered by their parent)"),
					default: 1,
				},
			],
			(v) => run(v.leaf_only),
			__("Get All Item Groups"),
			__("Get")
		);
	} else {
		run(true);
	}
}

function ff_select_multiple(frm, table) {
	const cfg = FF_PICKER_TABLES[table];
	const setters = {};
	let get_query = null;
	if (cfg.doctype === "Item") {
		setters.item_group = null;
		setters.brand = null;
		get_query = () => ({ filters: { disabled: 0, is_sales_item: 1, has_variants: 0 } });
	} else if (cfg.doctype === "Item Group") {
		setters.parent_item_group = null;
		setters.is_group = null;
	}
	new frappe.ui.form.MultiSelectDialog({
		doctype: cfg.doctype,
		target: frm,
		setters,
		get_query,
		primary_action_label: __("Add"),
		action(selections) {
			if (selections && selections.length) ff_append_values(frm, table, selections);
			this.dialog.hide();
		},
	});
}

function ff_setup_table_buttons(frm) {
	Object.entries(FF_PICKER_TABLES).forEach(([table, cfg]) => {
		const grid = frm.fields_dict[table] && frm.fields_dict[table].grid;
		if (!grid || grid.__ff_buttons) return;
		grid.__ff_buttons = true;
		grid.add_custom_button(__("Get All {0}", [cfg.label]), () => ff_get_all(frm, table));
		grid.add_custom_button(__("Select Multiple"), () => ff_select_multiple(frm, table));
	});
}

frappe.ui.form.on("Friends and Family Settings", {
	refresh(frm) {
		if (frm.has_perm("write")) ff_setup_table_buttons(frm);
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
		if (frappe.model.can_get_report("Friends and Family Log")) {
			// PN-132: program cost and usage.
			frm.add_custom_button(
				__("Performance Report"),
				() => frappe.set_route("query-report", "Friends and Family Performance"),
				__("View")
			);
		}
		if (frappe.model.can_read("Friends and Family Message")) {
			frm.add_custom_button(
				__("Customer Messages"),
				() => frappe.set_route("List", "Friends and Family Message"),
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
