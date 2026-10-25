# Coupons and promotions: a flexibility study

**Date:** 2026-10-26
**Scope:** `posnext_promotions` and POSNext's coupon and promotion engine.

**Code read:**

| Code | What it is |
|---|---|
| Promotions `main` @ `2e5777c` | the base |
| Promotions #5 (`feat/port-staging-september-coupon-matrix` @ `1ed3a68`) | line numbers below refer to this unless noted |
| Promotions #12 and #13 | the same-item gift fix, and the per-coupon setting for already-discounted lines |
| POSNext `develop` with #424–#428 | the POS client (`POS/src`) |

**Method:** code reading only.

---

## 1. Decision: the changes to make, ranked by value for effort

| # | Change | Customer need | Settings and where they live | Engine change | Effort |
|---|---|---|---|---|---|
| 1 | **One way to say how offers combine** (§2) | "This promo must not stack", "coupons stack with promos", "always give the best deal". Today this is spread across a fixed matrix, an untyped default and a per-coupon checkbox. | **Promotional Scheme and POS Coupon:** `combine_group` (Exclusive / Item promos / Order promos / Coupons / Gifts) and `priority`. **POS Settings (per POS Profile):** `conflict_policy` (Priority / Best for customer). This replaces the hard-coded matrix (`promotion_exclusions.py:38-57`) and generalises the new per-coupon `promotion_conflict`. | A single resolver step after the line passes (`apply_offers.py:1530`). It decides per line which candidate discounts apply, and the matrix becomes its default data. | M |
| 2 | **Daily and weekday time windows** (happy hour, "Fridays 10–12") | Very common in Egyptian retail and food service. Foodics and Square expose it natively. | **Promotional Scheme and POS Coupon:** `active_days` (Mon…Sun checkboxes), `daily_from` and `daily_to` | `schedule.py` treats `pos_active_from_time`/`to_time` as absolute boundaries of the first and last day only, *"they do not represent a daily repeating schedule"* (`promotions/schedule.py:11-13`). Add a daily window check to `filter_rules_by_schedule` (`schedule.py:173`) **and** to the client eligibility check. | S |
| 3 | **Payment-method offers** ("10% with CIB card", "valU 0% installments", "cashback with wallet") | Card-linked deals are a staple at Carrefour Egypt and B.TECH ([Carrefour × CIB](https://www.carrefouregypt.com/mafegy/ar/n/c/clp_cib), [SCB campaign](https://scbank.com.eg/Carrefour_Campaign.aspx)). | **Promotional Scheme:** `payment_methods` (table of Mode of Payment) and `apply_at` = Checkout | No code path uses `mode_of_payment` today: grep finds nothing in `apply_offers.py` or `coupon_engine.py`. Re-evaluate checkout-scoped offers in the payment dialog when the method changes, and show them as a header discount line. | M |
| 4 | **Usage limits and budget** | "First 100 customers", "max 2 per customer per day", "stop at EGP 50,000 of discount". | **Scheme and Coupon:** `max_uses_total`, `max_uses_per_customer`, `max_uses_per_day`, `budget_amount` | Coupons have `maximum_use`/`used`/`one_use` (`pos_coupon.json`) plus `maximum_use_per_customer` (`install.py:72-77`). Schemes have only `one_time_per_customer` (`custom/promotional_scheme.json`). Record redemptions in one ledger doctype (one row per invoice × offer, with amount) and check limits in `apply_offers` and `validate_coupon`. Generalises `one_time_usage.py:10-39`. | M |
| 5 | **Coupon batches** (generate N unique codes, print or SMS them, track each) | Campaign flyers, influencer codes, staff vouchers. Odoo's coupon programs generate codes in bulk ([Cybrosys overview](https://www.cybrosys.com/blog/an-overview-of-coupons-and-promotion-in-odoo-17-point-of-sale)). | **POS Coupon Batch** (new): template coupon, count, prefix, expiry, and an optional customer list | No generation exists. grep finds no bulk create in either `promotions.py`. Each generated POS Coupon links back to its batch, and the engine is unchanged. | S |
| 6 | **Customer targeting offline** (customer group or tag, territory/branch, price list) | VIP and employee prices, branch-only promos. Foodics has "Limit to customer tag" ([Foodics](https://foodics.helpjuice.com/96006-manage-promotions/559020-promotion-overview)); Lightspeed has customer groups and outlets. | Already possible with ERPNext Pricing Rule `applicable_for`, `warehouse` and `for_price_list` **online**. Expose them in PromotionManagement. | Online, `apply_offers` passes `customer_group` and `territory` to ERPNext (`apply_offers.py:1224-1241`). **Offline, the client check ignores them:** `posOffers.checkOfferEligibility` tests only items, groups, brands, qty, amount and one-time (`stores/posOffers.js` ≈ lines 297-408). Ship the conditions with `get_offers` and check them offline. | S |
| 7 | **"Why" on the receipt and in reports** | Customers dispute prices, and managers ask "what did promo X cost us?" | **POS Settings:** `print_promotion_lines` (Yes/No) | Lines already carry `pricing_rules`, `discount_source` and `coupon_code`. Persist an `applied_promotions` child table on the Sales Invoice (offer, type, amount, coupon), print it, and add a "Promotion cost" report. Today only `coupon_code` is a custom field on the invoice (`custom/sales_invoice.json`). | M |
| 8 | **Cashier override with authorisation** (apply or remove a promo manually, extend an expired coupon) | Store reality: a coupon expired yesterday, or a manager approves a goodwill discount. | **POS Profile:** `promotion_override_role` | POSNext already has an authorisation gate for expense voids (`pos_next/authorization`). Reuse it for "force apply / force remove offer" and record it in the ledger from #4. | M |

**Already sound, so keep it:**
- the named-part line accumulator (`promotions/engine.py:1-35`)
- the idempotent pipeline
- cheapest and most-expensive Min/Max (`apply_discount_on_price`, `custom/pricing_rule.json`)
- the Accumulative cap with `Item.max_discount` (`engine.py:178-203`)
- Gift Pool (`api/gift_pool.py`)
- recursive cycles (`apply_offers.py:191-345`)
- excluding free items that are out of stock (`apply_offers.py:746`)

---

## 2. A unified conflict model

### Today: three separate mechanisms

1. **The interaction matrix:** a hard-coded `(item state × target) → allowed` table (`api/promotion_exclusions.py:38-57`, PR #5). States: `already_discounted`, `excluded_brand`, `eligible`, `accumulative`, plus phase-2 `xy_trigger` and `routine_trigger`. Targets: `gwp`, `auto_discount`, `coupon`. Unknown pairs default to `True` (`:231`).
2. **The promotion type decides the state.** `promotion_type` is a Select of Item Level / Auto / GWP / Gift Pool (`custom/promotional_scheme.json`). A scheme with a **blank** type, which is what POSNext's own `create_promotion` produces, falls through every check. That is the root of bug B in §5.
3. **Per-coupon `promotion_conflict`** (new): Skip the line / Add on top / Better of the two (`coupon_engine.py`, Promotions #13). The default for new coupons comes from POS Settings `default_coupon_promotion_conflict`.

ERPNext's own `priority` and `apply_multiple_pricing_rules` still decide among Pricing Rules inside `erpnext_apply_pricing_rule` (`apply_offers.py:1281`). That is a fourth mechanism, invisible in the POS UI.

### Proposed: one resolver, three knobs

| Knob | Where | Values | Meaning |
|---|---|---|---|
| `combine_group` | Scheme / Coupon | `Exclusive` · `Item` · `Order` · `Gift` · `Coupon` | The kind of discount it is |
| `combines_with` | Scheme / Coupon | multi-select of groups (default per group, below) | Which other groups may apply to the same line or order |
| `priority` | Scheme / Coupon | 1–100 (default 50) | Order of evaluation and the tie-break |
| `conflict_policy` | POS Settings (per Profile) | `Priority` · `Best for customer` | How to choose when candidates can't combine |

**Resolution, per line and then per order:**
1. Collect candidate discounts.
2. Keep every set of mutually combinable candidates.
3. If more than one set is left, choose by `conflict_policy`. **Priority** takes the highest-priority set. **Best for customer** takes the largest total saving; this is what Lightspeed does for overlapping promos ([Lightspeed X-Series](https://x-series-support.lightspeedhq.com/hc/articles/50814305333659)).
4. Apply the chosen set in priority order, using the existing accumulator.

Shopify's model is the reference here: each discount declares which classes it combines with (product, order, shipping), and on the same item the best single product discount wins ([Shopify: combining discounts](https://help.shopify.com/manual/discounts/discount-combinations)).

**Default `combines_with`, which reproduces today's behaviour:**

| Group | Combines with (default) | Today's equivalent |
|---|---|---|
| Item (Item Level) | Gift | Matrix: `already_discounted` blocks `auto_discount` and `coupon` |
| Item (Accumulative) | Item(Auto), Gift | Matrix: `accumulative` allows auto, blocks coupon |
| Order (Auto Discount, Transaction) | Gift | Header discount; ERPNext priority |
| Gift (GWP, Gift Pool, same/other-item free) | Item, Order, Coupon | Matrix: GWP allowed everywhere |
| Coupon | per coupon (below) | `promotion_conflict` |
| Exclusive | nothing | (new) |

**How the new per-coupon setting maps:**

| `promotion_conflict` | `combines_with` for the coupon | Resolution |
|---|---|---|
| Skip the line | {Gift} | The coupon applies only to lines with no Item or Order discount |
| Add on top | {Item, Order, Gift} | Stacked (the "stacked" line update already exists) |
| Better of the two | {Gift}, plus per-line Best-for-customer | The coupon competes with the line's promo; the larger one wins |

**Migration:**
- **v1:** add the fields. Fill `combine_group` from `promotion_type` (blank → Item, which fixes bug B), and `combines_with` from the defaults above. Coupons map from `promotion_conflict`. The resolver reads only these fields, so behaviour is unchanged.
- **v2:** show the knobs in PromotionManagement and CouponManagement, with the defaults filled in.
- **v3:** retire the matrix constants. Keep the matrix as the default table, not as code branches.

---

## 3. Settings catalogue

| Setting | Scope | Default | Preserves |
|---|---|---|---|
| `combine_group` | Scheme / Coupon | from `promotion_type` (blank → Item) | Today's matrix states |
| `combines_with` | Scheme / Coupon | per-group table in §2 | Today's matrix booleans |
| `priority` | Scheme / Coupon | ERPNext `priority`, else 50 | ERPNext priority ordering |
| `conflict_policy` | POS Settings | Priority | ERPNext's priority-first resolution |
| `promotion_conflict` | POS Coupon | from `exclude_already_discounted_items` (1 → Skip, 0 → Add on top) | The coupon checkbox |
| `default_coupon_promotion_conflict` | POS Settings | Skip the line | Safe default for new coupons |
| `active_days` | Scheme / Coupon | all days | Today (no weekday filter) |
| `daily_from` / `daily_to` | Scheme / Coupon | empty (all day) | Today's absolute `pos_active_from/to_time` stay as-is |
| `payment_methods` | Scheme | empty (any) | Today |
| `apply_at` | Scheme | Cart | Today (offers evaluate on cart changes) |
| `max_uses_total` | Scheme / Coupon | 0 (unlimited); coupons keep `maximum_use` | Coupon `maximum_use` |
| `max_uses_per_customer` | Scheme / Coupon | 0; schemes map `one_time_per_customer` → 1 | `one_time_per_customer`, `maximum_use_per_customer` |
| `max_uses_per_day` | Scheme / Coupon | 0 | Today |
| `budget_amount` | Scheme / Coupon | 0 (unlimited) | Today |
| `rounding` | POS Settings | none | Today (2-decimal currency rounding) |
| `print_promotion_lines` | POS Settings | No | Today's receipt |
| `promotion_override_role` | POS Profile | empty (off) | Today (no override) |
| Coupon batch: `count`, `prefix`, `template`, `customers` | POS Coupon Batch | n/a | Existing single coupons are untouched |

---

## 4. Risks

- **Money correctness.** Every new knob multiplies the combinations. Three money bugs surfaced in one session (§5).
  - Gate every engine change on a table-driven test matrix: offer type × combine setting × cart shape, asserting the totals the customer pays.
  - Run an "invariant" assertion in `apply_offers`: the line net is never below 0, the discount never exceeds the base, and **adding a coupon never raises the grand total**. That last one would have caught bug B.
- **Offline parity.** Offline evaluation is a separate JS engine (`stores/posOffers.js`, `utils/offerStrategies.js`, `posCart.applyOffersOffline`). It already lacks customer group, territory, time-of-day, warehouse and price-list conditions (`posOffers.checkOfferEligibility`, ≈ lines 297-408). Each new setting must be either implemented in both engines or flagged **online-only**, and online-only offers hidden or marked when offline.
  - Longer term: share one declarative rule format, so one test suite runs against both engines.
- **Performance.** `apply_offers` already runs ERPNext's engine, line passes, min/max, transaction offers, recursion and stock checks on every cart change. The resolver adds a set search per line. Bound it: at most k candidates per line (k ≤ 5) and cache the rule maps per request. Usage limits add a ledger read, so cache per customer per request.
- **Backward compatibility.**
  - Existing coupons and schemes must behave identically after migration (§2, v1). Do a dry-run report on production data first: "these N coupons would change behaviour".
  - PR #5 changed coupon eligibility (it narrowed `is_coupon_broad_discounted`) without a migration. That's the direct cause of bug B. Any further change to defaults needs a migration step and a release note.
- **UX overload.** Every setting gets a sensible default and a one-line hint (as the new "When a line already has a discount" control has). Put advanced knobs behind "More options".

---

## 5. Bugs found this session that motivate this

| | Bug | Status |
|---|---|---|
| A | **Same-item gift carved below `min_qty`.** With "buy 2 get 1" and exactly 2 scanned, `_apply_bundled_same_item_free_discounts` made one of the two paid units free. The customer paid 15 for 2 instead of 30. The GWP path already had the guard (`gwp.get_gwp_same_item_free_qty`). | Fixed in Promotions PR #12 (`eb022b3`, `3b0f5b0`). The rule is no longer reported as applied until the gift is earned. |
| B | **An untyped scheme is replaced by a coupon** (PR #5 only). A blank-type Samsung 10% scheme didn't block a coupon, and the coupon engine priced the line from list price as coupon-only. The 10% vanished and the customer paid EGP 780 more for using a coupon. | Fixed by the per-coupon `promotion_conflict` in Promotions #13, stacked on #5. §2 removes the class of bug. |
| C | **Coupon share key mismatch.** The engine sends `coupon_only_discount`, but the POS read `coupon_discount_amount`. A coupon stacked on a promo showed 1,379.90 instead of 300 on its chip and in totals. | Fixed in POSNext #427. |

All three are field-name and semantic drift between separate code paths: the GWP vs bundled paths, the matrix vs the coupon engine, the server vs the client. That's the strongest argument for the single resolver and the shared test matrix in §2 and §4.

## Sources

- Shopify, combining discounts: https://help.shopify.com/manual/discounts/discount-combinations
- Shopify POS discount combinations: https://help.shopify.com/en/manual/sell-in-person/shopify-pos/discount-management/combining-discounts
- Odoo 17 POS coupons, promotions and loyalty (secondary source): https://www.cybrosys.com/blog/an-overview-of-coupons-and-promotion-in-odoo-17-point-of-sale
- Foodics promotions (priority, customer tags, days and times): https://foodics.helpjuice.com/96006-manage-promotions/559020-promotion-overview
- Square automatic discounts (BOGO, quantity, schedule, minimum spend): https://developer.squareup.com/docs/catalog-api/cookbook/auto-apply-discounts and https://squareup.com/help/us/en/article/6603
- Lightspeed overlapping promotions (best deal): https://x-series-support.lightspeedhq.com/hc/articles/50814305333659 and https://o-series-support.lightspeedhq.com/hc/articles/31329470438171
- Carrefour Egypt card offers: https://www.carrefouregypt.com/mafegy/ar/n/c/clp_cib and https://scbank.com.eg/Carrefour_Campaign.aspx
