// Copyright (c) 2026, BrainWise and contributors
// For license information, please see license.txt

frappe.query_reports["Friends and Family Performance"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["Summary", "Invoices", "Items", "Customers", "Comparisons"],
			default: "Summary",
			reqd: 1,
		},
		{
			fieldname: "periodicity",
			label: __("Periodicity"),
			fieldtype: "Select",
			options: ["Monthly", "Weekly", "Daily"],
			default: "Monthly",
			depends_on: "eval:doc.view == 'Summary'",
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{ fieldname: "pos_profile", label: __("POS Profile"), fieldtype: "Link", options: "POS Profile" },
		{ fieldname: "customer", label: __("Customer"), fieldtype: "Link", options: "Customer" },
		{ fieldname: "item_group", label: __("Item Group"), fieldtype: "Link", options: "Item Group" },
		{ fieldname: "brand", label: __("Brand"), fieldtype: "Link", options: "Brand" },
		{
			fieldname: "top_n",
			label: __("Top N Items"),
			fieldtype: "Int",
			default: 20,
			depends_on: "eval:doc.view == 'Items'",
		},
		{
			fieldname: "only_cap_hits",
			label: __("Only Customers Who Hit Caps"),
			fieldtype: "Check",
			depends_on: "eval:doc.view == 'Customers'",
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		return data && data.is_total_row ? `<b>${value}</b>` : value;
	},
};
