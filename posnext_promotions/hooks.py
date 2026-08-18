app_name = "posnext_promotions"
app_title = "POSNext Promotions"
app_publisher = "BrainWise"
app_description = "Standalone promotions engine, GWP, coupons extras, and POS authorization gate for ERPNext"
app_email = "support@brainwise.me"
app_license = "agpl-3.0"

# Independent of pos_next — do not declare it in required_apps.
# required_apps = []

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
		],
		"validate": [
			"posnext_promotions.overrides.pricing_rule.enforce_cross_cart_pricing_config",
			"posnext_promotions.overrides.pricing_rule.validate_unique_promotion_type_per_item",
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

scheduler_events = {
	"daily": [
		"posnext_promotions.tasks.cleanup_expired_promotions.cleanup_expired_promotions",
	],
}

before_install = "posnext_promotions.install.before_install"
after_migrate = "posnext_promotions.install.after_migrate"
