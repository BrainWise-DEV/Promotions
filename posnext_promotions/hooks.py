app_name = "posnext_promotions"
app_title = "POSNext Promotions"
app_publisher = "BrainWise"
app_description = "Standalone promotions engine, GWP, coupons extras, and POS authorization gate for ERPNext"
app_email = "support@brainwise.me"
app_license = "agpl-3.0"

# Independent of pos_next — do not declare it in required_apps.
required_apps = ["erpnext"]

# Vanilla develop Vue calls pos_next.api.*; Frappe routes those to this app when installed.
override_whitelisted_methods = {
	"pos_next.api.invoices.apply_offers": "posnext_promotions.api.offers.apply_offers",
	"pos_next.api.offers.get_offers": "posnext_promotions.api.offers.get_offers",
	"pos_next.api.offers.validate_coupon": "posnext_promotions.api.offers.validate_coupon",
	"pos_next.api.offers.get_active_coupons": "posnext_promotions.api.offers.get_active_coupons",
	"pos_next.api.offers.calculate_coupon_discount": "posnext_promotions.api.offers.calculate_coupon_discount",
	"pos_next.api.offers.item_has_active_promotion": "posnext_promotions.api.offers.item_has_active_promotion",
	"pos_next.api.offers.get_customer_one_time_redemptions": "posnext_promotions.api.offers.get_customer_one_time_redemptions",
	"pos_next.api.promotions.get_promotions": "posnext_promotions.api.promotions.get_promotions",
	"pos_next.api.promotions.get_promotion_details": "posnext_promotions.api.promotions.get_promotion_details",
	"pos_next.api.promotions.create_promotion": "posnext_promotions.api.promotions.create_promotion",
	"pos_next.api.promotions.update_promotion": "posnext_promotions.api.promotions.update_promotion",
	"pos_next.api.promotions.toggle_promotion": "posnext_promotions.api.promotions.toggle_promotion",
	"pos_next.api.promotions.delete_promotion": "posnext_promotions.api.promotions.delete_promotion",
	"pos_next.api.promotions.get_item_groups": "posnext_promotions.api.promotions.get_item_groups",
	"pos_next.api.promotions.get_brands": "posnext_promotions.api.promotions.get_brands",
	"pos_next.api.promotions.get_coupons": "posnext_promotions.api.promotions.get_coupons",
	"pos_next.api.promotions.get_coupon_details": "posnext_promotions.api.promotions.get_coupon_details",
	"pos_next.api.promotions.create_coupon": "posnext_promotions.api.promotions.create_coupon",
	"pos_next.api.promotions.update_coupon": "posnext_promotions.api.promotions.update_coupon",
	"pos_next.api.promotions.toggle_coupon": "posnext_promotions.api.promotions.toggle_coupon",
	"pos_next.api.promotions.delete_coupon": "posnext_promotions.api.promotions.delete_coupon",
	"pos_next.api.gift_pool.gift_pool_item_query": "posnext_promotions.api.gift_pool.gift_pool_item_query",
}

extend_bootinfo = "posnext_promotions.boot.extend"

override_doctype_class = {
	"Pricing Rule": "posnext_promotions.overrides.custom_pricing_rule.CustomPricingRule",
}

doctype_js = {
	"Pricing Rule": "public/js/pricing_rule.js",
	"Promotional Scheme": "public/js/promotional_scheme.js",
	"User": "public/js/user.js",
}

doc_events = {
	"Promotional Scheme": {
		"before_validate": [
			"posnext_promotions.promotions.schedule.normalize_schedule_fields",
			"posnext_promotions.overrides.pricing_rule.normalize_accumulative_scheme",
			"posnext_promotions.overrides.pricing_rule.normalize_gift_pool_scheme",
		],
		"validate": [
			"posnext_promotions.overrides.pricing_rule.enforce_cross_cart_pricing_config",
			"posnext_promotions.overrides.pricing_rule.validate_unique_promotion_type_per_item",
			"posnext_promotions.overrides.pricing_rule.validate_gift_pool_scheme",
		],
		"on_update": "posnext_promotions.overrides.pricing_rule.sync_promotion_fields_to_pricing_rules",
	},
	"Pricing Rule": {
		"before_validate": "posnext_promotions.promotions.schedule.normalize_schedule_fields",
		"validate": "posnext_promotions.overrides.pricing_rule.enforce_cross_cart_pricing_config",
	},
	"Sales Invoice": {
		"validate": [
			"posnext_promotions.overrides.sales_invoice_free_bundle.combine_packed_qty_for_free_product_bundles",
			"posnext_promotions.overrides.pricing_rule.apply_min_max_price_discounts",
		],
		"before_submit": "posnext_promotions.authorization.gate.enforce_document",
		"on_submit": "posnext_promotions.api.one_time_usage.record_one_time_offer_usage",
		"on_cancel": "posnext_promotions.api.one_time_usage.release_one_time_offer_usage",
	},
	"Sales Order": {"validate": "posnext_promotions.overrides.pricing_rule.apply_min_max_price_discounts"},
	"Quotation": {"validate": "posnext_promotions.overrides.pricing_rule.apply_min_max_price_discounts"},
	"Delivery Note": {"validate": "posnext_promotions.overrides.pricing_rule.apply_min_max_price_discounts"},
	"POS Invoice": {"validate": "posnext_promotions.overrides.pricing_rule.apply_min_max_price_discounts"},
}

# Daily cleanup stays on pos_next develop so it does not run twice when both apps
# are installed. Keep the task module for sites that call it explicitly.

before_install = "posnext_promotions.install.before_install"
after_migrate = "posnext_promotions.install.after_migrate"
