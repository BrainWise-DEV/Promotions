# Generic Promotions Architecture — Cross-Repository Review

**Date:** 2026-09-08
**Status:** Request changes before implementation planning
**Reviewed:** `BrainWise-DEV/Promotions` PR #1, `posnext_promotions` @ `70be440`, `pos_next`, `nexdine` @ `03ebd4b`, and the relevant ERPNext POS Invoice implementation
**Evidence level:** Static source review plus repository and CI inspection. The promotion defects have not been reproduced against a live database.
**Validation:** Every factual claim below was independently re-derived from source on the bench. Commands are published inline so figures are reproducible. Two blocking findings and four gaps were added during validation; two of this review's own replacement figures were found unreproducible and corrected. See §*Validation summary* and §*Corrections to this review*.

---

## Validation summary

| Item | Count | Outcome |
|---|---|---|
| Factual corrections to PR #1 | 10 | **10 confirmed** |
| Blocking findings originally raised | 4 | **4 confirmed against source** |
| Blocking findings added by validation | 2 | BF5 (consolidation), BF6 (ERPNext coupon counter) |
| Additional gaps added by validation | 4 | Precision across splits; NexDine trust-safety scope reduction; offline fixes unblocked; phase gates |
| Figures in this review found unreproducible | 2 | LOC and invoice-reference counts — corrected with published methods |

**PR #1 should not proceed to implementation planning.** The architectural conclusion — promotions must not depend on `pos_next` — is correct and confirmed. The design is not ready.

---

## Executive decision

PR #1 reaches the correct major conclusion: the promotions domain cannot depend on
`pos_next`. The reason is broader than NexDine. Promotions are a generic commercial
capability that must work across POS applications, ERPNext Desk documents, APIs,
e-commerce, hospitality, and future sales channels. NexDine is the first strong proof of
that requirement, not the architectural center of the system.

The proposed design is not ready for per-repository implementation plans yet. It still
frames too much of the solution as parity between two invoice types and two POS products.
The core must instead model promotions independently of any document type, UI framework,
or fulfillment workflow. Sales Invoice, POS Invoice, NexDine KOT/recipe handling, and POS
Next offline checkout are adapters around that domain.

The recommended destination is:

> Extract the neutral promotions core directly. It owns promotion definitions,
> eligibility, calculation, entitlement ledgers, quotes, reservations, consumption,
> reversal, audit, and offline capability contracts. POS Next, NexDine, ERPNext Desk,
> and future channels integrate through explicit adapters.

The requirement is now explicitly generic, so Option C should be done directly. Do not
perform Option B and later repeat its metadata migration as Option C. The existing
`posnext_promotions` package can become the migration source and, if useful, retain
promotion-management UI, but the reusable domain must not carry POS Next assumptions.

## Verified conclusions

The following conclusions from PR #1 are supported by the inspected code. Validation status and reproducing command are given where a figure is involved.

| Conclusion | Status | Evidence |
|---|---|---|
| NexDine declares `required_apps = ["hrms"]`, no runtime dependency on `pos_next` | ✅ confirmed | `nexdine/hooks.py:11-12`; `git grep -n pos_next -- '*.py' '*.json' '*.toml' '*.txt'` returns no import, hook or dependency |
| NexDine Python size | ⚠️ **corrected** | **368 tracked files, 27,543 lines** — `git ls-files '*.py' \| xargs cat \| wc -l`. PR #1's 43,282 and this review's 42,912 both unreproducible; see §*Corrections to this review* |
| NexDine is primarily POS-Invoice-based | ✅ confirmed, figure corrected | `git grep -o '"POS Invoice"' -- '*.py'` → **178**; `'"Sales Invoice"'` → **18** |
| NexDine has no dedicated coupon/promotion authoring DocTypes or complete engine | ✅ confirmed | no matching DocType directories; discount surface limited to manual `additional_discount_percentage`, a TS totals engine, and an offer cache |
| `posnext_promotions` is predominantly Sales-Invoice-native | ✅ confirmed | `hooks.py` `doc_events` — 4 capabilities on Sales Invoice, 1 on POS Invoice |
| Promotions registers only Min/Max for POS Invoice, and it returns early when `pos_next` is installed | ✅ confirmed | `posnext_promotions/overrides/pricing_rule.py:714` |
| Promotions does not register the authorization gate, one-time recording, one-time release or free-bundle handling for POS Invoice | ✅ confirmed | `posnext_promotions/hooks.py:67-79` |
| NexDine's offline Pricing Rule query excludes NULL `valid_from` / `valid_upto` | ✅ confirmed | `nexdine/nexdine/api/nexdine_offline.py:357-377` |
| Cached offer payload lacks eligibility scope | ✅ confirmed | same function returns only `name, title, apply_on, rate_or_discount, discount_percentage, rate` |
| NexDine re-derives base line prices from authoritative Item Price records | ✅ confirmed — **and under-exploited, see Gap C** | `nexdine_order.py:1395-1425` clears `invoice.items` and rebuilds from `Item Price`, throwing when absent; `git grep -n ignore_pricing_rule -- '*.py'` → **no match** |
| NexDine's recipe-stock hook consumes ingredients for every recipe-backed line, including a free dish — economically correct | ✅ confirmed | `nexdine.nexdine.hooks.nexdine_recipe_stock.before_submit` |
| NexDine's Frappe v15 CI is real and working; `03ebd4b` dated 2026-07-08 | ✅ confirmed | `git log -1 --date=iso 03ebd4b` → `2026-07-08 14:53:31 +0300`; `.github/workflows/ci.yml` |
| The duplicate promotions surface in `pos_next` is substantial | ✅ confirmed | 39 shared function names; 2,201 LOC in `pos_next/api/offers.py`, `api/promotions.py`, `overrides/pricing_rule.py`, `doctype/pos_coupon/pos_coupon.py` |

The broader conclusion is therefore correct: the viable dependency direction is from
every consuming sales channel to the promotions core, never from Promotions to one
specific POS application or invoice type.

## Generic promotions boundary

The core abstraction is not "apply offers to a Sales Invoice" or "add promotions to
NexDine." It is:

> Evaluate a commercial basket in an authoritative context, produce deterministic
> benefits and obligations, and manage any scarce entitlements through an auditable
> lifecycle.

The core must not know about Vue, React, KOTs, tables, rooms, or the internal controller
of a particular invoice. It receives normalized inputs and returns a decision. Consumer
adapters decide how that decision is materialized into their documents and fulfillment
flows.

### Generic input contract

```text
PromotionContext
  tenant: company, currency, jurisdiction
  channel: desk, pos_next, nexdine, ecommerce, api, other
  actor: user, role, device, session
  customer: identity, groups, territory, segments
  location: branch, warehouse, outlet
  transaction: type, timestamp, price list, source document, logical order
  capabilities: online, offline snapshot, stock reservation, authorization

Basket
  stable line identity
  item, UOM, quantity, authoritative base price
  item group, brand and attributes
  existing discount state
  parent/modifier/component relationships
  tax and charge classification
```

Channel-specific data may be supplied in a namespaced extension object, but generic rule
code must not import a consumer application to interpret it. Rules that depend on an
extension declare that requirement explicitly and become ineligible on channels that do
not provide it.

### Generic output contract

```text
PromotionDecision
  calculation_id and engine_version
  applied and rejected rules with explanation codes
  line adjustments and header adjustments
  free-item grants and their qualifying parents
  entitlement reservations required
  authorization requirements
  stock requirements
  warnings, incompatibilities and capability failures
  immutable input and rule-version hashes
```

The calculation result is data, not an already-mutated Frappe document. Each adapter
must prove that the document it saves represents that result exactly.

### Generic lifecycle

```text
evaluate → quote → reserve → consume → reverse
                ↘ expire
```

- **Evaluate** is pure and may be repeated.
- **Quote** binds a result to normalized inputs and rule versions for a limited time.
- **Reserve** atomically claims scarce coupon, gift, one-time, budget, or stock capacity.
- **Consume** attaches the reservation to the authoritative submitted transaction.
- **Reverse** records an idempotent compensating event; it does not erase history.
- **Expire** releases an unused reservation safely.

Simple deterministic discounts may not require a scarce reservation, but they still
carry a calculation identity and explanation trace.

### Required core ports

The neutral core should define interfaces for:

- Catalog and authoritative base pricing.
- Customer and historical purchase facts.
- Rule and schedule storage.
- Inventory availability and optional reservation.
- Entitlement ledger and atomic uniqueness.
- Authorization policy and grant verification.
- Tax/charge classification supplied by the consumer.
- Clock, currency precision and rounding policy.
- Audit, telemetry and reconciliation.

ERPNext implementations of these ports can live in the core app. Restaurant, offline,
or UI-specific orchestration remains in adapters.

---

## Blocking finding 1 — the coupon models are incompatible

**Validation: CONFIRMED. This is the most important finding in the review, and PR #1 missed it entirely.**

This is the largest missing decision in the current remediation design.

The two invoice types do not mean the same thing by `coupon_code`:

| Document | Field semantics | Maintained by |
|---|---|---|
| Sales Invoice under Promotions | Custom **`Data`** field containing a `POS Coupon` code | `posnext_promotions` |
| POS Invoice | **Native ERPNext `Link`** → `Coupon Code` | ERPNext |

Verified: `pos_invoice.json` declares `coupon_code` as `Link`/`Coupon Code`; `sales_invoice.json` declares **no** coupon field; `posnext_promotions/posnext_promotions/custom/sales_invoice.json` adds `coupon_code` as `Data`.

ERPNext's POS Invoice controller validates its `coupon_code` as a standard `Coupon Code`
and updates ERPNext's native coupon counters during submit and cancellation
(`erpnext/accounts/doctype/pos_invoice/pos_invoice.py:227-252, 286-289`).

The separate `POS Coupon` DocType contains `erpnext_coupon_code` and `pricing_rule`
fields, but the inspected implementation does not populate or maintain them. Re-homing
`POS Coupon` from `pos_next` to Promotions therefore does not make it compatible with
NexDine's POS Invoice path.

> **Consequence for PR #1:** its ADR-6 ("register the same lifecycle hooks on POS Invoice")
> is unsafe as written. Writing a `POS Coupon` code into POS Invoice's `coupon_code` hands
> ERPNext a string it will resolve as a `Coupon Code` document name, and NexDine would carry
> two coupon systems on one document. ADR-6 must be withdrawn and replaced by the adapter
> contract.

### Required ADR — canonical coupon model

The design must choose one of these explicitly:

1. **Canonical ERPNext model — preferred clean destination.** Extend ERPNext `Coupon
   Code` and Pricing Rule with the extra promotion metadata, migrate existing `POS
   Coupon` data, and use the immutable promotion redemption ledger as the authority.
2. **Compatibility model.** Keep `POS Coupon`, re-home it to Promotions, and add
   distinct `promotion_coupon` and `promotion_coupon_code` fields to both invoice
   types. Leave POS Invoice's native `coupon_code` field untouched.

Do not place `POS Coupon` values into POS Invoice's existing `coupon_code`. ERPNext will
interpret them as names of standard `Coupon Code` documents.

The ADR must additionally define:

- Whether native ERPNext coupons and Promotions coupons may coexist or stack.
- Which record owns validity, company, customer, usage limit, and gift-card balance.
- How existing `POS Coupon`, ERPNext `Coupon Code`, and invoice data are migrated.
- Whether the legacy mutable `used` counters are retained only as projections.
- How submit, cancel, return, amend, and retry affect the immutable redemption ledger.
- How one coupon redemption is allocated across NexDine split invoices.

Until this ADR is closed, "NexDine installs Promotions alone and gets everything" is not
true.

## Blocking finding 2 — hook parity is not consumer integration

**Validation: CONFIRMED.** `nexdine_order.py:745` passes the client `items` list alongside the authoritative `inv`:

```python
_run_kot_with_failure_surface(inv, customer, table, items, past_item, comments)
```

This is a fulfilment defect, not an accounting one, which is precisely why hook parity cannot reach it.

Registering the same lifecycle hooks on Sales Invoice and POS Invoice is necessary but
insufficient. Generic Promotions needs a stable adapter contract because every consumer
can have a different document-construction, pricing, fulfillment, and settlement
pipeline. NexDine is the concrete case that exposes this gap.

NexDine currently performs this sequence:

1. Resolve the POS Invoice and authoritative price list.
2. Parse and validate the client item/add-on selection.
3. Clear and rebuild invoice rows using server-loaded Item Prices.
4. Materialize taxes and shipping.
5. Validate sellable and recipe stock.
6. Save the invoice.
7. Optionally settle and submit it.
8. Generate KOT changes using the original client `items` list.

The relevant flow is in
`nexdine/nexdine/doctype/nexdine_order/nexdine_order.py:609-745`.

If Promotions adds a free prepared item to the authoritative invoice, the current KOT
call will not see it because `_run_kot_with_failure_surface` receives the original
browser item list, not the final invoice rows. A free dish could consequently appear on
the invoice and consume ingredients without ever reaching the kitchen.

### Required NexDine orchestration

The NexDine adapter should execute:

```text
resolve context and authoritative base prices
  → evaluate promotions
  → materialize discounts and free lines
  → validate sellable and recipe stock
  → save draft and persist calculation identity
  → atomically reserve scarce entitlements before submit
  → submit and finalize entitlement consumption
  → generate KOT from the authoritative final-line delta
  → carry calculation identity and provenance through consolidation (see BF5)
```

The adapter context must include:

- Company, currency, posting time, POS Profile, price list, and warehouse.
- Customer, customer group, and the anonymous/default-customer distinction.
- Branch, restaurant, room, table, waiter, and cashier.
- Order type, including Dine In, Take Away, Delivery, and Aggregator.
- Add-on parent, group, selection, surcharge, and default-selection identity.
- Stable client line identity and authoritative server line identity.
- Offline snapshot, device, cashier, and sequence identity.
- Split group, source invoice, and logical order identity.

Promotion core must not import NexDine. NexDine owns the adapter that translates its
document and restaurant context into the stable provider contract.

## Blocking finding 3 — bill splitting is absent from the design

**Validation: CONFIRMED.** `_copy_item_fields` (`nexdine_order.py:1003-1021`) copies exactly 15 fields — `item_code, item_name, qty, rate, price_list_rate, base_price_list_rate, comment, custom_course, cost_center, custom_is_addon, custom_addon_group_id, custom_addon_group, custom_addon_group_name, custom_addon_surcharge, custom_is_default_selection`. **None** is `is_free_item`, `pricing_rules`, `discount_percentage` or `discount_amount`.

NexDine supports split-by-items, split-table, and equal/custom payment collection. The
current `_copy_item_fields` implementation does not preserve:

- `is_free_item`
- Promotion or Pricing Rule identity
- Calculation or quote version
- Free-item parent/provenance
- Coupon/redemption allocation
- Logical promotion transaction identity

Without a defined policy, splitting can lose, duplicate, or re-evaluate a benefit. For
example, two sibling invoices might each attempt to consume a one-use coupon, or a free
dish might lose its provenance when copied.

### Required split policy

- Evaluate promotions against the logical original order before a split is committed.
- Freeze the calculation and allocate its monetary and free-item effects to siblings.
- Consume coupon, one-time, gift-pool, and cross-cart entitlements once per logical
  promotion transaction—not once per sibling invoice.
- Copy promotion provenance whenever invoice rows move.
- Recalculate or reject when a split changes the qualifying basket.
- Recalculate or reject when a sibling's customer differs from the customer whose
  eligibility produced the promotion.
- Prevent independent cancellation of a sibling from reversing a group entitlement
  incorrectly.
- Define how returns against one sibling restore quantities or entitlements.
- **Added by validation:** state the monetary **allocation rule** (largest-remainder or
  equivalent) and require sibling totals to reconcile to the parent to the last minor unit.
  See Gap B.

Required tests include splitting baskets containing a coupon, free dish, threshold
discount, one-time offer, cross-cart rule, and add-ons.

## Blocking finding 4 — recipe and composite ownership is underspecified

**Validation: CONFIRMED, and the reframing is strictly better than PR #1's.**

`nexdine_recipe_stock.before_submit` is not a component expander. It creates a separate
Material Issue Stock Entry for recipe ingredients. `hospitality_core` uses a different
composite processing mechanism. Calling both "expanders" hides the actual integration
risk.

ADR-4 should define **stock-consumption ownership** instead:

- Which app owns consumption for each sellable item?
- Can one item be configured in both recipe/composite systems?
- If both apps are installed, how is exactly one owner selected?
- Is a NexDine + `hospitality_core` site officially supported or merely possible?
- How is free-promotion provenance carried into Stock Entry rows and profitability
  reporting?
- How are return, cancellation, and failed-submit reversals made idempotent?
- How are serial, batch, UOM, warehouse, and negative-stock policies preserved?

If both restaurant systems are supported together, the acceptance invariant is **one
economic inventory consumption**, not merely "components expanded once." If composed
installation is not a supported product shape, remove it from the mandatory CI matrix
and declare the incompatibility explicitly.

---

## Blocking finding 5 — consolidation produces the accounting document *(added by validation)*

**Severity: HIGH.** On the POS Invoice path the durable accounting document is not the POS
Invoice. **POS Invoice Merge Log creates a Sales Invoice** —
`erpnext/accounts/doctype/pos_invoice_merge_log/pos_invoice_merge_log.py:350`
(`frappe.new_doc("Sales Invoice")`), recorded at `:160` as `consolidated_invoice`. NexDine
references merge logs in 10 files.

Three consequences, none currently in the design:

**(a) Promotions' Sales Invoice hooks fire a second time.** `posnext_promotions` registers
`record_one_time_offer_usage` on Sales Invoice `on_submit` and
`apply_min_max_price_discounts` on `validate`. Consolidation submits a Sales Invoice, so both
run again over rows already priced on the POS Invoice. Under any hook-parity proposal a
one-time redemption would be recorded once at POS Invoice submit and again at consolidation.
Today this is idempotent **only by accident** — the composite autoname plus
`ignore_if_duplicate=True`.

**(b) Discount provenance is destroyed at consolidation.** `merge_pos_invoice_into`
(`:234-238`):

```python
item.rate = item.net_rate
item.amount = item.net_amount
item.base_amount = item.base_net_amount
item.price_list_rate = 0
```

`price_list_rate` is **zeroed** and the discount is baked into `rate`. The consolidated Sales
Invoice retains no record of list price or discount applied. The definition-of-ready criterion
*"every applied discount is explainable from a stored calculation version"* is therefore
**unachievable on the POS Invoice path** unless calculation identity is carried across the
merge explicitly.

**(c) `apply_min_max_price_discounts` silently no-ops on consolidated invoices**, because
`_apply_discount` (`posnext_promotions/overrides/pricing_rule.py:783`) returns early when
`base_rate <= 0`. Correct behaviour, reached by accident, undocumented, and one refactor away
from becoming a double discount.

### Required ADR — authoritative document

- Which document is authoritative for promotion state: POS Invoice, the consolidated Sales
  Invoice, or a promotion transaction record spanning both.
- How calculation identity, `is_free_item`, rule provenance and coupon allocation survive
  `merge_pos_invoice_into`.
- Which promotion hooks are suppressed on consolidated invoices, and how that is enforced
  rather than accidental.
- How returns via `consolidated_credit_note` reverse entitlements exactly once.
- Whether promotion audit rows reference the POS Invoice, the consolidated invoice, or both.

## Blocking finding 6 — ERPNext's own coupon counter is defective *(added by validation)*

**Severity: HIGH.** This review cites `update_coupon_code_count` as the mechanism that makes
POS Invoice coupons work. It does not note that the mechanism carries the same lost-update
defect PR #1 raised for `pos_next` — and that **this is the counter NexDine relies on today.**

`erpnext/accounts/doctype/pricing_rule/utils.py:756-772`:

```python
coupon = frappe.get_doc("Coupon Code", coupon_name)
if coupon.used < coupon.maximum_use:
    coupon.used = coupon.used + 1
    coupon.save(ignore_permissions=True)
```

Read-modify-write with no `for_update` and no atomic conditional `UPDATE`. Two concurrent
tills redeeming the final allowance both read the same value and both write the same
increment.

The defect therefore exists in **three** implementations: the dead copy in
`posnext_promotions/api/coupon_engine.py:437`, the live one in
`pos_next/pos_next/doctype/pos_coupon/pos_coupon.py:181`, and ERPNext core. If the canonical
coupon ADR resolves toward option 1 (ERPNext `Coupon Code`), the redemption ledger is not
merely a modernisation — it is **repairing a live defect in the path NexDine already uses.**

**Secondary:** `if coupon.used < coupon.maximum_use` with `maximum_use = 0` takes the else
branch and throws *"Allowed quantity is exhausted"*. Whether an unlimited-use ERPNext coupon
is usable on POS Invoice at all requires an explicit test.

---

## Required KOT and add-on policy

A free prepared item should normally:

- Appear on the KOT exactly once.
- Consume its recipe exactly once.
- Carry zero customer revenue.
- Book normal COGS.
- Be removed or cancellation-KOTed correctly if the promotion is withdrawn.
- Preserve the applied rule and parent qualifying lines for audit.

Paid modifiers and add-ons should not automatically become free because their parent
dish is free. Add-ons should also not independently qualify as paid basket items unless
the promotion explicitly allows that behavior.

The evaluator needs explicit flags for at least:

- Whether add-on surcharges contribute to minimum amount.
- Whether add-on quantities contribute to minimum quantity.
- Whether a free parent includes default or paid modifiers.
- Whether a free product can be independently customized.
- Whether an automatically added free dish requires a new KOT after the original order
  has already entered preparation.

## Offline findings — reclassify and redesign

**Validation: CONFIRMED, with one consequence to add (Gap D).** `db.offers` is provably
inert — `pos/src/lib/offline/preload.ts:319-328` only `clear()`s and `bulkPut()`s it; the
sole other references are schema declarations at `pos/src/lib/offline/db.ts:160,247,327`. No
read consumer exists.

The NULL-date query and incomplete payload are real, but they are not currently proven
checkout defects. The React client writes the returned records to IndexedDB, but no
consumer of `db.offers` was found beyond clear and `bulkPut`
(`pos/src/lib/offline/preload.ts:319-328`). The cached offer data appears inert.

Classify N1 and N2 as latent/incomplete integration defects until execution proves a
consumer exists.

Do not replace `_fetch_active_offers` with an unrestricted call to
`posnext_promotions.api.offers.get_offers`. That could cache rules that ADR-3 itself
declares online-only.

Promotions should instead publish a dedicated, versioned offline bundle containing only
certified deterministic rules. The bundle must include:

- Snapshot ID, immutable hash, issue time, expiry, and evaluator version.
- Company, POS Profile, price list, currency, taxes, rounding, and warehouse scope.
- Full item/group/brand/customer/order-type eligibility.
- Quantity, amount, stacking, priority, and exclusion semantics.
- An explicit capability marker proving that the rule is offline-safe.
- Manual discount and rate-override ceilings.

NexDine already has a TypeScript totals engine and structured offline conflict handling.
Its adapter should consume the certified bundle and run shared golden conformance vectors
against the server evaluator.

Coupons, one-time rules, gift pools, accumulative rules, and stock-dependent free gifts
remain online-only until there is a reservation or bounded-risk design for them.

## Authorization across consumers

Authorization is a generic policy service, not a Sales Invoice hook. The core should
evaluate protected commercial actions and verify a grant, while each adapter supplies
the channel-specific action and binding context. A manager approval granted in one
channel, document, action, or calculation must not authorize another.

The common action vocabulary should cover manual discounts, price overrides, returns,
voids, complimentary items, promotion exceptions, entitlement overrides, and policy
changes. Consumers may add namespaced actions without putting their UI or workflow code
inside the core.

For NexDine specifically, simply adding the gate to POS Invoice `before_submit` is not
enough. NexDine accepts manual `additional_discount_percentage`, shipping overrides,
returns, voids, split operations, and other restaurant actions before submission.

The NexDine policy must classify:

- Manual header discount.
- Manual rate or price override.
- Shipping/delivery fee override.
- Return with and without a source invoice.
- Void after KOT.
- Removal of a prepared item.
- Complimentary item not generated by a promotion.
- Bill or table splitting after a promotion is frozen.
- Customer replacement after a customer-bound promotion is quoted.

Authorization grants should remain user-, session-, action-, document-, and calculation-
bound, single-use, time-limited, and audited. Redis failure must deny PIN approval or use
an explicitly designed database-backed fallback.

---

## Additional gaps *(added by validation)*

### Gap A — see Blocking findings 5 and 6

Promoted to blocking findings above.

### Gap B — currency precision and rounding across splits · MEDIUM

Neither document addresses precision. `posnext_promotions/overrides/pricing_rule.py:825-834`
`_rate_precision` returns a hardcoded `2` for any payload lacking a `precision()` method —
wrong for 3-decimal currencies (KWD, BHD, OMR, TND). Splitting compounds this: BF3 requires
allocating a frozen monetary effect across siblings, which is a rounding-allocation problem
that must sum exactly to the original.

**Required:** resolve precision from the document currency rather than defaulting to 2; state
the split allocation rule explicitly; require a test that sibling totals reconcile to the
parent to the last minor unit in a 3-decimal currency.

### Gap C — NexDine is already trust-safe, which shrinks the critical path · MEDIUM (scope reduction)

This review lists NexDine's server-side price derivation as a verified conclusion but its
delivery plan does not exploit it.

`_populate_invoice_items` (`nexdine/nexdine/doctype/nexdine_order/nexdine_order.py:1395-1425`)
sets `invoice.items = []`, rebuilds every row from server-side `Item Price` lookups, and
**throws** when no price exists. NexDine **never sets `ignore_pricing_rule`**
(`git grep -n ignore_pricing_rule -- '*.py'` → no match), so ERPNext's pricing engine remains
live.

That is the opposite of `pos_next`, which sets `ignore_pricing_rule = 1`
(`pos_next/api/invoices.py:786`) and comments *"Trust frontend's price_list_rate"* (`:839`).

**Consequence:** the client-trust remediation is a **`pos_next` defect, not a generic
redesign.** NexDine already satisfies the invariant. Presenting it as cross-cutting overstates
the work and delays the consumer that is ready. **NexDine can receive promotions without
waiting for the `pos_next` trust-boundary work**, which reorders the delivery plan in the
product's favour.

### Gap D — the offline fixes are unblocked · LOW

Because `db.offers` is provably inert (no read consumer), **N1 and N2 are free to fix now**,
before any ADR closes. Correcting the NULL-date filter and widening the payload cannot regress
behaviour that nothing consumes. They belong in Phase 2, not behind the offline-bundle design.

### Gap E — no rollback or abort criteria · MEDIUM

Both documents define a destination and a phase sequence. Neither defines what makes a phase
*fail*, nor how to reverse a landed phase. Given NexDine is live at 10+ outlets and the plan
includes live metadata migration between apps, each phase needs: preconditions, an explicit
abort trigger, a reversal procedure, and a named decision-maker. The shadow-rollout phase in
particular needs a stated difference-rate threshold above which enforcement does not proceed.

---

## Factual corrections required in PR #1

All ten confirmed by validation. Each carries the reproducing evidence.

### NexDine frontend · ✅ CONFIRMED

NexDine's `/pos` is React 19 and TypeScript, not Vue. It uses Zustand, Dexie, Vite, and
Vitest (`pos/package.json`). The separate KOT Display application uses Vue.

*Evidence:* `pos/package.json` — react ^19.0.0, react-dom ^19.0.0, zustand ^5.0.6, dexie
^4.4.2, vite ^6.2.0, vitest ^3, typescript ~5.7.2. `kotdisplay/package.json` — vue ^3.3.4.

Replace every "NexDine Vue POS" statement with "NexDine React/TypeScript POS."

### LOC count · ✅ CONFIRMED — **and this review's substitute is also unreproducible**

PR #1's `43,282` came from `find … -name '*.py' -print -exec cat {} +`, where `-print` fed
every *filename* into the line count and `node_modules` was not excluded.

| Method | Result |
|---|---|
| `git ls-files '*.py' \| xargs cat \| wc -l` (tracked only) | **27,543** across **368** files |
| `find . -path '*/node_modules/*' -prune -o -name '*.py' -print0 \| xargs -0 cat \| wc -l` | 35,017 |
| PR #1's broken command | 43,282 |
| This review's figure | 42,912 — not reproducible on this bench |

**Use:** *"368 tracked Python files, 27,543 lines (`git ls-files '*.py' | xargs cat | wc -l`)"*,
or drop the figure. The argument does not depend on it.

### `pos_next` mentions · ✅ CONFIRMED

"Exactly one mention of `pos_next` anywhere" is false. There are multiple comments and
design-document references. The material claim is:

> NexDine has no imports, hooks, declared dependency, or runtime calls into `pos_next`.

*Evidence:* `git grep -li pos_next` → 4 files (`docs/2026-04-14-offline-mode-design.md`,
`nexdine/nexdine/api/nexdine_qz.py`, `pos/src/components/offline/CacheStatus.tsx`,
`pos/src/lib/offline/mutex.ts`), 14 occurrences, none an import, hook or dependency.

### Invoice reference counts · ✅ CONFIRMED — **and this review's substitute is also unreproducible**

| Method | POS Invoice | Sales Invoice | Ratio |
|---|---|---|---|
| `git grep -o '"POS Invoice"' -- '*.py'` (quoted DocType strings, tracked) | **178** | **18** | ~10:1 |
| `git grep -o 'POS Invoice'` (raw, all tracked files) | 828 | 207 | 4:1 |
| PR #1 | 163 | 13 | not reproducible |
| This review | 405 | 77 | not reproducible |

The quoted-string method is the meaningful one — it counts DocType references rather than
prose — and it supports the qualitative conclusion more strongly than either published figure.

### Promotions surface · ✅ CONFIRMED

"No promotions surface at all" is too broad. NexDine has no dedicated promotion engine
or authoring DocTypes, but it does have:

- Manual additional discounts.
- A TypeScript totals engine.
- A Pricing Rule offer cache.
- Offline discount operations.

*Evidence:* `additional_discount_percentage` / `discount_amount` in `nexdine_offline.py`,
`nexdine_order.py`, `nexdine_pos/api.py`; `pos/src/lib/totals/engine.ts` with `engine.test.ts`
and `fallback.ts`.

Use "no dedicated promotions engine or authoring model."

### "Does nothing" statement · ✅ CONFIRMED

With NexDine alone, Promotions' POS Invoice Min/Max handler can run. With `pos_next`
installed, that handler returns early. Promotions also changes Pricing Rule metadata and
can affect ERPNext through its import-time patch.

Use this narrower conclusion:

> Promotions provides no complete NexDine transactional promotion lifecycle. On a
> composed site containing `pos_next`, even its sole POS Invoice calculation hook is
> suppressed.

### Functional-superset claim · ✅ CONFIRMED

Thirty-nine shared function names and additional modules establish a broader feature
surface, not strict behavioral supersession. Replace "strict functional superset" with
"broader implementation that appears intended to supersede the duplicated engine," and
require contract tests before deletion.

### Dead DocTypes · ✅ CONFIRMED — PR #1 was wrong

Do not mark `POS Offer`, `POS Offer Detail`, and `POS Coupon Detail` as safe to drop yet.
`POS Offer` remains linked from the workspace, its form controller, POS Coupon navigation,
and child schemas. Before removal, inspect live row counts, Dynamic Links, fixtures,
reports, exports, integrations, and customer scripts, then provide a migration or archival
path.

*Evidence:* `git grep -l "POS Offer"` → `pos_next/pos_next/doctype/pos_coupon/pos_coupon.js`,
`pos_next/pos_next/workspace/posnext/posnext.json`, `README.md`. **PR #1's zero-reference
count searched Python and `POS/src` only — it omitted desk JS and workspace JSON.**
`POS Offer Detail` and `POS Coupon Detail` are genuinely unreferenced outside their own
directories, but still require the row-count and link audit above.

### One-time key migration contradiction · ✅ CONFIRMED

`CODE_REVIEW.md` says migration must detect both one- and two-colon historical names,
while the design spec proves the declared autoname cannot generate two-colon names.

Use one consistent conclusion: query for semantic duplicates and enforce uniqueness on
`customer + pricing_rule`; do not plan name normalization unless manually named or
imported rows are shown to exist.

*Evidence:* `frappe/model/naming.py:565-583` — `_format_autoname` strips only the `format:`
prefix, substitutes only `{...}` params, and returns the result verbatim; its docstring states
it is *"independent of remaining string or separators."*

### Stale NexDine appendix · ✅ CONFIRMED

The appendix in `CODE_REVIEW.md` still states that NexDine runs `pos_next +
hospitality_core`. Delete or fully rewrite that appendix. A top-level errata note is not
sufficient because the appendix reaches architectural conclusions from the discarded
premise.

---

## Corrections to this review

Applied during validation, in the same spirit the review applies to PR #1.

1. **Publish or drop the LOC and invoice-reference figures.** 42,912 and 405/77 do not
   reproduce on this bench. Reproducible values with published commands are given above. The
   review's own standard applies to its own numbers.
2. **"Near-zero cost" was PR #1's claim about the harness, and this review is right to reject
   it.** Adapting NexDine's CI retires the *construction* question, not the verification
   blocker: private-repo auth, pinned SHAs, upgrade matrices and concurrency tests remain
   real work. PR #1's framing is corrected accordingly.
3. **The CI provenance claim checks out** — `git log -1 --date=iso 03ebd4b` →
   `2026-07-08 14:53:31 +0300`.
4. **Gap C should be promoted from a bullet in Verified Conclusions to a scope decision**,
   because it changes the delivery order: NexDine does not need the `pos_next` trust-boundary
   work.
5. **One residual absolute.** "NexDine has no complete promotions engine" is the correct
   formulation and is used in most places; at least one instance still reads as absolute.

---

## Final dependency ADR

The product requirement is now explicitly generic. Therefore the neutral boundary
should be created directly instead of first transferring ownership to a POS-branded app
and extracting it later.

The recommended graph is:

```text
ERPNext
└── promotion_core
    ├── consumed by ERPNext document adapters
    ├── consumed by pos_next adapter
    ├── consumed by NexDine adapter
    └── available to future channel adapters

HRMS
└── NexDine

posnext_promotions
└── migration source and optional management/application layer
```

`promotion_core` should own:

- Promotion definitions and extensions to Pricing Rule/Promotional Scheme.
- Coupon definition after the coupon-model ADR is resolved.
- Immutable entitlement/redemption ledger.
- Eligibility and calculation semantics.
- Quote, reserve, consume, reverse, and offline-bundle contracts.
- Explanation traces and calculation-version persistence.
- Promotion authoring permissions and audit.

It should expose stable service contracts rather than whitelisted functions named after
one consumer. Compatibility endpoints may delegate to those services during migration,
but they must not become the permanent domain API.

Generic ERPNext adapters should own:

- Quotation, Sales Order, Delivery Note, Sales Invoice, and POS Invoice lifecycle
  translation where standard ERPNext semantics are sufficient.
- Authoritative document normalization and persistence checks.
- Submit, cancel, amend, and return mappings to entitlement events.
- **Consolidation behaviour: POS Invoice Merge Log provenance and hook suppression (BF5).**
- Desk and generic API integration.

`pos_next` should own:

- Its Vue checkout integration.
- Sales Invoice construction and authoritative acceptance.
- **Remediation of `ignore_pricing_rule = 1` and frontend price trust (Gap C — this is
  `pos_next`-only work).**
- Its offline queue and device policy.
- Translation from its cart model to the provider contract.

NexDine should own:

- Its React checkout integration.
- POS Invoice construction and authoritative acceptance.
- Branch, order-type, table, restaurant, room, add-on, and aggregator context.
- KOT generation from final authoritative lines.
- Recipe-stock integration.
- Bill/table split allocation.
- Its offline queue and conflict UI.

Neither POS application should depend on or import the other. The core must not import
either POS application.

## Harness assessment

NexDine's CI retires the question of how to construct a basic Frappe v15 harness, but it
does not retire the Promotions verification blocker at near-zero cost.

The Promotions harness still needs:

- Authenticated checkout of private repositories.
- Immutable repository SHAs in each matrix.
- Clean standalone installation.
- Upgrade installation from the pre-migration app versions.
- Exact discovered and executed test counts.
- MariaDB-backed concurrent redemption tests.
- Sales Invoice and POS Invoice lifecycle tests.
- **Consolidation tests: POS Invoice → Merge Log → Sales Invoice, asserting hooks fire once
  and provenance survives (BF5).**
- `pos_next` Vue and NexDine React conformance tests.
- Alternate app ordering where composition remains supported.
- Clean uninstall/reinstall and failed-migration recovery tests.
- A composed-site test only for combinations declared supported.

Suggested matrices:

| Matrix | Required purpose |
|---|---|
| ERPNext + Promotions | Canonical platform installation and desk flows |
| ERPNext + Promotions + `pos_next` | Sales Invoice consumer |
| ERPNext + HRMS + Promotions + NexDine | POS Invoice consumer |
| Upgrade: `pos_next`-owned metadata → Promotions-owned metadata | Re-home safety |
| Upgrade: legacy coupon data → chosen canonical model | Coupon migration safety |
| Both POS apps | Only if the product officially supports this composition |
| NexDine + `hospitality_core` | Only if the product officially supports both recipe systems |

CI work can begin immediately and in parallel with the remaining ADRs. The PR itself
currently has no status checks.

## Required generic and NexDine adversarial tests

Every adapter must pass the same generic conformance and adversarial vectors:

- The same normalized basket, context, rule versions, currency precision, and engine
  version produce the same decision in every channel.
- Disabled, expired, future, wrong-company, wrong-customer, wrong-channel, and
  wrong-location rules are rejected even when named by the client.
- Client totals, discount amounts, eligibility claims, free rows, and rule IDs cannot
  alter the authoritative result.
- Priority, exclusivity, stacking, maximum discount, and rounding have shared golden
  vectors.
- Concurrent final coupon, one-time, gift, and budget allowances produce exactly one
  permitted winner.
- Retried quote, reserve, consume, reverse, cancel, and return operations are
  idempotent.
- Failure of Redis, inventory, history, authorization, or entitlement services cannot
  grant additional value.
- Every applied benefit is reconstructable from stored normalized input, rule versions,
  engine version, and explanation trace.
- Unsupported channel capabilities make the rule ineligible with an explicit reason;
  they never trigger an implicit fallback.
- A core release runs contract tests against every supported adapter before rollout.

For NexDine, additionally require:

- A free prepared item is present on the invoice, KOT, recipe Stock Entry, receipt, and
  promotion audit exactly once.
- Paid add-ons remain paid when their parent dish is free unless the rule says otherwise.
- Add-ons do not qualify independently unless explicitly enabled.
- A free item added after the first KOT produces the correct incremental KOT.
- Removing or cancelling a free prepared item produces the correct kitchen cancellation.
- A coupon applied to POS Invoice does not collide with ERPNext's native Coupon Code
  validation.
- A final coupon allowance has exactly one winner across concurrent NexDine tills.
- One-time redemption has exactly one winner across concurrent POS Invoice submissions.
- A by-items split does not duplicate or lose free-item or discount provenance.
- Equal/custom payment splits consume one logical coupon only once.
- Changing customer during a split cannot retain an ineligible customer-bound benefit.
- A return or group cancellation reverses exactly the consumed entitlement and recipe
  stock once.
- Dine In, Take Away, Delivery, and Aggregator orders receive only eligible rules.
- Branch, restaurant, room, price-list, company, and warehouse scope cannot bleed.
- Offline unsafe rules are unavailable before the cashier promises them.
- A tampered offline outcome is quarantined rather than silently accepted or repriced.

**Added by validation:**

- Consolidating a POS Invoice carrying a promotion records the redemption **exactly once**,
  not once at POS Invoice submit and again at consolidation (BF5a).
- The consolidated Sales Invoice can still explain every applied discount despite
  `price_list_rate` being zeroed at `pos_invoice_merge_log.py:237` (BF5b).
- An ERPNext `Coupon Code` with `maximum_use = 0` behaves as intended on POS Invoice rather
  than throwing *"Allowed quantity is exhausted"* (BF6).
- Split sibling totals reconcile to the parent to the last minor unit in a 3-decimal currency
  (Gap B).

## Revised delivery sequence

### Phase 0 — deployment inventory and containment

- Inventory every managed and customer-managed site.
- Record installed apps, commit SHAs, active promotion settings, coupon data, submitted
  promotion-bearing invoices, and actual consumer paths.
- Treat NexDine's README as evidence that NexDine itself is live, not evidence that
  Promotions is installed at those outlets.
- If Promotions is live, disable unsafe coupons, one-time offers, gift pools, and free
  gifts until server enforcement is complete.
- If Promotions is not live, keep those capabilities behind a release gate.

### Phase 1 — harness and characterization

- Adapt NexDine's working CI.
- Publish exact test counts.
- Capture current Sales Invoice and POS Invoice behavior before refactoring.
- Add failing tests for the confirmed fail-open defects.

### Phase 2 — independent containment fixes

- Fix the one-time key lookup and semantic uniqueness.
- Remove client-named rule readmission.
- Make coupon/redemption failures deny value.
- Validate gift stock at submission.
- Fail PIN lockout closed.
- Replace destructive install behavior.
- Make the ERPNext patch observable, then remove it behind the provider contract.
- Extend authorization coverage to POS Invoice where the action model is already clear.
- **Added by validation:** fix N1 and N2 now — the offer cache has no read consumer, so
  correcting the NULL-date filter and widening the payload cannot regress anything (Gap D).

Temporary fixes may exist in both repositories only while the duplicated implementation
remains deployed. Delete the `pos_next` copy as soon as the consolidated consumer is
released.

### Phase 3 — close schema and ownership ADRs

- Choose the canonical coupon model (BF1).
- **Choose the authoritative document across consolidation (BF5).**
- Establish the neutral `promotion_core` package and ownership boundary directly.
- Design versioned metadata and data migrations.
- Define supported app combinations and stock-consumption ownership.

### Phase 4 — provider and ledger

- Implement one authoritative evaluator.
- Persist calculation identity and explanation trace.
- Implement quote, reserve, consume, reverse, and offline-bundle operations.
- Use atomic database constraints and idempotency keys for scarce entitlements.
- **This repairs the live ERPNext counter defect, not merely `pos_next`'s (BF6).**
- Remove import-time monkey-patching and runtime installed-app branching.

### Phase 5 — consumer adapters

- Implement the generic ERPNext document adapters.
- Implement and verify the `pos_next` Sales Invoice adapter, **including its
  `ignore_pricing_rule` remediation (Gap C — `pos_next`-only)**.
- Implement the NexDine POS Invoice adapter. **This does not depend on the `pos_next`
  trust-boundary work and may proceed in parallel (Gap C).**
- Complete KOT, recipe, add-on, split, return, cancel, and authorization behavior.
- Remove duplicate endpoints, hooks, globals, and engine code from `pos_next`.

### Phase 6 — offline certified subset

- Ship versioned snapshots and shared conformance vectors.
- Enable only deterministic, explicitly certified promotion classes.
- Quarantine mismatches and preserve the amount already collected.
- Expand capabilities individually after observed reconciliation results are acceptable.

### Phase 7 — shadow and outlet rollout

- Shadow-evaluate old and new calculations without changing charged totals.
- Classify every difference as rounding, stale data, evaluator mismatch, or attempted
  manipulation.
- Pilot at one controlled NexDine outlet.
- Expand outlet by outlet with rollback, reconciliation, and profitability reporting.

### Phase gates *(added by validation — Gap E)*

Every phase above requires, before it starts and before it is declared done:

| Element | Requirement |
|---|---|
| Preconditions | Which prior phase outputs must exist |
| Abort trigger | The observation that stops the phase |
| Reversal procedure | How a landed phase is undone, tested in CI |
| Decision-maker | Named role who calls abort or proceed |
| Phase 7 specifically | A stated difference-rate threshold above which enforcement does not proceed |

This is mandatory rather than advisory: NexDine is live at 10+ outlets and the plan includes
live metadata migration between apps.

## Definition of ready for implementation plans

Per-repository plans can be written after these decisions are closed:

- [ ] Canonical coupon model chosen (BF1).
- [ ] **Authoritative document across consolidation chosen (BF5).**
- [ ] Neutral core ownership and package boundary accepted; no deferred B-to-C migration
  remains.
- [ ] Generic input, output, lifecycle, capability, and port contracts accepted.
- [ ] Standard ERPNext document-adapter coverage accepted.
- [ ] Cross-channel conformance and capability-failure behavior accepted.
- [ ] NexDine pricing/KOT/recipe orchestration contract accepted.
- [ ] Bill-split promotion allocation policy accepted, **including the monetary allocation
  rule and precision handling (Gap B)**.
- [ ] Add-on and free-prepared-item policy accepted.
- [ ] Supported composed-site combinations declared.
- [ ] Recipe/composite stock-consumption ownership declared.
- [ ] Deployment inventory determines containment versus release-gate sequencing.
- [ ] CI matrix has named repositories, branches/SHAs, and test ownership.
- [ ] **Phase gates and rollback criteria defined (Gap E).**

Once those decisions are made, implementation plans should be split into core, migration,
and consumer-adapter plans, but share one contract and one set of conformance vectors. No
repository should independently invent promotion eligibility, redemption, or calculation
semantics.

## Recommended next decisions, in order

1. **Canonical coupon model ADR** (BF1). Blocks everything on the POS Invoice path.
2. **Authoritative-document / consolidation ADR** (BF5). Blocks any hook or adapter design.
3. **Re-scope client-trust remediation as `pos_next`-only** (Gap C). Unblocks NexDine ahead
   of `pos_next`.
4. **Harness, adapted from `nexdine/.github/workflows/ci.yml`.** Independent of every ADR;
   start now, with the real cost acknowledged.
5. **Phase 2 fixes, plus N1/N2** (Gap D), which are free while the offer cache is inert.
6. **Split, KOT and stock-ownership ADRs** (BF2, BF3, BF4), including precision (Gap B).
7. **Phase gates and rollback criteria** (Gap E) before any live migration.

## Final assessment

The rejection of the companion architecture is correct. NexDine demonstrates the need,
but the destination is a generic commercial promotions capability usable by any channel.
The remaining design work is focused rather than exploratory:

- Resolve `POS Coupon` versus ERPNext `Coupon Code`.
- **Resolve which document is authoritative across POS Invoice consolidation.**
- Define the neutral calculation and entitlement contracts.
- Migrate ownership directly to `promotion_core` without an intermediate re-home.
- Add generic ERPNext document adapters.
- Put Promotions into NexDine's authoritative server flow before save, submit, and KOT.
- Preserve promotion identity through bill and table splitting, **and through consolidation**.
- Assign recipe/composite stock-consumption ownership.
- Correct the PR's factual overstatements and stale appendix.
- **Correct this review's own two unreproducible figures.**

After those changes, the design will be ready to produce coordinated implementation
plans for the neutral core, metadata migration, generic ERPNext integration, `pos_next`,
and NexDine.

---

*Validation performed 2026-09-08 against `nexdine@03ebd4b`, `pos_next` working tree,
`posnext_promotions@70be440` and ERPNext v15 on this bench. All findings are static-analysis
results; none has been reproduced against a live database.*
