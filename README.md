# POSNext Promotions

Standalone ERPNext app for advanced promotions (GWP, accumulative discounts, schedule windows, coupon extras) and the POS authorization gate.

This app does **not** depend on `pos_next`. `pos_next` does **not** depend on this app.

They compose on a site through Frappe `doc_events`, `override_doctype_class`, custom fields, and `extend_bootinfo` (`boot.posnext_promotions = 1`).

Optional runtime: if POS Next's Vue sees `frappe.boot.posnext_promotions`, it may call `posnext_promotions.api.*`. Without this app, POS Next keeps its baseline offers API.

## Install

```
bench get-app /path/to/posnext_promotions
bench --site <site> install-app posnext_promotions
```

Place this app **after** `pos_next` in `sites/apps.txt` when both are installed so this app's Pricing Rule monkey-patch wins.

## Independence

- No `required_apps = ["pos_next"]`
- No Python `import pos_next`
- POS Coupon extra fields are created in `after_migrate` only if DocType `POS Coupon` exists
