# Generic Promotions Architecture

**Status:** Architecture decisions required · implementation not authorized
**Date:** 2026-09-08
**Scope:** `promotion_core` (proposed), `posnext_promotions`, `pos_next`, `nexdine`, `hospitality_core`, ERPNext v15
**Baselines:** `nexdine@03ebd4b` · `posnext_promotions@70be440` · `pos_next` working tree · ERPNext v15 on bench
**Evidence:** static source analysis. **No finding has been reproduced against a live database.** Every figure below carries its reproducing command.

---

## 1. Decisions

### 1.1 Decided

| # | Decision | Rationale |
|---|---|---|
| **D1** | **Promotions is a generic commercial capability, not a POS feature.** It must serve POS applications, ERPNext Desk documents, APIs, e-commerce and future channels. | NexDine proves the requirement; it is not the centre of it. |
| **D2** | **No promotions app may depend on a POS application.** Dependencies run from every sales channel *to* the promotions core. | `nexdine` does not use `pos_next` and never will. Binding promotions to `pos_next` would force a competing POS onto every NexDine outlet. |
| **D3** | **Extract the neutral core directly** as `promotion_core`. Do not re-home DocTypes to `posnext_promotions` first and extract later. | The intermediate step pays the live metadata migration twice. |
| **D4** | **ERPNext `Coupon Code` is the canonical coupon model.** `POS Coupon` is migrated and retired, not re-homed. | `POS Coupon` duplicates ERPNext concepts — identity, type, customer, validity, usage limits, linked Pricing Rule. Its bridge fields were never maintained. |
| **D5** | **Free composite/recipe items carry zero revenue, consume stock, and book normal COGS**, with provenance preserved. | Economically correct; `nexdine_recipe_stock` already behaves this way. |
| **D6** | **Offline: certified-subset only, receipts bind at the collected amount.** Coupons, one-time offers, gift pools, accumulative and stock-dependent gifts are online-only until a reservation design exists. | Preserving a price promised while disconnected is a different invariant from trusting arbitrary browser prices. |

### 1.2 Open — in dependency order

| # | Decision | Blocks | Owner |
|---|---|---|---|
| **O1** | **Logical transaction identity.** One definition spanning bill splits, POS Invoice consolidation, and the Sales Order → Sales Invoice chain. | F1, F2, F6 — and the redemption ledger's uniqueness key | Engineering |
| **O2** | **Per-document coupon support matrix.** Which selling documents support coupons; whether Quotation gains enforcement or loses the field. | F3, all adapter work | Product + Engineering |
| **O3** | **Coupon → Pricing Rule cardinality**, and the `disabled` mapping that follows from it. | F4, migration safety and runtime cost | Engineering |
| **O4** | **Authoritative document across consolidation** — which record owns promotion state. | F6, any hook or adapter design | Engineering |
| **O5** | **Stock-consumption ownership** when NexDine and `hospitality_core` are both installed; whether that combination is a supported product shape. | F7, CI matrix | Product |
| **O6** | **Deployment inventory.** Which sites run `posnext_promotions`. | containment vs. release-gate sequencing | Deployment owner |
| **O7** | **Coupon code uniqueness policy** for multi-company sites. | F9, migration | Product |
| **O8** | **Phase gates and rollback criteria.** | F10, any live migration | Engineering lead |

**O1 is the keystone.** Three of the highest-severity findings are one missing concept.

### 1.3 Deployment posture

Unverified. Repository history cannot establish deployment — Frappe apps are commonly installed from branches, and `posnext_promotions` is absent from this bench's `apps.txt`, which is weak evidence only.

> **Until O6 closes, treat `posnext_promotions` as potentially deployed.**

NexDine's README states it has run *"at scale, serving over 10+ outlets for the past 10 months"* — evidence that **NexDine** is live, not that promotions is installed at those outlets.

- **If deployed:** containment is Phase 0 and ships before the harness.
- **If confirmed undeployed:** containment becomes a release gate; the harness starts first.
- **Either way:** no release exposes coupons, one-time offers, gift pools or free gifts before server-side enforcement is complete.

---

## 2. Context

Two POS products and a promotions engine exist on one bench, with no shared promotions domain.

| App | Role | Invoice type | Promotions today |
|---|---|---|---|
| `pos_next` | POS, Vue frontend | Sales Invoice | own engine + `POS Coupon` + one-time ledger |
| `nexdine` | Restaurant ERP, React/TypeScript POS, KDS, analytics | **POS Invoice** | none beyond manual discounts and a TS totals engine |
| `posnext_promotions` | advanced promotions | Sales Invoice | GWP, Gift Pool, Accumulative, schedules, auth gate |
| `hospitality_core` | hotel PMS | POS Invoice + Sales Invoice | none |

Measured, with commands:

```
nexdine       git ls-files '*.py' | xargs cat | wc -l        →  27,543 across 368 files
nexdine       git grep -o '"POS Invoice"'   -- '*.py'        →  178
nexdine       git grep -o '"Sales Invoice"' -- '*.py'        →   18
duplication   function names present in both pos_next and posnext_promotions  →  39
duplication   pos_next LOC superseded by posnext_promotions →  2,201
```

`nexdine` declares `required_apps = ["hrms"]` and has **no imports, hooks, declared dependency or runtime calls** into `pos_next`.

**What NexDine already gets right, and `pos_next` does not.** `nexdine_order.py:1395-1425` clears `invoice.items` and rebuilds every row from server-side `Item Price`, throwing when no price exists; NexDine never sets `ignore_pricing_rule`. `pos_next/api/invoices.py:786` sets `ignore_pricing_rule = 1` and `:839` comments *"Trust frontend's price_list_rate"*.

> **Scope consequence:** client-trust remediation is a **`pos_next` defect, not a generic redesign**. NexDine already satisfies the invariant and does not need to wait for it.

---

## 3. Target architecture

### 3.1 The abstraction

> Evaluate a commercial basket in an authoritative context, produce deterministic benefits and obligations, and manage scarce entitlements through an auditable lifecycle.

The core knows nothing of Vue, React, KOTs, tables, rooms, or any invoice controller. It receives normalized input and returns a decision. Adapters materialize that decision into their documents and fulfilment flows.

### 3.2 Contracts

```text
PromotionContext
  tenant: company, currency, jurisdiction
  channel: desk, pos_next, nexdine, ecommerce, api, other
  actor: user, role, device, session
  customer: identity, groups, territory, segments
  location: branch, warehouse, outlet
  transaction: type, timestamp, price list, source document, LOGICAL ORDER ID
  capabilities: online, offline snapshot, stock reservation, authorization

Basket
  stable line identity
  item, UOM, quantity, authoritative base price
  item group, brand and attributes
  existing discount state
  parent/modifier/component relationships
  tax and charge classification

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

Channel-specific data travels in a namespaced extension object. Generic rule code must never import a consumer application to interpret it; a rule depending on an extension declares that requirement and becomes ineligible on channels that do not supply it.

**The decision is data, not a mutated Frappe document.** Each adapter must prove the document it saves represents that decision exactly.

### 3.3 Lifecycle

```text
evaluate → quote → reserve → consume → reverse
                ↘ expire
```

- **Evaluate** — pure, repeatable.
- **Quote** — binds a result to normalized inputs and rule versions for a limited time.
- **Reserve** — atomically claims scarce coupon, gift, one-time, budget or stock capacity.
- **Consume** — attaches the reservation to the authoritative submitted transaction.
- **Reverse** — idempotent compensating event; never erases history.
- **Expire** — releases an unused reservation safely.

Deterministic discounts may need no reservation, but still carry a calculation identity and explanation trace.

### 3.4 Ports

Catalog and authoritative base pricing · customer and purchase history · rule and schedule storage · inventory availability and optional reservation · entitlement ledger and atomic uniqueness · authorization policy and grant verification · tax and charge classification supplied by the consumer · clock, currency precision and rounding policy · audit, telemetry and reconciliation.

ERPNext implementations of these ports may live in the core. Restaurant, offline and UI orchestration stays in adapters.

### 3.5 Ownership

```text
ERPNext
└── promotion_core
    ├── ERPNext document adapters
    ├── pos_next adapter
    ├── nexdine adapter
    └── future channel adapters

HRMS
└── nexdine

posnext_promotions
└── migration source, optional management UI
```

| Owner | Responsibilities |
|---|---|
| **ERPNext** | `Coupon Code` identity, type, customer, validity, `maximum_use`, linked Pricing Rule; Pricing Rule discount mechanics, scope, conditions; POS Invoice's native `coupon_code`; native validation and submit/cancel lifecycle |
| **`promotion_core`** | eligibility and calculation semantics; immutable entitlement ledger; quote/reserve/consume/reverse; offline bundle contracts; explanation traces and calculation-version persistence; authoring permissions and audit; **extensions to native DocTypes only where ERPNext lacks a capability** |
| **ERPNext adapters** | Quotation, Sales Order, Delivery Note, Sales Invoice, POS Invoice lifecycle translation; document normalization and persistence checks; submit/cancel/amend/return → entitlement events; **consolidation provenance and hook suppression (F6)** |
| **`pos_next`** | Vue checkout; Sales Invoice construction and authoritative acceptance; **remediation of `ignore_pricing_rule` and frontend price trust**; offline queue and device policy; cart → provider translation |
| **`nexdine`** | React checkout; POS Invoice construction and authoritative acceptance; branch/order-type/table/room/add-on/aggregator context; KOT from final authoritative lines; recipe stock; split allocation; offline queue and conflict UI |
| **`hospitality_core`** | hotel composite consumption, subject to O5 |

Neither POS application depends on or imports the other. The core imports neither.

The core exposes **stable service contracts**, not whitelisted functions named after one consumer. Compatibility endpoints may delegate during migration but must not become the permanent domain API.

### 3.6 The reuse rule

```text
use native ERPNext behavior unchanged
  → extend native DocTypes when a field or policy is missing
  → adapt native services to additional document types
  → add a supplemental record only for a capability ERPNext truly does not model
```

The burden of proof falls on every new coupon-related table, field, endpoint and counter. A supplemental `Promotion Redemption` record is permitted **only** for per-customer uniqueness, logical-order and split identity, idempotency and cross-document audit. It is **prohibited** from becoming a second coupon definition or a second mutable usage counter.

Using ERPNext natively does not mean inheriting its defects — see F5.

---

## 4. Finding register

Severity-ranked. Every finding carries evidence, the failure it produces, and the required response. Confidence is **confirmed** (re-derived from source) unless stated.

| # | Finding | Sev | Blocks |
|---|---|---|---|
| F1 | No logical transaction identity — splits, consolidation and SO→SI all lose or double-count entitlements | **HIGH** | O1 |
| F2 | Adapter contract missing; KOT is built from the client item list | **HIGH** | O1, O2 |
| F3 | Native coupon support covers 2 of 5 selling documents; Quotation is decorative | **HIGH** | O2 |
| F4 | Coupon → Pricing Rule cardinality unstated; migration can share rules destructively | **HIGH** | O3 |
| F5 | ERPNext's native coupon services carry three defects | **HIGH** | — |
| F6 | Consolidation destroys discount provenance and re-fires hooks | **HIGH** | O1, O4 |
| F7 | Recipe/composite stock-consumption ownership undefined | **HIGH** | O5 |
| F8 | Currency precision and split allocation unspecified | MEDIUM | O1 |
| F9 | `coupon_code` is globally unique with no company scope | MEDIUM | O7 |
| F10 | No phase gates or rollback criteria | MEDIUM | O8 |
| F11 | NexDine offline offer cache: NULL dates excluded, no eligibility scope | LOW | — |
| F12 | `Coupon Code` carries `amended_from` but is not submittable | LOW | — |

Defects internal to `posnext_promotions` are catalogued separately in `CODE_REVIEW.md` and scheduled in Phase 2.

---

### F1 — No logical transaction identity · HIGH

**One missing concept, three manifestations.** Each was found separately; all three resolve together.

**(a) Bill splitting.** `nexdine_order.py:1003-1021` `_copy_item_fields` copies 15 fields — `item_code, item_name, qty, rate, price_list_rate, base_price_list_rate, comment, custom_course, cost_center, custom_is_addon, custom_addon_group_id, custom_addon_group, custom_addon_group_name, custom_addon_surcharge, custom_is_default_selection`. **None** is `is_free_item`, `pricing_rules`, `discount_percentage` or `discount_amount`. Splitting destroys promotion provenance; two siblings may each attempt to consume a one-use coupon.

**(b) POS Invoice consolidation.** POS Invoice Merge Log creates a Sales Invoice (`pos_invoice_merge_log.py:350`, recorded at `:160`). `posnext_promotions` registers `record_one_time_offer_usage` on Sales Invoice `on_submit`, so a redemption is recorded at POS Invoice submit **and again** at consolidation. Idempotent today only by accident — composite autoname plus `ignore_if_duplicate=True`.

**(c) Sales Order → Sales Invoice.** ERPNext's Sales Order already increments `used` on submit (`sales_order.py:448`) and decrements on cancel (`:482`). Adding Sales Invoice counting, as §3.5 requires, double-counts:

| Flow | Consumption events |
|---|---|
| Quotation → SO → SI | SO submit + SI submit = **2** for one sale |
| Direct SI, no SO | 1 |
| POS Invoice → Merge Log → consolidated SI | **2** |

ERPNext has no answer because it never implemented Sales Invoice.

**Required.** Consumption keys on the **logical transaction**, never on a document. Define once: a logical promotion transaction id created at first evaluation, carried onto every derived document, with the redemption ledger unique on `(coupon, logical_transaction)`. Then:

- Evaluate against the logical original order before a split commits; freeze the calculation and allocate its monetary and free-item effects to siblings.
- Consume coupon, one-time, gift-pool and cross-cart entitlements **once per logical transaction**.
- Copy promotion provenance whenever invoice rows move.
- Recalculate or reject when a split changes the qualifying basket, or when a sibling's customer differs from the customer whose eligibility produced the promotion.
- Prevent a sibling's independent cancellation from reversing a group entitlement.
- Define how returns against one sibling restore quantities and entitlements.

### F2 — Adapter contract missing; KOT built from the client item list · HIGH

Registering identical lifecycle hooks on Sales Invoice and POS Invoice is necessary but insufficient. Every consumer has a different document-construction, pricing, fulfilment and settlement pipeline.

NexDine's sequence (`nexdine_order.py:609-745`): resolve invoice and price list → parse client selection → **clear and rebuild rows from server Item Prices** → materialize taxes and shipping → validate sellable and recipe stock → save → optionally settle and submit → generate KOT.

The defect is the last step. `nexdine_order.py:745`:

```python
_run_kot_with_failure_surface(inv, customer, table, items, past_item, comments)
```

`items` is the **client** list, passed alongside the authoritative `inv`. A promotion-added free dish exists on the invoice and consumes recipe stock but **never reaches the kitchen**. Hook parity cannot reach this — it is a fulfilment defect, not an accounting one.

**Required NexDine orchestration:**

```text
resolve context and authoritative base prices
  → evaluate promotions
  → materialize discounts and free lines
  → validate sellable and recipe stock
  → save draft and persist calculation identity
  → atomically reserve scarce entitlements before submit
  → submit and finalize entitlement consumption
  → generate KOT from the authoritative final-line delta
  → carry calculation identity and provenance through consolidation (F6)
```

Adapter context must carry: company, currency, posting time, POS Profile, price list, warehouse; customer, customer group, and the anonymous/default-customer distinction; branch, restaurant, room, table, waiter, cashier; order type (Dine In, Take Away, Delivery, Aggregator); add-on parent, group, selection, surcharge and default-selection identity; stable client and authoritative server line identity; offline snapshot, device, cashier and sequence identity; split group, source invoice and logical order identity.

The core must not import NexDine. NexDine owns the translation.

### F3 — Native coupon support covers 2 of 5 selling documents · HIGH

"Use ERPNext where complete" is the right rule. ERPNext is complete on **two** selling documents, absent on two, and **inconsistent on a third**:

| DocType | `coupon_code` field | Native validation | Native counting |
|---|---|---|---|
| Sales Order | ✅ | ✅ `sales_order.py:233` | ✅ `:448` used / `:482` cancelled |
| POS Invoice | ✅ | ✅ `pos_invoice.py:230` | ✅ `:252` used / `:289` cancelled |
| **Quotation** | ✅ | ❌ none | ❌ none |
| **Sales Invoice** | ❌ | ❌ | ❌ |
| **Delivery Note** | ❌ | ❌ | ❌ |

Quotation carries the field with no enforcement anywhere — its only occurrence of the word is the type annotation `coupon_code: DF.Link | None` (`quotation.py:62`). A coupon on a Quotation **displays, is never validated, is never counted**. That actively misleads whoever sets it.

This is reachable today: `posnext_promotions` already registers `apply_min_max_price_discounts` on Sales Order, Quotation and Delivery Note.

**Required.** State per selling document whether coupons are supported, and for each supported one whether native services are invoked or the adapter supplies them. Sales Invoice should gain a **Link to `Coupon Code`**, not the current `Data` field. Quotation must gain enforcement or lose the field — leaving it decorative is not a decision.

### F4 — Coupon → Pricing Rule cardinality unstated · HIGH

`Coupon Code.pricing_rule` is `Link` → `Pricing Rule`, **`reqd: 1`, `unique: 0`**. N coupons may share one Pricing Rule.

`Coupon Code` has **no `company` and no `disabled` field** — full field list: `coupon_name, coupon_type, customer, coupon_code, pricing_rule, valid_from, valid_upto, maximum_use, used, description, amended_from`. So both must map onto the Pricing Rule, and migration step "create **or match** its native Pricing Rule" **shares** it. Then:

- Disabling one migrated coupon **disables every coupon sharing that rule**.
- Company scope is shared, so two coupons cannot target different companies on one rule.

Creating one rule per coupon avoids that but yields a **Pricing Rule explosion** — N legacy coupons become N rules, each evaluated by ERPNext's engine on every cart, on a live system.

**Required.** State the cardinality. If rules are shared, `disabled` cannot map to `Pricing Rule.disable` and needs a small `Coupon Code` extension field. If per-coupon, the migration must report the resulting rule count and its evaluation cost before it runs.

### F5 — ERPNext's native coupon services carry three defects · HIGH

Adopting the native model (D4) is correct. Inheriting its defects is not.

**(a) Lost update.** `erpnext/accounts/doctype/pricing_rule/utils.py:756-772`:

```python
coupon = frappe.get_doc("Coupon Code", coupon_name)
if coupon.used < coupon.maximum_use:
    coupon.used = coupon.used + 1
    coupon.save(ignore_permissions=True)
```

Read-modify-write, no `for_update`, no atomic conditional `UPDATE`. Two tills redeeming the final allowance both read the same value and write the same increment. **This is the counter NexDine relies on today**, so the same defect exists in three implementations — the dead copy at `posnext_promotions/api/coupon_engine.py:437`, the live one at `pos_next/.../pos_coupon.py:181`, and ERPNext core.

Secondary: `used` is declared `read_only` yet written via `coupon.save()`, running full document validation and `on_update` hooks on every redemption. An atomic conditional `UPDATE` removes that cost too.

**(b) `maximum_use = 0` validates, then throws at submit.** The two native functions disagree:

```python
# validate_coupon_code — utils.py:752
elif coupon.maximum_use and coupon.used >= coupon.maximum_use:   # 0 is falsy → SKIPPED
# update_coupon_code_count — utils.py:760
if coupon.used < coupon.maximum_use:                             # 0 < 0 is False → else
else:
    frappe.throw(_("{0} Coupon used are {1}. Allowed quantity is exhausted"))
```

The cashier is told the coupon is valid, then the sale fails at submit. **Late failure is the defect**, not the rejection itself.

**(c) Gift Card customer binding is never enforced — and adopting native is a regression.** `validate_coupon_code` (`utils.py:746-753`) checks only `valid_from`, `valid_upto` and `maximum_use`. `Coupon Code.customer` exists and is **never read**. A Gift Card assigned to Customer A is redeemable by Customer B.

> `posnext_promotions/api/coupon_engine.py:48-51` **enforces this today.** Migrating to native `Coupon Code` therefore **removes an existing control**. This is a regression to prevent, not merely a native gap to fill.

**Required.** Atomic conditional `UPDATE` with the redemption ledger as authority and `used` demoted to a projection; characterize and correct `maximum_use = 0`; enforce customer binding in the adapter before value is granted.

### F6 — Consolidation destroys discount provenance and re-fires hooks · HIGH

On the POS Invoice path the durable accounting document is **not** the POS Invoice. `merge_pos_invoice_into` (`pos_invoice_merge_log.py:234-238`):

```python
item.rate = item.net_rate
item.amount = item.net_amount
item.base_amount = item.base_net_amount
item.price_list_rate = 0
```

`price_list_rate` is **zeroed** and the discount baked into `rate`. The consolidated Sales Invoice retains no record of list price or discount applied, so *"every applied discount is explainable from a stored calculation version"* is **unachievable on that path** unless calculation identity crosses the merge explicitly.

Hook re-firing is covered in F1(b). Third effect: `apply_min_max_price_discounts` silently no-ops on consolidated invoices because `_apply_discount` (`posnext_promotions/overrides/pricing_rule.py:783`) returns early when `base_rate <= 0` — correct behaviour reached by accident, undocumented, one refactor from becoming a double discount.

**Required.** Decide which record owns promotion state; define how calculation identity, `is_free_item`, rule provenance and coupon allocation survive the merge; enforce hook suppression rather than relying on accident; define how `consolidated_credit_note` returns reverse entitlements exactly once; state whether audit rows reference the POS Invoice, the consolidated invoice, or both.

### F7 — Recipe/composite stock-consumption ownership undefined · HIGH

`nexdine_recipe_stock.before_submit` is **not** a component expander — it creates a separate Material Issue Stock Entry for recipe ingredients. `hospitality_core` uses a different mechanism. Calling both "expanders" hides the real risk.

**Required, as stock-consumption ownership:** which app owns consumption per sellable item; whether one item can be configured in both systems; how exactly one owner is selected when both are installed; whether NexDine + `hospitality_core` is a supported shape or merely possible; how free-promotion provenance reaches Stock Entry rows and profitability reporting; how return, cancellation and failed-submit reversals stay idempotent; how serial, batch, UOM, warehouse and negative-stock policies are preserved.

If both are supported, the invariant is **one economic inventory consumption**, not "components expanded once". If not supported, remove the combination from the CI matrix and declare the incompatibility.

**Free prepared item policy (D5) in full.** Appears on the KOT exactly once; consumes its recipe exactly once; zero customer revenue; normal COGS; removed or cancellation-KOTed correctly when the promotion is withdrawn; applied rule and qualifying parents preserved for audit. Paid modifiers do not become free because their parent is free, and add-ons do not independently qualify as paid basket items unless a rule allows it. The evaluator needs explicit flags for whether add-on surcharges contribute to minimum amount, whether add-on quantities contribute to minimum quantity, whether a free parent includes default or paid modifiers, whether a free product can be independently customized, and whether a free dish added after the first KOT requires an incremental KOT.

### F8 — Currency precision and split allocation unspecified · MEDIUM

`posnext_promotions/overrides/pricing_rule.py:825-834` `_rate_precision` returns a hardcoded `2` for any payload without a `precision()` method — wrong for 3-decimal currencies (KWD, BHD, OMR, TND). F1 requires allocating a frozen monetary effect across siblings, which is a rounding-allocation problem that must sum exactly to the original.

**Required.** Resolve precision from document currency; state the allocation rule (largest-remainder or equivalent); test that sibling totals reconcile to the parent to the last minor unit in a 3-decimal currency.

### F9 — `coupon_code` globally unique, no company scope · MEDIUM

`Coupon Code.coupon_name` and `coupon_code` are both **UNIQUE**, and the DocType has **no company field**. Two companies on one site cannot both issue `SUMMER25`. Migration collision detection is necessary but is a mechanic, not a policy.

**Required.** Choose: namespace codes per company at migration, accept global uniqueness as a declared product constraint, or add a company extension with composite uniqueness.

### F10 — No phase gates or rollback criteria · MEDIUM

The delivery plan defines a destination and a sequence but not what makes a phase *fail* or how to reverse a landed one. NexDine is live at 10+ outlets and the plan includes live metadata migration between apps.

**Required, per phase:** preconditions, an explicit abort trigger, a reversal procedure tested in CI, and a named decision-maker. Phase 7 additionally needs a stated difference-rate threshold above which enforcement does not proceed.

### F11 — NexDine offline offer cache · LOW

`nexdine/nexdine/api/nexdine_offline.py:357-377` `_fetch_active_offers` filters `valid_from <= today AND valid_upto >= today`, so a Pricing Rule with `NULL valid_upto` — an open-ended promotion, the common case — is silently excluded. The payload also returns only `name, title, apply_on, rate_or_discount, discount_percentage, rate`: no item, group, brand, customer, company, quantity, amount, stacking or schedule scope, so no scoped rule can be evaluated offline.

**Latent, not a live checkout defect.** `db.offers` is inert — `pos/src/lib/offline/preload.ts:319-328` only `clear()`s and `bulkPut()`s it; the sole other references are schema declarations at `pos/src/lib/offline/db.ts:160,247,327`. No read consumer exists.

**Consequence: these are free to fix now.** Correcting the filter and widening the payload cannot regress behaviour nothing consumes. Schedule in Phase 2, not behind the offline-bundle design.

**Do not** wire `posnext_promotions.api.offers.get_offers` straight into this cache — it would cache rules D6 declares online-only. Publish a certified bundle instead (§5.2).

### F12 — `Coupon Code` submittability · LOW

`coupon_code.json` declares `is_submittable: 0` while retaining `amended_from`. Vestigial today; becomes a real question if the Promotion Approver role (§6) introduces a coupon approval workflow.

---

## 5. Coupon migration

### 5.1 Legacy `POS Coupon` → native `Coupon Code`

Map each concept to its existing native owner. Do not copy native fields into a new DocType.

| Legacy `POS Coupon` | Native destination |
|---|---|
| `coupon_name`, `coupon_code`, `coupon_type`, `customer` | `Coupon Code` |
| `valid_from`, `valid_upto`, `maximum_use`, `used` | `Coupon Code` |
| `pricing_rule` | `Coupon Code.pricing_rule` |
| `disabled` | Pricing Rule `disable` — **or a `Coupon Code` extension, per O3** |
| `company`, `campaign` | Linked Pricing Rule applicability |
| `discount_type`, percentage, amount | Linked Pricing Rule rate/discount fields |
| `min_amount`, `max_amount`, `apply_on` | Linked Pricing Rule thresholds |
| `one_use` | Small `Coupon Code` extension + redemption-ledger uniqueness |
| referral relationship | Generic referral integration or consumer adapter — not Coupon Code duplication |
| excluded items/groups/brands | Pricing Rule scope/exclusion extensions |

Per legacy coupon:

1. Create or match one native `Coupon Code` via a deterministic migration key.
2. Create or match its Pricing Rule — **subject to O3; matching shares the rule (F4)**.
3. Map percentage/amount and eligibility onto the Pricing Rule, not onto Coupon Code.
4. Map customer-bound gift cards to native `customer` and `coupon_type`.
5. Preserve original identifiers and submitted-document references in migration/audit fields.
6. Reconcile legacy `used` against submitted source documents. **Do not copy an unverified counter.**
7. Detect code/name collisions and **abort with a report** — never silently rename (F9).
8. Run in shadow/read compatibility mode for one release if deployed data requires it.
9. Remove legacy write paths, then remove `POS Coupon`, its controller, API CRUD, permission strings, fixtures and workspace links after the rollback window closes.

One coupon table and one coupon definition remain: ERPNext `Coupon Code`.

**Before removing anything from `pos_next`:** `POS Offer` is **not** dead — it is referenced from `pos_next/pos_next/doctype/pos_coupon/pos_coupon.js` and `pos_next/pos_next/workspace/posnext/posnext.json`. `POS Offer Detail` and `POS Coupon Detail` appear unreferenced but still require row counts, Dynamic Links, fixtures, reports, exports, integrations and customer scripts to be checked first.

### 5.2 Coupon identity at API boundaries

ERPNext names a `Coupon Code` document from `coupon_name` (`autoname: field:coupon_name`, **UNIQUE reqd**), while the human-entered or scanned value lives in a separate **UNIQUE** `coupon_code` field. Existing ERPNext tests commonly make these identical. **Adapters must never assume it.**

- Document fields and ledger references carry the native **document name**.
- One public `resolve_coupon(code, context)` service accepts the scanned code, finds the unique document, and returns its name **only after native validation plus the customer-binding check F5(c) requires**.
- Clients never select or forge a Pricing Rule as proof of coupon eligibility.
- Responses may echo the display code; calculations and persistence bind to the document name and Pricing Rule version.

### 5.3 Offline bundle

`promotion_core` publishes a versioned bundle containing **only certified deterministic rules**: snapshot id, immutable hash, issue time, expiry, evaluator version; company, POS Profile, price list, currency, taxes, rounding, warehouse scope; full item/group/brand/customer/order-type eligibility; quantity, amount, stacking, priority and exclusion semantics; an explicit offline-safe capability marker; manual discount and rate-override ceilings.

NexDine's existing TypeScript totals engine consumes the bundle and runs shared golden conformance vectors against the server evaluator.

Coupons, one-time rules, gift pools, accumulative rules and stock-dependent free gifts stay **online-only** until a reservation or bounded-risk design exists (D6).

---

## 6. Authorization

Authorization is a **generic policy service**, not a Sales Invoice hook. The core evaluates protected commercial actions and verifies a grant; each adapter supplies the channel-specific action and binding context. A grant issued for one channel, document, action or calculation must never authorize another.

Common action vocabulary: manual discounts, price overrides, returns, voids, complimentary items, promotion exceptions, entitlement overrides, policy changes. Consumers add namespaced actions without putting UI or workflow code in the core.

Adding the gate to POS Invoice `before_submit` is **not** sufficient for NexDine, which accepts manual `additional_discount_percentage`, shipping overrides, returns, voids and split operations before submission. The NexDine policy must classify: manual header discount; manual rate or price override; shipping/delivery fee override; return with and without a source invoice; void after KOT; removal of a prepared item; complimentary item not generated by a promotion; bill or table split after a promotion is frozen; customer replacement after a customer-bound promotion is quoted.

Grants stay user-, session-, action-, document- and calculation-bound, single-use, time-limited and audited. **Redis failure must deny PIN approval**, or use an explicitly designed database-backed fallback.

**Authoring authority.** `Promotional Scheme.write` is the wrong authority for creating commercial liabilities from a POS session. Classify all whitelisted endpoints as read-only, calculation, mutation, administrative or lookup before threat-modelling; then apply a role model of Promotion Viewer (inspect, simulate) · Editor (create and edit drafts) · Approver (approve and activate) · Administrator (cancel, archive, exceptional changes) · Cashier (apply only). With company-level user permissions, separate checks per DocType, allowlisted mutable fields, before/after audit logging, approval required for **activation** rather than creation, offline snapshot invalidation when an active rule changes, and rate-limited mutation endpoints.

---

## 7. Verification

### 7.1 Harness

`nexdine/.github/workflows/ci.yml` is a working Frappe v15 harness — MariaDB 10.6, Redis 7, Python 3.12, Node 20, `bench init --frappe-branch version-15`, sequential app installs, `bench run-tests --app`, plus a frontend job. It passed at `03ebd4b` (`2026-07-08 14:53:31 +0300`).

It retires the question of **how to construct** a harness. It does not retire the verification blocker cheaply. Still required: authenticated checkout of private repositories; immutable SHAs per matrix; clean standalone installation; upgrade installation from pre-migration versions; **exact discovered and executed test counts**; MariaDB-backed concurrent redemption tests; Sales Invoice and POS Invoice lifecycle tests; consolidation tests asserting hooks fire once and provenance survives (F6); `pos_next` Vue and NexDine React conformance tests; alternate app ordering where composition is supported; clean uninstall/reinstall and failed-migration recovery.

"Command exited successfully" is insufficient when 227 tests sit in four directories.

| Matrix | Purpose |
|---|---|
| ERPNext + `promotion_core` | Canonical platform install and Desk flows |
| ERPNext + core + `pos_next` | Sales Invoice consumer |
| ERPNext + HRMS + core + `nexdine` | POS Invoice consumer |
| Upgrade: `pos_next`-owned metadata → core-owned | Re-home safety |
| Upgrade: legacy coupon data → native `Coupon Code` | Migration safety |
| Both POS apps | Only if officially supported |
| `nexdine` + `hospitality_core` | Only if O5 declares it supported |

Do not combine the 59-file formatting cleanup with money-path changes. Establish a formatting baseline separately, then enforce it.

### 7.2 Conformance vectors — every adapter

- Identical normalized basket, context, rule versions, precision and engine version produce an identical decision in every channel.
- Disabled, expired, future, wrong-company, wrong-customer, wrong-channel and wrong-location rules are rejected **even when named by the client**.
- Client totals, discount amounts, eligibility claims, free rows and rule IDs cannot alter the authoritative result.
- Priority, exclusivity, stacking, maximum discount and rounding share golden vectors.
- Concurrent final coupon, one-time, gift and budget allowances yield **exactly one** winner.
- Retried quote, reserve, consume, reverse, cancel and return are idempotent.
- Failure of Redis, inventory, history, authorization or entitlement services **cannot grant value**.
- Every applied benefit is reconstructable from stored input, rule versions, engine version and explanation trace.
- Unsupported channel capabilities make a rule ineligible with an explicit reason — never an implicit fallback.
- A core release runs contract tests against every supported adapter before rollout.

### 7.3 Adversarial vectors — per finding

| Vector | Finding |
|---|---|
| Quotation → SO → SI consumes the coupon **exactly once** | F1(c) |
| POS Invoice → consolidation records the redemption **exactly once** | F1(b), F6 |
| A by-items split neither duplicates nor loses free-item or discount provenance | F1(a) |
| Equal/custom payment splits consume one logical coupon once | F1(a) |
| Changing customer during a split cannot retain an ineligible customer-bound benefit | F1(a) |
| A free prepared item appears on invoice, KOT, recipe Stock Entry, receipt and audit exactly once | F2, F7 |
| A free item added after the first KOT produces the correct incremental KOT | F2 |
| Removing or cancelling a free prepared item produces the correct kitchen cancellation | F2, F7 |
| Paid add-ons stay paid when the parent is free; add-ons do not independently qualify | F7 |
| A coupon on a Quotation either enforces or is absent — never accepted-and-uncounted | F3 |
| Disabling one migrated coupon disables no other coupon | F4 |
| A `maximum_use = 0` coupon behaves consistently at validation **and** submit | F5(b) |
| A Gift Card bound to Customer A is rejected for Customer B on every supported document | F5(c) |
| A coupon applied to POS Invoice does not collide with native `Coupon Code` validation | F3, F5 |
| The consolidated Sales Invoice can still explain every discount despite `price_list_rate = 0` | F6 |
| A return or group cancellation reverses the entitlement and recipe stock exactly once | F1, F7 |
| Split sibling totals reconcile to the parent to the last minor unit in a 3-decimal currency | F8 |
| Migration aborts with a report on a code collision rather than renaming | F9 |
| Dine In, Take Away, Delivery and Aggregator orders receive only eligible rules | F2 |
| Branch, restaurant, room, price-list, company and warehouse scope cannot bleed | F2 |
| Offline-unsafe rules are unavailable **before** the cashier promises them | D6 |
| A tampered offline outcome is quarantined, never silently accepted or repriced | D6 |
| Every hook executes once regardless of `apps.txt` order | F6 |

---

## 8. Delivery

Parallel workstreams, not a linear sequence. **Every phase carries the gates F10 requires: preconditions, abort trigger, tested reversal, named decision-maker.**

| Phase | Content | Depends on |
|---|---|---|
| **0 — Facts and containment** | Inventory every managed and customer-managed site: installed apps, commit SHAs, promotion settings, coupon data, submitted promotion-bearing invoices, real consumer paths. If promotions is live, disable coupons, one-time offers, gift pools and free gifts until server enforcement exists; otherwise gate them for release. | — |
| **1 — Harness and characterization** | Adapt NexDine's CI. Publish exact test counts. Characterize current Sales Invoice and POS Invoice behaviour before refactoring. Add failing tests for confirmed fail-open defects. | **nothing — start now** |
| **2 — Independent containment fixes** | One-time key lookup and semantic uniqueness; remove client-named rule readmission; coupon/redemption failures deny value; submit-time gift-stock validation; PIN lockout fails closed; replace destructive install behaviour; make the ERPNext patch observable; extend authorization to POS Invoice where the action model is clear; **fix F11 now — the offer cache has no read consumer**. | Phase 1 |
| **3 — Close the ADRs** | O1–O5, O7. Establish `promotion_core` and its boundary directly. Design versioned metadata and data migrations. | Phase 1 |
| **4 — Provider and ledger** | One authoritative evaluator; calculation identity and explanation trace; quote/reserve/consume/reverse/offline-bundle; atomic constraints and idempotency keys. **This repairs the live ERPNext counter defect (F5a), not merely `pos_next`'s.** Remove import-time monkey-patching and installed-app branching. | Phase 3 |
| **5 — Consumer adapters** | Generic ERPNext document adapters. `pos_next` Sales Invoice adapter **including its `ignore_pricing_rule` remediation**. NexDine POS Invoice adapter — **does not depend on the `pos_next` trust-boundary work and may run in parallel**. KOT, recipe, add-on, split, return, cancel and authorization behaviour. Remove duplicate endpoints, hooks, globals and engine code from `pos_next`. | Phase 4 |
| **6 — Offline certified subset** | Versioned snapshots and shared conformance vectors. Only explicitly certified deterministic classes. Quarantine mismatches; preserve the amount collected. Expand one capability at a time. | Phase 5 |
| **7 — Shadow and outlet rollout** | Shadow-evaluate old and new without changing charged totals. Classify every difference as rounding, stale data, evaluator mismatch or attempted manipulation. Pilot one controlled NexDine outlet, then expand with rollback, reconciliation and profitability reporting. | Phase 6 |

Temporary fixes may live in two repositories only while the duplicated implementation is deployed. Delete the `pos_next` copy as soon as the consolidated consumer ships.

---

## 9. Definition of ready for implementation plans

- [x] Canonical coupon model chosen — **D4**
- [x] Neutral core ownership and package boundary accepted, no deferred re-home — **D3**
- [x] Composite/recipe free-item accounting policy — **D5**
- [x] Offline capability and commercial-binding policy — **D6**
- [ ] **Logical transaction identity defined once, covering split, consolidation and SO→SI — O1**
- [ ] Per-document coupon support declared; Quotation resolved — **O2**
- [ ] Coupon → Pricing Rule cardinality declared, with the `disabled` mapping that follows — **O3**
- [ ] Authoritative document across consolidation — **O4**
- [ ] Recipe/composite stock-consumption ownership and supported combinations — **O5**
- [ ] Deployment inventory determines containment vs. release gate — **O6**
- [ ] Coupon code uniqueness policy for multi-company — **O7**
- [ ] Phase gates and rollback criteria — **O8**
- [ ] Generic input, output, lifecycle, capability and port contracts accepted — §3.2–3.4
- [ ] Standard ERPNext document-adapter coverage accepted — §3.5
- [ ] NexDine pricing/KOT/recipe orchestration contract accepted — F2
- [ ] Bill-split allocation policy including precision — F1, F8
- [ ] Add-on and free-prepared-item policy accepted — F7
- [ ] CI matrix has named repositories, branches/SHAs and test ownership — §7.1

Implementation plans then split into **core**, **migration** and **consumer-adapter** plans sharing one contract and one set of conformance vectors. No repository independently invents promotion eligibility, redemption or calculation semantics.

---

## Appendix A — Evidence catalogue

Every non-obvious claim, with its source. Commands are reproducible on this bench.

| Claim | Source |
|---|---|
| `nexdine` size | `git ls-files '*.py' \| xargs cat \| wc -l` → 27,543 / 368 files |
| `nexdine` invoice bias | `git grep -o '"POS Invoice"' -- '*.py'` → 178; `'"Sales Invoice"'` → 18 |
| `nexdine` independence | `nexdine/hooks.py:11-12` `required_apps = ["hrms"]`; 4 files mention `pos_next`, none an import, hook or dependency |
| `nexdine` frontend | `pos/package.json` react ^19.0.0, zustand ^5.0.6, dexie ^4.4.2, vite ^6.2.0, vitest ^3, typescript ~5.7.2; `kotdisplay/package.json` vue ^3.3.4 |
| `nexdine` server-authoritative pricing | `nexdine_order.py:1395-1425`; `git grep -n ignore_pricing_rule -- '*.py'` → no match |
| `pos_next` client trust | `pos_next/api/invoices.py:786` `ignore_pricing_rule = 1`; `:839` "Trust frontend's price_list_rate" |
| Duplication | 39 shared function names; 2,201 LOC in `pos_next/api/offers.py`, `api/promotions.py`, `overrides/pricing_rule.py`, `doctype/pos_coupon/pos_coupon.py` |
| `Coupon Code` schema | `autoname: field:coupon_name`; `coupon_name` UNIQUE reqd; `coupon_code` UNIQUE; `pricing_rule` Link reqd **not unique**; no `company`, no `disabled`; `is_submittable: 0` with `amended_from` |
| Native coupon coverage | SO `sales_order.py:233,448,482`; POS Invoice `pos_invoice.py:230,252,289`; Quotation field only (`quotation.py:62`); Sales Invoice `grep -c coupon` → 0; Delivery Note no field |
| Native counter defects | `pricing_rule/utils.py:746-753` validation, `:756-772` counting |
| Existing customer-binding control | `posnext_promotions/api/coupon_engine.py:48-51` |
| Consolidation | `pos_invoice_merge_log.py:160,234-238,350` |
| KOT from client list | `nexdine_order.py:745` |
| Split field copying | `nexdine_order.py:1003-1021` |
| Offline cache inert | `pos/src/lib/offline/preload.ts:319-328`; `pos/src/lib/offline/db.ts:160,247,327` |
| Offline NULL-date filter | `nexdine/nexdine/api/nexdine_offline.py:357-377` |
| `POS Offer` still referenced | `pos_next/.../pos_coupon/pos_coupon.js`; `pos_next/pos_next/workspace/posnext/posnext.json` |
| Redemption autoname | `one_time_customer_offer_usage.json:3` `format:{customer}:{pricing_rule}`; `frappe/model/naming.py:565-583` returns the formatted name verbatim |
| NexDine CI | `.github/workflows/ci.yml`; `git log -1 --date=iso 03ebd4b` → 2026-07-08 14:53:31 +0300 |

## Appendix B — Review provenance

This document supersedes the review-and-rebuttal exchange it grew from. Recorded so conclusions can be audited.

**Corrections applied to `BrainWise-DEV/Promotions` PR #1.** NexDine's POS is React/TypeScript, not Vue. LOC and invoice-reference figures were produced by a broken command (`find … -print -exec cat`, feeding filenames into the count, no `node_modules` exclusion) and are replaced by the published-method values in Appendix A. "Exactly one mention of `pos_next`" → "no imports, hooks, declared dependency or runtime calls". "No promotions surface at all" → "no dedicated promotions engine or authoring model". "Does nothing on NexDine" → "provides no complete NexDine transactional promotion lifecycle; on a composed site containing `pos_next`, even its sole POS Invoice calculation hook is suppressed". "Strict functional superset" → "broader implementation apparently intended to supersede the duplicated engine; contract tests required before deletion". `POS Offer` was wrongly marked dead — the reference count omitted desk JS and workspace JSON. The one-time key migration contradiction resolved to: enforce uniqueness on `customer + pricing_rule`, no name normalization. The stale NexDine appendix in `CODE_REVIEW.md` was rewritten, not annotated. **ADR-6 "hook parity" was withdrawn** — F3 and F6 make it unsafe.

**Corrections applied to the intermediate review.** Its replacement LOC (42,912) and invoice-reference (405/77) figures were as unreproducible as the ones they corrected; Appendix A supersedes both. Its "near-zero cost" characterization of the harness was PR #1's claim and is corrected in §7.1. Its treatment of NexDine's server-authoritative pricing as a passing observation is promoted to a scope decision in §2, because it removes NexDine from the critical path of `pos_next`'s trust-boundary work.

**Findings added during validation:** F1(b) and F1(c), F3, F4, F5(a)–(c), F6, F8, F9, F10, F12, and the F11 reclassification.

---

*All findings are static-analysis results at the baselines above. None has been reproduced against a live database — closing that gap is Phase 1.*
