# F&F Phase 1 baseline

Site: nexus.local. Run with
`bench --site nexus.local execute posnext_promotions.dev_tests.run --kwargs '{"module": "<module>"}'`.
Module prefix: `posnext_promotions.posnext_promotions.doctype.`.

POS Profile context: when `FF_ACCEPTANCE_POS_PROFILE` is unset, the acceptance module picks
`profile 1` or else the first enabled profile. That fallback depends on ordering, so set the
variable for a deterministic run. The module was verified green three ways:

- with no variable set, which picked "Main POS Profile" (company BrainWise (Demo), SAR, Cash);
- with `FF_ACCEPTANCE_POS_PROFILE="Main POS Profile"`;
- with `FF_ACCEPTANCE_POS_PROFILE="Branch POS Profile"`.

Every profile on nexus.local uses the "Standard Selling" price list, which is in EGP. The
acceptance tests now price through their own selling Price List in the profile currency, so the
profile's price list currency no longer matters.

## Result after Task 0b (all green)

| Module (suffix) | Run | Failed | Errored |
|---|---|---|---|
| friends_and_family_settings.test_friends_and_family_settings | 20 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_apply | 23 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_best_discount | 31 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_eligibility | 19 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_gifts | 8 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_scheme_rules | 7 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_scheme_profile | 13 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_acceptance | 20 | 0 | 0 |
| friends_and_family_settings.test_friends_and_family_caps | 19 | 0 | 0 |
| friends_and_family_log.test_friends_and_family_log | 21 | 0 | 0 |
| friends_and_family_settings.test_ff_testing_helpers | 2 | 0 | 0 |

Caps tests live in the `friends_and_family_settings` folder (module above).

## Was failing → cause → fix

At commit 23824d5 there were 24 failures or errors:

- **apply: test_ineligible_customer_gets_no_discount.** Cause: stale fixture. The "ineligible" customer got a verified email on the approved domain. It was ineligible only through Customer Group, which no longer counts (v2_6_0 `hide_ff_group_flag_eligibility`). Fix: the customer now gets a verified email on a domain that is not approved.
- **best_discount: test_ineligible_customer_keeps_promotion_only, test_coupon_for_ineligible_customer_unchanged, test_fnf_only_response_strips_stale_stamp_for_ineligible.** Cause: the same stale fixture. Fix: the same change.
- **best_discount: test_coupon_never_overrides_bigger_promotion** (IndexError). Cause: stale test. The coupon engine ignores `exclude_already_discounted_items`; the coupon's `promotion_conflict` option replaced it (see `coupon_engine.get_coupon_eligible_items` and `install._backfill_coupon_promotion_conflict`). Fix: the test sets `promotion_conflict="Better of the two"` and asserts that the coupon is rejected and the promotion keeps the line (`kept_promotion_items`).
- **best_discount: test_invoice_header_discount_weaker_than_fnf_is_removed** (TypeError). Cause: code bug. `hasattr(frappe._dict, "as_dict")` is always True, so `_scenario_b_lines` and `_restored_copy` called `None()` on dict lines. Fix: `callable(getattr(item, "as_dict", None))` in `friends_and_family_apply.py`.
- **acceptance: tests 01, 01b, 02–09 and 11–15** (amounts scaled by 0.077). Cause: environment. The profile's price list is in EGP and the invoice is in SAR, so the expected prices were converted. Fix: the test creates a Price List in the profile currency, uses it for the demo Item Prices, and sets it as the demo customers' default price list.
- **acceptance: 01, 05, 05b, 06, 08, 09, 11, 12 and 14** (after the currency fix: an extra "Order Comparison" log, a 20% header discount, a promotion winning). Cause: environment. A site-wide Transaction Pricing Rule (20% on Grand Total) applied to every test invoice. Fix: `setUpClass` disables every Pricing Rule except the demo rules, inside the class transaction that is rolled back.
- **acceptance: test_10_coupon_vs_fnf** (ValidationError "is not the coupon applied to this invoice"). Cause: stale test. `overrides/scoped_coupon.validate_scoped_coupon_lines` requires the header `coupon_code` to match a line's `posnext_coupon_code`, and the POS sends both. Fix: the test sets `doc.coupon_code` as well.
