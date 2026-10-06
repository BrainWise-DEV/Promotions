// Copyright (c) 2026, BrainWise and contributors
// For license information, please see license.txt

frappe.listview_settings["Friends and Family Log"] = {
	add_fields: ["log_type", "is_cancelled", "capped"],
	get_indicator(doc) {
		if (doc.is_cancelled) return [__("Cancelled"), "gray", "is_cancelled,=,1"];
		const colors = {
			"Discount Applied": "green",
			"Comparison Lost": "orange",
			Excluded: "gray",
			"Cap Hit": "red",
			"Order Comparison": "blue",
			"Eligibility Applied": "green",
			"Eligibility Removed": "red",
			"Discount Reversed": "red",
			"Discount Returned": "orange",
			"Settings Changed": "purple",
		};
		return [__(doc.log_type), colors[doc.log_type] || "gray", `log_type,=,${doc.log_type}`];
	},
};
