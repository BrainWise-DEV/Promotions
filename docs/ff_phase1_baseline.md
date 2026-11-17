# F&F Phase 1 baseline (before any Phase 1 change)

Site: nexus.local. Run with
`bench --site nexus.local execute posnext_promotions.dev_tests.run --kwargs '{"module": "<module>"}'`.
Module prefix: `posnext_promotions.posnext_promotions.doctype.`. Pre-existing failures are recorded, not fixed.

| Module (suffix) | Run | Failed | Errored |
|---|---|---|---|
| friends_and_family_settings.test_friends_and_family_settings | 20 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_apply | 23 | 1 | 0 |
| friends_and_family_settings.test_friends_and_family_best_discount | 31 | 3 | 2 |
| friends_and_family_settings.test_friends_and_family_eligibility | 19 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_gifts | 8 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_scheme_rules | 7 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_scheme_profile | 13 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_acceptance | 20 | 16 | 1 |
| friends_and_family_settings.test_friends_and_family_caps | 19 | 0 | 0 |
| friends_and_family_log.test_friends_and_family_log | 21 | 0 | 0 |

## Pre-existing failures

- apply: test_ineligible_customer_gets_no_discount (F)
- best_discount: test_coupon_for_ineligible_customer_unchanged (F), test_fnf_only_response_strips_stale_stamp_for_ineligible (F), test_ineligible_customer_keeps_promotion_only (F), test_coupon_never_overrides_bigger_promotion (E, IndexError), test_invoice_header_discount_weaker_than_fnf_is_removed (E)
- acceptance (F): test_01_eligible_customer_gets_fnf_on_submit, test_01b_verified_domain_customer_eligible_without_group_or_flag, test_02_ineligible_customer_gets_nothing, test_03_cashier_cannot_force_flag_or_rate, test_04_excluded_item_gets_no_fnf, test_05_mixed_cart_with_brand_exclusion, test_05b_all_items_excluded_message, test_06_lower_promotion_loses_to_fnf_with_message, test_07_higher_promotion_wins_with_message, test_08_tie_goes_to_fnf, test_09_no_stacking_with_order_discount, test_11_per_order_cap, test_12_monthly_cap_and_return_credit, test_13_program_expiry_stops_fnf_immediately, test_14_cancel_return_log_and_report_netting, test_15_settings_and_eligibility_changes_are_logged
- acceptance (E): test_10_coupon_vs_fnf

Caps tests live in the `friends_and_family_settings` folder (module above); all green.
