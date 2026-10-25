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

**Revised 2026-10-26 after review.** The architecture and roadmap are approved. The review asked for these corrections:
- §2: corrected groups (Auto Discount is per line, not Order), mutual `combines_with`, defined Priority and Best-for-customer semantics, cart-level resolution, eligibility kept separate from conflicts, manual discounts, and a trace
- §4: the coupon money guard
- §6: delivery plan

### Review decisions

| Decision | Outcome |
|---|---|
| Build first | 1. Conflict model (§2). 2. Weekday/hour windows (#2). 3. Offline targeting (#6). Next: coupon batches (#5). |
| Conflict model delivery | Four PRs: test matrix and resolver contract → migration and dry-run → shadow comparison → activation (§6) |
| Defaults | `default_coupon_promotion_conflict` = Skip the line. `conflict_policy` = Priority. Existing coupons migrate from `exclude_already_discounted_items` (1 → Skip the line, 0 → Add on top). |
| Money matrix | Adopted first. "Adding a coupon never raises the grand total" is both a metamorphic test and a production guard (§4). |

---

## 1. Decision: the changes to make, ranked by value for effort

| # | Change | Customer need | Settings and where they live | Engine change | Effort |
|---|---|---|---|---|---|
| 1 | **One way to say how offers combine** (§2) | "This promo must not stack", "coupons stack with promos", "always give the best deal". Today this is spread across a fixed matrix, an untyped default and a per-coupon checkbox. | **Promotional Scheme and POS Coupon:** `combine_group` (Item / Accumulative / Auto / Order / Gift / Coupon / Exclusive), `combines_with` and `priority`. **POS Settings (per POS Profile):** `conflict_policy` (Priority / Best for customer). This replaces the hard-coded matrix (`promotion_exclusions.py:38-57`) and generalises the new per-coupon `promotion_conflict`. | An eligibility step, then one cart-level resolver after the line passes (`apply_offers.py:1530`). The matrix becomes its default data. | M |
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

### Today: four separate mechanisms

1. **The interaction matrix:** a hard-coded `(line state × target) → allowed` table (`api/promotion_exclusions.py:38-57`, PR #5).
   - States: `already_discounted`, `excluded_brand`, `eligible`, `accumulative`, plus the phase-2 states `xy_trigger` and `routine_trigger`.
   - Targets: `gwp`, `auto_discount`, `coupon`.
   - Unknown pairs default to `True`.
   - **Every target is per line.** Auto Discount is applied line by line even when its rule is `apply_on = Transaction`: `filter_auto_discount_from_header` (`promotions/engine.py:558`) strips it from the header so it isn't counted twice.
2. **The promotion type decides the state.** `promotion_type` is a Select with Item Level / Auto / GWP / Gift Pool (`custom/promotional_scheme.json`). A scheme with a **blank** type, which is what POSNext's own `create_promotion` produces, falls through every check. That is the root of bug B in §5.
3. **Per-coupon `promotion_conflict`** (Promotions #13): Skip the line / Add on top / Better of the two. The default for new coupons comes from POS Settings `default_coupon_promotion_conflict`.
4. **ERPNext's own `priority` and `apply_multiple_pricing_rules`** choose among Pricing Rules inside `erpnext_apply_pricing_rule` (`apply_offers.py:1281`). This one is invisible in the POS UI.

The matrix also mixes two questions. `excluded_brand` is an **eligibility** fact: this coupon does not cover this brand. `already_discounted` is a **conflict** fact: another discount already sits on this line. The model below separates the two.

### Proposed: eligibility first, then one resolver

**Step 1, eligibility (per offer, no knowledge of other offers).** An offer becomes a *candidate* only if it passes its own conditions:
- dates and the new weekday/hour windows
- customer, group, territory and branch
- items, groups and brands, including coupon brand exclusions
- min qty and min amount
- usage limits
- payment method

Each failure records a reason code in the trace. Conflicts are never decided here.

**Step 2, conflict resolution (all candidates, whole cart).**

| Knob | Where | Values | Meaning |
|---|---|---|---|
| `combine_group` | Scheme / Coupon | `Item` · `Accumulative` · `Auto` · `Order` · `Gift` · `Coupon` · `Exclusive` | The kind of discount it is. `Manual` is a group the system assigns to cashier discounts; it isn't selectable. |
| `combines_with` | Scheme / Coupon | multi-select of groups (default in the table below) | The groups it agrees to share the cart with |
| `priority` | Scheme / Coupon | integer; a higher number wins, as in ERPNext | The ordering used by the `Priority` policy and for application order |
| `conflict_policy` | POS Settings (per Profile) | `Priority` (default) · `Best for customer` | How to choose between conflicting sets |

**Semantics**, which both the server engine and the offline JS engine must implement identically:

1. **Compatibility is mutual.** Offers A and B can be in the same set only if A's group is in B's `combines_with` **and** B's group is in A's `combines_with`. One side refusing is enough to block, and the trace records which side refused.
2. **Scope of a conflict.** Line-scoped groups (`Item`, `Accumulative`, `Auto`, `Manual`, and line coupons) conflict only when they touch the **same line**. `Order` and whole-cart coupons touch every paid line. `Gift` touches the lines that trigger it.
3. **Global resolution at cart level.** The resolver builds one compatibility graph over all candidates in the cart, and enumerates its *maximal* compatible sets (maximal cliques). It does not resolve line by line, because one order offer or a gift's trigger lines link the lines together.
4. **`Priority` policy.** For each set, list its offers' priorities in descending order and compare the lists lexicographically, so the higher first element wins. If one list is a prefix of the other, the longer list wins. Remaining ties go to the deterministic tie-break.
5. **`Best for customer` policy.** Simulate every set on the same cart, with the same price list, customer, taxes and currency precision, and choose the lowest **payable grand total** (before rounding adjustment). Ties go to the `Priority` comparison, then to the deterministic tie-break. Comparing simulated totals catches interactions, such as a percentage on an already discounted base or an Accumulative cap, that adding up nominal savings would miss.
6. **Deterministic tie-break.** Sort each set's offer IDs (scheme or coupon name), compare those tuples, and the smaller one wins. The same cart therefore always resolves the same way, online and offline.
7. **Application order.** The chosen set is applied in descending priority through the existing named-part accumulator (`promotions/engine.py:1-35`). Accumulative caps and `Item.max_discount` still apply.
8. **Manual discounts are constraints, not candidates.**
   - A cashier line discount or price edit is placed in group `Manual` with `combines_with = {Order, Gift}`. That reproduces today's matrix: a manual line blocks Auto and coupons, and keeps gifts.
   - The resolver never drops the cashier's discount. It drops the candidates that are incompatible with it, and lists them in the trace.
   - Removing a promo in favour of a manual discount, or the reverse, goes through the authorised override (§1 #8).
9. **Bounded search.** Enumeration is exponential in the worst case, so the resolver caps it at 12 candidates for each connected component of the graph. Beyond that it falls back to a greedy pass in `Priority` order and marks the trace `bounded: true`. Real carts are far below the cap, because most candidates are independent and fall into separate components.
10. **Within one group, ERPNext keeps its job.** Two Item rules on one line are still chosen by ERPNext's `priority` and `apply_multiple_pricing_rules`. The resolver works one level up, between groups. This keeps v1 exactly equivalent to today.

**The trace.** Every resolution returns `offer_trace`, which the "why" receipt lines (§1 #7) and support use:
- for each offer: whether it was eligible, with reason codes for failures
- each refused pair, with the side that refused it
- the sets considered and each one's score (the priority list, or the simulated payable total)
- the chosen set, and the tie-break rule that decided, if any
- `bounded`, and the result of the coupon guard (§4)

### Default `combines_with`, derived from today's matrix

These defaults are symmetric, so with them the mutual rule changes nothing. ✓ means the pair combines, ✗ means it conflicts, and "per coupon" means the coupon's setting (next table) decides.

| | Item | Accum. | Auto | Manual | Order | Gift | Coupon |
|---|---|---|---|---|---|---|---|
| **Item** | ERPNext | ✗ | ✗ | ✗ | ✓ | ✓ | per coupon |
| **Accumulative** | ✗ | ✓ summed, capped | ✓ | ✗ | ✓ | ✓ | per coupon |
| **Auto** | ✗ | ✓ | ERPNext | ✗ | ✓ | ✓ | per coupon |
| **Manual** | ✗ | ✗ | ✗ | n/a | ✓ | ✓ | per coupon |
| **Order** | ✓ | ✓ | ✓ | ✓ | ERPNext | ✓ | to confirm (PR 1) |
| **Gift** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Coupon** | per coupon | per coupon | per coupon | per coupon | to confirm (PR 1) | ✓ | ✗ one per invoice |

`Exclusive` combines with nothing, including `Gift`.

Where each cell comes from:
- **Item–Auto ✗, Manual–Auto ✗:** the matrix pair `(already_discounted, auto_discount) = False`, together with `is_already_discounted` covering Item Level and manual lines.
- **Accumulative–Auto ✓, Accumulative–Coupon blocked under Skip:** the `accumulative` matrix row.
- **Gift ✓ everywhere:** `(*, gwp) = True`, and `_COUPON_NON_BLOCKING_SOURCES`.
- **Order ✓ with line discounts:** ERPNext applies the header discount after line discounts.
- **Order–Coupon:** not proven from the code. The header-attribution branch at `apply_offers.py:1067` treats a coupon specially. PR 1 pins it with a test before any default is written.

**How the per-coupon setting maps.** The coupon's `combines_with` against the line-scoped groups (`Item`, `Accumulative`, `Auto`, `Manual`) is set as follows:

| `promotion_conflict` | `combines_with` for the coupon | Resolution |
|---|---|---|
| Skip the line | {Gift} | The coupon skips lines that carry any line-scoped discount; the line keeps its promo |
| Add on top | {Item, Accumulative, Auto, Manual, Gift} | Stacked (the `stacked` line update already exists) |
| Better of the two | {Gift}, and the conflict on that line is resolved with `Best for customer` | The coupon competes with the line's promo, and the lower payable total wins |

**Migration:**
- **v1:**
  - Add the fields.
  - Fill `combine_group` from `promotion_type`. A blank type becomes `Item`; this is a behaviour change on #5 and it fixes bug B.
  - Fill `combines_with` from the table above.
  - Existing coupons map from `exclude_already_discounted_items`: 1 → Skip the line, 0 → Add on top.
- **v2:** show the knobs in PromotionManagement and CouponManagement, with the defaults filled in.
- **v3:** retire the matrix constants. Keep them only as the default table.

The delivery order and the shadow period are in §6.

---

## 3. Settings catalogue

| Setting | Scope | Default | Preserves |
|---|---|---|---|
| `combine_group` | Scheme / Coupon | from `promotion_type` (blank → Item); manual discounts are always `Manual` | Today's matrix states |
| `combines_with` | Scheme / Coupon | the symmetric table in §2; compatibility needs both sides | Today's matrix booleans |
| `priority` | Scheme / Coupon | ERPNext `priority`, else 0; higher wins; ties go to offer ID | ERPNext priority ordering |
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
  - Invariants checked in `apply_offers`: no line net is below 0, and no discount exceeds its base.
  - **Coupon guard, as a test and in production.** "Adding a coupon never raises the grand total" would have caught bug B.
    - **What is compared:** after the coupon pass, compare `grand_total` with the coupon against the pre-coupon `grand_total` from the same request. Both are computed on the same cart, price list, customer, taxes and context, and both are rounded with `flt(x, precision("grand_total"))` before comparing. An equal total is allowed, for a coupon worth 0.
    - **Production:** if the total with the coupon is higher, discard the coupon result and return the pre-coupon state. The POS gets "Coupon not applied: it would raise the total", and an Error Log records the cart, coupon and `offer_trace`. Nothing raises an exception at the till.
    - **Test:** a metamorphic test runs over every cart in the money matrix and asserts `total(cart + coupon) ≤ total(cart)`. It runs against the server and the offline engine.
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

## 6. Delivery plan

**Conflict model:** four PRs, each mergeable on its own.

| PR | Contains | Behaviour change |
|---|---|---|
| 1. Test matrix + resolver contract | The money matrix (offer group × setting × cart shape → expected payable total) pinned against **today's** engine, including Order–Coupon. The coupon guard (§4) in production. The resolver as a pure function, with its trace and tie-break, unit-tested on the same matrix but not wired in. | Only the guard |
| 2. Migration + dry-run | The new fields and the backfill (§2 v1). A "would change" report listing every scheme and coupon whose outcome differs under the new model, starting with blank-type schemes. | None |
| 3. Shadow comparison | The resolver runs next to the matrix on live carts. Disagreements are logged with both traces, and the matrix still decides. | None |
| 4. Activation | A setting switches pricing to the resolver once shadow disagreements are understood. v2 UI knobs. The matrix code is retired a release later (v3). | Yes, behind the setting |

**Then:** weekday/hour windows (#2, server and offline), offline customer targeting (#6), and coupon batches (#5). Each of these must pass the money matrix before merging.

## Sources

- Shopify, combining discounts: https://help.shopify.com/manual/discounts/discount-combinations
- Shopify POS discount combinations: https://help.shopify.com/en/manual/sell-in-person/shopify-pos/discount-management/combining-discounts
- Odoo 17 POS coupons, promotions and loyalty (secondary source): https://www.cybrosys.com/blog/an-overview-of-coupons-and-promotion-in-odoo-17-point-of-sale
- Foodics promotions (priority, customer tags, days and times): https://foodics.helpjuice.com/96006-manage-promotions/559020-promotion-overview
- Square automatic discounts (BOGO, quantity, schedule, minimum spend): https://developer.squareup.com/docs/catalog-api/cookbook/auto-apply-discounts and https://squareup.com/help/us/en/article/6603
- Lightspeed overlapping promotions (best deal): https://x-series-support.lightspeedhq.com/hc/articles/50814305333659 and https://o-series-support.lightspeedhq.com/hc/articles/31329470438171
- Carrefour Egypt card offers: https://www.carrefouregypt.com/mafegy/ar/n/c/clp_cib and https://scbank.com.eg/Carrefour_Campaign.aspx
