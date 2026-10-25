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

**Revision 3, 2026-10-26, after a second review.** These sections changed:
- §2: the conflict graph, atomic effects, the ERPNext bundle boundary, money bases and numeric rules, and Order vs Coupon
- §4: interactive and submission guards, and the offline scope
- §6: the integration baseline, and an honest dry-run
- §1: competitor additions

**Revision 2.** The architecture and roadmap are approved. The review asked for these corrections:
- §2: corrected groups (Auto Discount is per line, not Order), mutual `combines_with`, defined Priority and Best-for-customer semantics, cart-level resolution, eligibility kept separate from conflicts, manual discounts, and a trace
- §4: the coupon money guard
- §6: delivery plan

### Review decisions

| Decision | Outcome |
|---|---|
| Build first | 1. Conflict model (§2). 2. Weekday/hour windows (#2). 3. Offline targeting (#6). Next: the redemption ledger (#4), **then** coupon batches (#5). Batches need atomic redemption, refund reversal and campaign reporting first. |
| PR 1 base | An integration baseline of #5 + #12 + #13, not #5 alone (§6) |
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

**Later candidates, from the competitor review** (not ranked yet):

| Addition | Reference |
|---|---|
| Fixed-price bundles and AND/OR mix-and-match triggers, with cheapest/most-expensive reward and repeat limits | Toast BOGO; Microsoft Dynamics 365 Commerce discount types |
| `sales_channels` / `order_types` eligibility (dine-in, takeaway, delivery); relevant to NexDine | Foodics promotions and branch targeting; Toast dining options |
| `max_applications_per_order`, which is separate from redemption counts | Shopify Buy X Get Y |
| Item discountability by class (no manual / promotion / tender discounts) | Square discount rules; Dynamics pricing controls |
| Override audit record: actor, approver, reason, before and after total, lines. Extends §1 #8. | Toast discount configuration |
| Tender rules for split and partial payment, refunds and tiers. Extends §1 #3. Tender discounts compound after item discounts. | Dynamics tender discounts |
| Next-order rewards, loyalty, gift cards, customer-specific codes, after the ledger and batches | Odoo discount and loyalty programs |
| Promotion accounting: funding source, GL account, margin impact, returns, sponsor reimbursement. Extends §1 #7. | Dynamics retail discounts; Lightspeed sales reporting |
| Localised (Arabic/English) promotion names on the cashier screen and receipt | Foodics; Lightspeed |

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

### Today: separate authorities

These facts were verified by running #5 (20/20 coupon tests) and #13 (26/26 coupon tests), and by reading the code:

- **Interaction matrix** (`api/promotion_exclusions.py:38-57`): a `(line state × target) → allowed` table, where every target is per line.
  - Item Level and manual discounts block Auto and coupons.
  - Accumulative and *transaction* Auto stack, but a *targeted* Auto rule defeats Accumulative on that line.
  - Gifts combine with everything.
  - Unknown pairs default to allowed.
  - Auto Discount is applied per line even when its rule is `apply_on = Transaction` (`filter_auto_discount_from_header`, `promotions/engine.py:558`).
- **`promotion_type` decides the state.** A **blank** type, which is what POSNext's own `create_promotion` produces, falls through every check. That is bug B in §5.
- **Per-coupon `promotion_conflict`** (#13): Skip the line / Add on top / Better of the two. The default comes from POS Settings `default_coupon_promotion_conflict`.
- **ERPNext same-group resolution.** `priority` and `apply_multiple_pricing_rules` remove alternatives inside `erpnext_apply_pricing_rule` (`apply_offers.py:1281`), before most of the custom pipeline sees them. `rule_map` is built largely from what ERPNext returned.
- **Item + transaction header discounts stack.** But the POS header has **one** money slot (`additionalDiscount`), and four mechanisms can write it, or a line, depending on evaluation order:
  - POS Coupons priced into lines by Promotions
  - legacy whole-cart POS Coupons, when that override is absent
  - ERPNext coupon-code Pricing Rules
  - order promotions

  So an order promotion and a whole-cart coupon are combined by last writer, not by a rule.
- **No offline coupon engine.** Offline, POSNext evaluates cached promotional offers only (`stores/posOffers.js:290`). Entering a coupon always calls the server.
- **Submission does not re-check coupon money.** `invoices.py:1260` checks that the coupon exists and is usable (`check_coupon_code`), then accepts the amounts the invoice already carries.

The matrix also mixes two questions. `excluded_brand` is an **eligibility** fact; `already_discounted` is a **conflict** fact.

### Proposed pipeline (v1)

```
candidate discovery → ERPNext same-group resolution → eligibility → effects → cross-group resolver → application
```

v1 is honest about its boundary. **ERPNext's output for one group on one line is a single input candidate, the "ERPNext bundle".** The resolver therefore cannot weigh, or explain, alternatives that ERPNext discarded. The trace marks those bundles `erpnext_resolved: true`, and lists the Pricing Rules that ERPNext returned. A later version can move candidate discovery ahead of ERPNext's filtering, to absorb same-group resolution. That is out of scope until shadow data shows it matters.

**1. Eligibility (per offer, with no knowledge of other offers).** An offer becomes a candidate only if it passes its own conditions:
- dates, and weekday/hour windows
- customer, group, territory and branch
- items, groups and brands, including coupon brand exclusions
- thresholds, which use the money bases below
- usage limits
- payment method

Each failure records a reason code. Conflicts are never decided here.

**2. Effects: the unit the resolver chooses.** A whole offer can't be the unit. With "Better of the two", coupon C can beat promotion P on line L1 while P beats C on L2, and the right answer is C@L1 + P@L2. So each candidate becomes atomic effects:

`effect = (offer, pricing rule or slab, affected lines, computed money effect)`

- A line-scoped offer yields one effect per line.
- An order offer or a whole-cart coupon yields one effect covering every paid line.
- A gift yields one effect covering its trigger lines.

The effect's stable ID is `offer name + "/" + pricing rule name + "/" + sorted line keys`.

**3. Conflict graph.** The nodes are effects. There is an edge between two effects that may **not** both apply. That is the case when either of these holds:
- **Overlap and incompatibility.** They touch a common line, and the groups aren't mutually compatible: A's group must be in B's `combines_with` **and** B's in A's.
- **A grouping constraint.**
  - at most one coupon per invoice
  - at most one slab per scheme
  - at most one effect per (offer, line)
  - the effects of one non-"Better" offer are all-or-nothing

  The last constraint is enforced by grouping an offer's effects into one node; a "Better of the two" coupon stays split per line.

Each **connected component of the conflict graph** is resolved independently. Effects in different components never conflict, so they always combine. The valid choices in a component are its **maximal independent sets**: sets with no conflict edge inside, to which no further effect can be added.

**4. Choosing a set** (`conflict_policy`):
- **Priority.** List the set's effect priorities in descending order. Compare the lists lexicographically; the higher first differing element wins, and if one list is a prefix of the other, the longer wins. Remaining ties go to the tie-break.
- **Best for customer.** Simulate each set on the same cart, price list, customer and taxes, using the numeric rules below. The lowest payable grand total, before rounding adjustment, wins. Ties go to the Priority comparison, then the tie-break.
- **Tie-break.** Compare the sorted tuples of effect IDs; the smaller tuple wins. IDs compare by Unicode code point. JavaScript must not use `<` or `localeCompare` on strings, which compare UTF-16 code units or depend on locale. Golden fixtures include a non-ASCII ID.

**5. Bounded search.** The cap is **not fixed yet.** Shadow mode (§6, PR 3) records the sizes of candidates and components on real carts, and the cap is set from that data. When a component exceeds the cap:
- **Priority:** add effects greedily in Priority order.
- **Best for customer:** greedily add the effect that gives the lowest simulated total, using Priority only for ties. Falling back to plain Priority would contradict the chosen policy.
- Either way, the trace records `bounded: true`, and support tools can show it.

**6. Application.** The chosen effects are applied in descending priority through the existing named-part accumulator (`promotions/engine.py:1-35`), and the Accumulative caps and `Item.max_discount` still apply.

**7. Manual discounts are constraints.**
- A cashier line discount is a fixed `Manual` effect with `combines_with = {Order, Gift}`, and the resolver never drops it.
- **Raw price edits are not covered yet.** The client sets `is_rate_manually_edited`, but the offer payload doesn't send it, and server detection looks only at discount fields or `discount_source = manual_discount`. PR 1 either sends the flag and the server honours it as `Manual`, or the contract excludes price edits until it does.
- Overriding a manual discount goes through the authorised override (§1 #8).

**The trace** (`offer_trace`), returned by every resolution:
- **per candidate:** eligibility result and reason codes; for an ERPNext bundle, the rules ERPNext returned
- **per conflict edge:** the two effects, and the reason (which side refused, or which grouping constraint)
- **per component:** the sets considered and their scores, the chosen set, the tie-break rule used, and `bounded`
- **the coupon guard result** (§4)

### Money bases and numeric rules

Every group must declare its bases. PR 1 pins **today's** values in tests first, and only then writes them into the contract.

| Basis | Must be declared |
|---|---|
| Threshold amount | Gross, or net of line discounts; tax-inclusive or tax-exclusive |
| Threshold membership | Whether excluded or non-discountable items count toward it. Trigger lines and rewarded lines are listed separately. |
| Threshold stability | **Proposal:** thresholds are evaluated once, on the pre-resolver base, so a discount chosen by the resolver can't knock out another offer's eligibility. Without this, resolution can oscillate. |
| Returns | Lines with negative quantity yield no effects. A return reverses the original invoice's recorded effects, through the redemption ledger. |

Numeric rules shared by Python and JavaScript:
- **Line effects:** each is rounded to currency precision when computed, not only at the end.
- **Percentages:** applied in descending priority, each on the running net of the line.
- **Fixed amounts across lines:** allocated in proportion to line net. The rounding remainder goes to the line with the highest net, and ties go to the lowest line index.
- **Tax rounding:** follows the ERPNext setting (per line or per invoice). Fixtures cover both settings, and both inclusive and exclusive taxes.
- **Golden fixtures:** compare every line effect, the tax rows, the header discount and the payable total, not only `grand_total`.

### Default `combines_with`

The defaults are symmetric, so the mutual rule changes nothing by default. ✓ means the pair combines, ✗ means it conflicts on a shared line, and "per coupon" means the next table decides.

| | Item | Accum. | Auto | Manual | Order | Gift | Coupon |
|---|---|---|---|---|---|---|---|
| **Item** | ERPNext bundle | ✗ | ✗ | ✗ | ✓ | ✓ | per coupon |
| **Accumulative** | ✗ | ✓ summed, capped | ✓ transaction Auto; ✗ targeted Auto (it wins today) | ✗ | ✓ | ✓ | per coupon |
| **Auto** | ✗ | (see left) | ERPNext bundle | ✗ | ✓ | ✓ | per coupon |
| **Manual** | ✗ | ✗ | ✗ | n/a | ✓ | ✓ | per coupon |
| **Order** | ✓ | ✓ | ✓ | ✓ | ERPNext bundle | ✓ | per coupon (below) |
| **Gift** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Coupon** | per coupon | per coupon | per coupon | per coupon | per coupon | ✓ | ✗ one per invoice |

`Exclusive` combines with nothing, including `Gift`. A targeted Auto rule beating Accumulative is today's behaviour, so the default keeps it: targeted Auto has a higher default priority, and the two conflict on that line.

**The per-coupon setting covers line discounts and the order discount alike.** It is **not** derived from today's last-writer behaviour; PR 1 pins that behaviour as "current", not as contract.

| `promotion_conflict` | Against line-scoped groups | Against `Order` |
|---|---|---|
| Skip the line | {Gift} only. The coupon skips lines that already carry a discount. | Conflict, and the Priority winner applies |
| Add on top | Combines (the `stacked` line update exists) | Combines. **The order of bases is a review decision.** Proposal: the order discount applies first, and a whole-cart coupon applies to the net after it. |
| Better of the two | Per line, the lower payable total wins | Simulate order-only, coupon-only, and the allowed combination, globally |

Whole-cart coupons are **an effect, not a header write.** In v4, one header slot holds the sum of the chosen header effects, and the trace attributes it to each source.

### Migration
- **v1:**
  - Add the fields.
  - Fill `combine_group` from `promotion_type`. A blank type becomes `Item`; this is a deliberate change that fixes bug B.
  - Fill `combines_with` from the table above.
  - Existing coupons map from `exclude_already_discounted_items`: 1 → Skip the line, 0 → Add on top.
- **v2:** show the knobs in PromotionManagement and CouponManagement.
- **v3:** retire the matrix constants.

Delivery is in §6.

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
  - **Coupon guard: "adding a coupon never raises the grand total".** This would have caught bug B. It runs at two points, because the request alone can't prove it.
    - **Interactive guard.** Today `validate_coupon` receives the customer, company and item rows, but not the taxes, price list or header discount, so it can't compare full totals. PR 1 widens the contract:
      - The client sends the full cart context.
      - The server returns a complete before and after: line effects, tax rows, header discount and payable total, each rounded with `flt(x, precision(...))`.
      - If the total with the coupon is higher, the coupon is not applied. The response carries a structured `reason_code: "COUPON_RAISES_TOTAL"` (the message is display text only), and an Error Log records the cart and `offer_trace`.
      - The client re-checks its own totals after applying the line updates, and refuses with the same code if they disagree.
    - **Submission guard.** On submit (`invoices.py:1260`), the server **recomputes** the coupon's effect on the invoice as it will be saved, instead of trusting the carried amounts. A mismatch beyond currency precision, or a coupon that raises the total, rejects the submission with a reason code. This covers stale clients, held carts restored later, direct API callers and offline sync.
    - **Test.** A metamorphic test runs over every cart in the money matrix and asserts `total(cart + coupon) ≤ total(cart)`, on the server engine and through the client's line-update path. An equal total is allowed, for a coupon worth 0.
    - **Offline coupons are out of scope.** Coupons need the server today, and PR 1 keeps it that way. Offline redemption needs usage limits, expiry, customer checks and reconciliation designed together, as a separate feature.
- **Offline parity.** Offline evaluation is a separate JS engine (`stores/posOffers.js`, `utils/offerStrategies.js`, `posCart.applyOffersOffline`). It already lacks customer group, territory, time-of-day, warehouse and price-list conditions (`posOffers.checkOfferEligibility`, ≈ lines 297-408). There is no offline coupon engine (coupons always call the server). Each new setting must be either implemented in both engines or flagged **online-only**, and online-only offers hidden or marked when offline.
  - Longer term: share one declarative rule format, so one test suite runs against both engines.
- **Performance.** `apply_offers` already runs ERPNext's engine, line passes, min/max, transaction offers, recursion and stock checks on every cart change. The resolver adds a search per conflict component. Its cap is set from shadow-mode measurements (§2, step 5). Cache the rule maps per request. Usage limits add a ledger read, so cache per customer per request.
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

**Baseline first.** PR 1 is branched from `chore/promotions-baseline-5-12-13`, which is #13 (stacked on #5) plus #12's commits cherry-picked. A plain merge of #12 drags in unrelated `main` changes to `install.py`.

Test results on the baseline (all test modules, posnext.local, 2026-10-26):

| Branch | Tests run | Not passing |
|---|---|---|
| #5 alone | 161 | 1 failure, 3 import errors |
| Baseline | 171 | the same 4, nothing new; 1 skip |

The 4 that fail on both:
- `test_gwp_many_items_max_basis` fails.
- `test_bundled_free_item`, `test_recursive_product_free` and `test_free_item_stock` can't import, because they still import `posnext_promotions.api.invoices`, which doesn't exist.

These are recorded as known failures. They are not copied into the contract.

Building the baseline exposed a gap in #12: `test_free_same_item` still asserted the old carving behaviour (2 scanned, pay for 1). It is fixed on #12 in `b367995`.

**Conflict model:** four PRs.

| PR | Contains | Behaviour change |
|---|---|---|
| 1. Test matrix + resolver contract + guards | <ul><li>**Money matrix.** It pins *current* behaviour, including #12 and #13 as intended corrections.</li><li>**Order vs Coupon cases:** the order promotion applied before and after the coupon; coupon removal and recalculation; Net Total vs Grand Total; inclusive and exclusive taxes; fixed and capped-percentage coupons; a threshold offer losing eligibility after another discount; held-cart restore and later revalidation; an ERPNext coupon Pricing Rule vs a POS Coupon.</li><li>**Golden fixtures** shared by Python and JavaScript.</li><li>**The pure resolver:** effects, conflict graph, both policies, tie-break and trace, unit-tested but not wired in.</li><li>**The interactive and submission coupon guards.**</li><li>**`is_rate_manually_edited`** sent in the offer payload, or explicitly excluded.</li></ul> | Only the guards |
| 2. Migration + configuration report | <ul><li>The fields and the backfill (§2 v1).</li><li>**A static report.** It can only list configuration: ambiguous mappings, blank types, Transaction rules that would map to `Item`, and asymmetric or invalid `combines_with`.</li><li>Likely changes against a **sampled cart corpus.**</li></ul> It does not claim actual outcome differences, which need real carts. | None |
| 3. Shadow comparison | The resolver runs next to the matrix on live carts. It logs disagreements with both traces, and records candidate and component sizes to set the search cap. The matrix still decides. | None |
| 4. Activation | A setting switches pricing to the resolver. Header effects are summed into the single slot with attribution. The v2 UI follows, and the matrix code is retired a release later. | Yes, behind the setting |

**Then:**
1. weekday/hour windows (#2)
2. offline targeting (#6)
3. the redemption ledger (#4)
4. coupon batches (#5)

Each must pass the money matrix before merging.

## Sources

- Shopify, combining discounts: https://help.shopify.com/manual/discounts/discount-combinations
- Shopify POS discount combinations: https://help.shopify.com/en/manual/sell-in-person/shopify-pos/discount-management/combining-discounts
- Odoo 17 POS coupons, promotions and loyalty (secondary source): https://www.cybrosys.com/blog/an-overview-of-coupons-and-promotion-in-odoo-17-point-of-sale
- Foodics promotions (priority, customer tags, days and times): https://foodics.helpjuice.com/96006-manage-promotions/559020-promotion-overview
- Square automatic discounts (BOGO, quantity, schedule, minimum spend): https://developer.squareup.com/docs/catalog-api/cookbook/auto-apply-discounts and https://squareup.com/help/us/en/article/6603
- Lightspeed overlapping promotions (best deal): https://x-series-support.lightspeedhq.com/hc/articles/50814305333659 and https://o-series-support.lightspeedhq.com/hc/articles/31329470438171
- Carrefour Egypt card offers: https://www.carrefouregypt.com/mafegy/ar/n/c/clp_cib and https://scbank.com.eg/Carrefour_Campaign.aspx
