# Generic Promotions Architecture

**Status:** Architecture decisions required · implementation not authorized
**Date:** 2026-09-08
**Scope:** `promotion_core` (the single permanent custom app), `posnext_promotions` (migration source), `pos_next`, `nexdine`, `hospitality_core`, ERPNext v15
**Baselines:** `nexdine@03ebd4b` · `posnext_promotions@70be440` · `pos_next` working tree · ERPNext v15 on bench
**PR artifact checked:** `BrainWise-DEV/Promotions` PR #1 at `b7ae3b7`
**Evidence:** static source analysis. **No finding has been reproduced against a live database.** Every figure below carries its reproducing command.

---

## 0. Validation disposition

**Conditionally accepted after correction.** All 19 factual evidence rows in Appendix A were re-derived from the stated source baselines. Architecture recommendations and future-failure scenarios are judgments, not executable facts, and are labelled as decisions, requirements or prospective risks.

Validation changed seven material points: NexDine is 42,619 Python LOC rather than 27,543 because the earlier command broke on apostrophe-containing paths; custom one-time recording is not currently performed on POS Invoice, so consolidation double-recording is a naïve-hook-parity risk; Quotation can calculate native coupon Pricing Rules but lacks an explicit coupon lifecycle; the custom model makes the `Promotion Redemption` ledger authoritative rather than ERPNext's `Coupon Code.used`; the native schema permits shared Pricing Rules while its runtime assumes one coupon per rule; the inert legacy offline cache should have its NULL predicate fixed but must not be widened into a second contract; and all permanent promotion code must live in one custom app, with consumer repositories exposing only promotion-agnostic extension seams where Frappe document hooks are insufficient.

The architecture is ready for decision-making, **not implementation planning**: O1–O8 and the normative-source cleanup in D7 remain explicit gates.

---

## 1. Decisions

### 1.1 Decided

| # | Decision | Rationale |
|---|---|---|
| **D1** | **Promotions is a generic commercial capability, not a POS feature.** It must serve POS applications, ERPNext Desk documents, APIs, e-commerce and future channels. | NexDine proves the requirement; it is not the centre of it. |
| **D2** | **The promotions app depends only on ERPNext, never on a POS app as a required dependency.** Optional consumer integrations are isolated inside the custom app and activated only when their target DocTypes/capabilities exist. | NexDine and `pos_next` remain independent products; neither is installed merely to obtain promotions. |
| **D3** | **Create one permanent custom app, `promotion_core`.** It contains the domain, DocTypes, services, channel adapters, integration assets, migrations and promotion tests. `posnext_promotions` is a temporary migration source and is retired; it is not a second management/UI app. | One installable owner prevents the duplicated endpoints, hooks, globals, counters and app-order behaviour that produced the current defects. For this specification, “our custom app” means `promotion_core`. |
| **D4** | **`promotion_core` owns the coupon model.** A `Promotion Coupon` DocType owned by the core is canonical. Sites governed by the core do not mix it with ERPNext's native `Coupon Code` lifecycle. `POS Coupon` is migrated into `Promotion Coupon` and retired. | Native `Coupon Code` has unsafe counter/customer semantics (F5), explicit lifecycle wiring on only 2 of 5 selling documents, and no company, disabled, per-customer-use or offline-capability policy. Correcting that lifecycle solely through public hooks would require bypassing or replacing most of it anyway. Deployment inventory must still prove whether native coupon rows or submitted native-coupon documents exist; their absence cannot be inferred from repository code. |
| **D5** | **Free composite/recipe items carry zero revenue, consume stock, and book normal COGS**, with provenance preserved. | Economically correct; `nexdine_recipe_stock` already behaves this way. |
| **D6** | **Offline: certified-subset only, receipts bind at the collected amount.** Coupons, one-time offers, gift pools, accumulative and stock-dependent gifts are online-only until a reservation design exists. | Preserving a price promised while disconnected is a different invariant from trusting arbitrary browser prices. |
| **D7** | **This document is the sole normative architecture specification.** | The older `2026-09-08-promotions-remediation-design.md` contains superseded Option B and hook-parity decisions. It must be deleted or reduced to a redirect before PR #1 merges. |
| **D8** | **No promotion-specific code is added to ERPNext, `pos_next`, NexDine or `hospitality_core`.** When existing hooks cannot expose a safe integration point, the consumer may add a small, documented, promotion-agnostic extension seam; its implementation, policy, UI and tests for promotional behaviour remain in `promotion_core`. | Frappe hooks can cover document lifecycle, but cannot safely replace NexDine's internal pre-KOT item list or split-field copying without duplicating its orchestration. A neutral seam is less coupling than a copied consumer workflow or import-time monkey patch. |

#### D4 invariants

Owning the model is only safe under three rules. Each is a test, not a convention.

**I1 — Never mix coupon lifecycles on one governed transaction.** Every ERPNext coupon behaviour
is guarded by `if self.coupon_code:` (`pos_invoice.py:227,249,286`;
`sales_order.py:231,446,480`). A transaction using `Promotion Coupon` keeps the native field
unset so native validation and counting do not also fire. Pre-install and migration checks must
inventory native `Coupon Code` rows and submitted documents carrying native `coupon_code`;
installation must abort with a report until the operator migrates them or explicitly keeps that
site outside the custom coupon lifecycle. Enforce the mutual exclusion with adapter validation
and a conformance vector, not with an unverifiable claim that native coupons have never been used.

**I2 — The core computes coupon discounts; ERPNext's engine will not.**
`pricing_rule/utils.py:594-602` applies a `coupon_code_based` Pricing Rule **only** when the
document's native `coupon_code` is set *and* resolves to that exact rule:

```python
if not d.coupon_code_based:
    doc.set(field, d.get(pr_field))
elif doc.get("coupon_code"):
    coupon_code_pricing_rule = frappe.db.get_value("Coupon Code", doc.get("coupon_code"), "pricing_rule")
    if coupon_code_pricing_rule == d.name:
```

Under I1 that branch is unreachable, so coupon-based Pricing Rules are **inert under ERPNext's
engine by design**. This is a consequence to state, not a defect to fix: the core owns coupon
calculation end to end (§3.2–3.3). Never set `coupon_code_based = 0` to make the engine apply
them — the discount would then apply to every cart with no coupon at all.

**I3 — Do not re-implement the defects being avoided.** The three F5 defects are cheap to
reproduce, and this codebase already reproduced one: `posnext_promotions/api/coupon_engine.py:437`
carries the identical unlocked read-modify-write. The core must ship an atomic conditional
`UPDATE` with the ledger as authority, treat `max_uses = 0` as unlimited with an explicit test,
and enforce customer binding before value is granted.

**Scope of what stays native.** D4 is an exception to §3.7, not a repeal of it. ERPNext keeps
Pricing Rule and Promotional Scheme for non-coupon promotion mechanics, stock, tax and
accounting. The exception applies where ERPNext models a **scarce entitlement** — the
money-critical, concurrency-sensitive part — incompletely.

### 1.2 Open — in dependency order

| # | Decision | Blocks | Owner |
|---|---|---|---|
| **O1** | **Logical promotion transaction identity and authority.** Define the authoritative Promotion Transaction record spanning bill splits, POS Invoice consolidation, and Sales Order → Sales Invoice; every derived document references it. | F1, F2, F6 — and supplemental redemption uniqueness | Engineering |
| **O2** | **Per-document coupon lifecycle matrix.** For each selling stage decide resolve, calculate, validate, reserve, consume, reverse, or provenance-only behavior. | F3, all adapter work | Product + Engineering |
| **O3** | **Coupon → Pricing Rule cardinality**, and the `disabled` mapping that follows from it. | F4, migration safety and runtime cost | Engineering |
| **O4** | **Stock-consumption ownership** when NexDine and `hospitality_core` are both installed; whether that combination is a supported product shape. | F7, CI matrix | Product |
| **O5** | **Deployment inventory.** Which sites run `posnext_promotions`. | containment vs. release-gate sequencing | Deployment owner |
| **O6** | **Coupon code uniqueness policy** for multi-company sites. | F9, migration | Product |
| **O7** | **Phase gates and rollback criteria.** | F10, any live migration | Engineering lead |
| **O8** | **Consumer extension-seam contract.** Approve the minimal promotion-agnostic hooks needed before KOT generation, during split-line copying, at authoritative basket persistence and in checkout UI composition. | D8, F2, F6, consumer integration plans | Consumer maintainers + Engineering |

**O1 is the keystone.** Three of the highest-severity findings are one missing concept.

### 1.3 Deployment posture

Unverified. Repository history cannot establish deployment — Frappe apps are commonly installed from branches, and `posnext_promotions` is absent from this bench's `apps.txt`, which is weak evidence only.

> **Until O5 closes, treat `posnext_promotions` as potentially deployed.**

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
nexdine       git ls-files -z '*.py' | xargs -0 cat | wc -l  →  42,619 across 368 files
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

ERPNext implementations of these ports and every promotion-aware adapter live in `promotion_core`. Consumer applications continue to own their ordinary checkout, kitchen, stock and offline workflows, but they do not implement promotion rules or redemption logic.

### 3.5 Ownership

```text
promotion_core (the only permanent custom promotions app)
├── required dependency: ERPNext
├── built-in adapter: ERPNext selling documents
├── optional adapter: pos_next
├── optional adapter: nexdine
├── optional adapter: hospitality_core
└── future adapters

nexdine ──required dependency──> HRMS
posnext_promotions ──data/code migration only──> promotion_core ──then retired
```

| Owner | Responsibilities |
|---|---|
| **ERPNext** | Pricing Rule and Promotional Scheme mechanics for **non-coupon** promotions; stock, tax and accounting. It contains no BrainWise promotion patch or override. Native coupon use is mutually exclusive with `Promotion Coupon` on a governed transaction (I1). |
| **`promotion_core`** | Every promotion-specific artifact: `Promotion Coupon`; rule extensions; evaluator; entitlement ledger; quote/reserve/consume/reverse; authorization; ERPNext/`pos_next`/NexDine/`hospitality_core` adapters; hooks; compatibility endpoints; migrations; authoring UI; checkout assets; offline bundles; audit and all promotion-specific tests. |
| **`pos_next`** | Its normal Vue checkout, Sales Invoice construction, offline queue and device policy. It removes its duplicated promotion engine and exposes only generic server/UI extension seams if required. Its independent frontend-price trust defect remains its own responsibility because it affects non-promotional pricing too. |
| **`nexdine`** | Its normal React checkout, POS Invoice, KOT, recipe stock, splitting and offline workflows. It exposes only the promotion-agnostic seams approved in O8; no coupon/promotion eligibility, calculation or redemption code lives here. |
| **`hospitality_core`** | Its normal hotel/composite-stock workflow, subject to O4. It exposes a generic stock-consumption arbitration/provenance seam only if the supported-composition decision requires one. |

Neither POS application depends on or imports the other. The `promotion_core.domain` package imports neither. Optional integration modules inside `promotion_core` may call documented public consumer contracts after capability detection; those imports must never leak into the domain or make a consumer a required app dependency.

The core exposes **stable service contracts**, not whitelisted functions named after one consumer. Compatibility endpoints may delegate during migration but must not become the permanent domain API.

### 3.6 Single-app code boundary

```text
promotion_core/
├── domain/                 # pure evaluation and policies; no consumer imports
├── promotion_core/doctype/ # Promotion Coupon, Redemption, Calculation, grants
├── services/               # quote/reserve/consume/reverse, stock and history ports
├── adapters/
│   ├── erpnext/            # Quotation, SO, DN, SI and POS Invoice lifecycle
│   ├── pos_next/           # request/context mapping and compatibility endpoints
│   ├── nexdine/            # POS Invoice, KOT, split, recipe and offline integration
│   └── hospitality/        # composite-stock arbitration when supported
├── public/                 # core-owned Desk, Vue/React integration bundles
├── migrations/             # POS Coupon and posnext_promotions migration
└── tests/                  # unit, database, contract, concurrency and adapter suites
```

Promotion-specific code, fixtures, Custom Fields, scheduled jobs, whitelisted methods and UI assets are forbidden in consumer repositories after migration. Temporary delegators are allowed only during a dated compatibility window and must contain no business rule.

The one-app rule does **not** justify copying a consumer workflow into an override. Frappe `doc_events` can cover most document lifecycle work, but static inspection confirms three boundaries they cannot safely supply alone:

| Consumer boundary | Why the custom app cannot infer it safely afterward | Allowed consumer change |
|---|---|---|
| NexDine final lines before KOT | `sync()` calls KOT with the original client `items`, not final `inv.items` (F2) | **no seam required — see below** |
| NexDine bill splitting | `_copy_item_fields()` uses a closed field list (F1) | a generic extra-child-fields/copy hook |

**The KOT boundary needs no seam.** Static inspection collapses it to a one-expression change
inside NexDine, which removes a reason for consumer-side scaffolding and strengthens the
one-app rule:

1. **Timing already works.** `inv.save()` runs ~41 lines before
   `_run_kot_with_failure_surface(...)` (`nexdine_order.py`), so a `validate` hook from the
   custom app has already persisted promotion-added free lines onto `inv` when KOT runs. No
   pre-KOT callback is needed to make the data available — it is already there.
2. **The KOT path already accepts invoice rows.** `create_order_items`
   (`nexdine_kot_generate.py:43-56`) reads every field through a dual lookup whose *second*
   branch is the POS Invoice Item field name:

   ```python
   "item_code":        item.get("item",             item.get("item_code")),
   "comments":         item.get("comment",          item.get("comments", "")),
   "is_addon":         item.get("is_addon",         item.get("custom_is_addon", 0)),
   "addon_group_id":   item.get("addon_group_id",   item.get("custom_addon_group_id", "")),
   "addon_group_name": item.get("addon_group_name", item.get("custom_addon_group_name", "")),
   ```

   Those `custom_*` names are exactly the fields `_copy_item_fields` copies
   (`nexdine_order.py:1015-1018`), and `load_json` passes non-strings through unchanged
   (`:36-39`).

3. **The change.** Pass the final rows instead of the client list at `nexdine_order.py:745`:

   ```python
   _run_kot_with_failure_surface(
       inv, customer, table, [d.as_dict() for d in inv.items], past_item, comments
   )
   ```

   `as_dict()` is required, not cosmetic: `create_order_items:48-49` subscripts `item["qty"]`
   and `item["item_name"]`, and Frappe's `BaseDocument` implements `get()` but **not**
   `__getitem__`. Converting those two subscripts to `.get()` as well would make the function
   shape-agnostic and is worth doing at the same time.

**Consequence.** The KOT row above needs no generic provider hook, no interface and no
registration. Two consumer seams remain to design (splitting, and stock-consumption arbitration
if O4 requires it), not three. Confirm by execution in Phase 1 before deleting the seam from the
plan — `compare_two_array` computes a delta against `previous_items`, so the equivalence must be
proven against a re-order that has already sent a KOT, not only against a first order.
| POS checkout UI and offline provider | independently compiled Vue/React applications cannot discover arbitrary app UI safely | a generic registered checkout-extension loader |
| Cross-app composite stock | two submit hooks can consume the same sellable item (F7) | a generic ownership/provenance hook if O4 supports composition |

These seams are consumer infrastructure, not promotion implementations: they contain no promotion DocType names, eligibility decisions, discount math or redemption calls. All callbacks and assets plugged into them are owned and tested by `promotion_core`. If maintainers reject a required neutral seam, that consumer capability remains unsupported; the fallback is **not** monkey-patching, hook-order dependence, or copying its orchestration into the custom app.

### 3.7 The reuse rule

```text
use native ERPNext behavior unchanged
  → extend native DocTypes when a field or policy is missing
  → adapt native services to additional document types
  → add a supplemental record only for a capability ERPNext truly does not model
```

The burden of proof falls on every new table, field, endpoint and counter. Most of the domain
satisfies this rule natively: Pricing Rule and Promotional Scheme keep non-coupon promotion
mechanics; stock, tax and accounting stay entirely ERPNext's.

**The rule has one declared exception: scarce entitlements (D4).** Where ERPNext models a
money-critical, concurrency-sensitive entitlement incompletely, reuse can import lifecycle
semantics that public hooks cannot reliably replace. Coupons are the current exception because
of the F5 counter/customer hazards, lifecycle wiring on only 2 of 5 documents (F3), and missing
company, disabled, per-customer-use and offline-capability policy. Pricing Rule continues to own
ordinary discount scope and stacking mechanics; those are not duplicated into the coupon table.

**The test for any future exception:** does ERPNext model the concept completely enough that
extension fields suffice, and are its defects fixable without patching core? If either answer
is no, the core owns it. Otherwise it reuses.

Using ERPNext natively does not mean inheriting its defects — and choosing to own something
does not license re-implementing them (I3, F5).

---

## 4. Finding register

Severity-ranked. Every finding carries evidence, the failure it produces, and the required response. Confidence is **confirmed** (re-derived from source) unless stated.

| # | Finding | Sev | Blocks |
|---|---|---|---|
| F1 | No logical transaction identity — splits, consolidation and SO→SI all lose or double-count entitlements | **HIGH** | O1 |
| F2 | Adapter contract missing; KOT is built from the client item list | **HIGH** | O1, O2 |
| F3 | Coupon lifecycle must be built on all 5 selling documents — native wires only 2 | **HIGH** | O2 |
| F4 | Coupon → Pricing Rule cardinality — **downgraded to MEDIUM by D4**; now a cost/usability choice, not a migration-safety blocker | MEDIUM | O3 |
| F5 | Three defects in ERPNext's coupon services — **no longer inherited under D4**; retained as I3 acceptance criteria | **HIGH** | — |
| F6 | Consolidation destroys discount provenance and can re-fire promotion hooks | **HIGH** | O1 |
| F7 | Recipe/composite stock-consumption ownership undefined | **HIGH** | O4 |
| F8 | Currency precision and split allocation unspecified | MEDIUM | O1 |
| F9 | `Promotion Coupon` code uniqueness across companies is unspecified | MEDIUM | O6 |
| F10 | No phase gates or rollback criteria | MEDIUM | O7 |
| F11 | NexDine offline offer cache: NULL dates excluded, no eligibility scope | LOW | — |
| F12 | Native `Coupon Code.amended_from` is vestigial; informational only under D4 | INFO | — |

Defects internal to `posnext_promotions` are catalogued separately in `CODE_REVIEW.md` and scheduled in Phase 2.

---

### F1 — No logical transaction identity · HIGH

**One missing concept, three manifestations.** Each was found separately; all three resolve together.

**(a) Bill splitting.** `nexdine_order.py:1003-1021` `_copy_item_fields` copies 15 fields — `item_code, item_name, qty, rate, price_list_rate, base_price_list_rate, comment, custom_course, cost_center, custom_is_addon, custom_addon_group_id, custom_addon_group, custom_addon_group_name, custom_addon_surcharge, custom_is_default_selection`. **None** is `is_free_item`, `pricing_rules`, `discount_percentage` or `discount_amount`. Splitting destroys promotion provenance; two siblings may each attempt to consume a one-use coupon.

**(b) POS Invoice consolidation.** POS Invoice Merge Log creates a Sales Invoice (`pos_invoice_merge_log.py:350`, recorded at `:160`). Today, `posnext_promotions` registers its custom one-time recorder only on Sales Invoice; it does **not** record that ledger on POS Invoice submit. The consolidated Sales Invoice can therefore become the first custom-ledger event if the required provenance survives the merge. Separately, for native-coupon flows, `Coupon Code` consumption occurs on the source POS Invoice. A naïve "hook parity" adapter would add the custom recorder to POS Invoice and then run it again at consolidation. The double-recording claim is therefore a **prospective failure of naïve parity**, not a verified current event.

**(c) Sales Order → Sales Invoice.** ERPNext's Sales Order already increments `used` on submit (`sales_order.py:448`) and decrements on cancel (`:482`). Blindly adding equivalent Sales Invoice counting during adapter expansion would double-count:

| Flow | Events under a naïve document-hook adapter |
|---|---|
| Quotation → SO → SI | SO submit + SI submit = **2** for one sale |
| Direct SI, no SO | 1 |
| POS Invoice → Merge Log → consolidated SI | source consumption + re-fired consolidated-document hook |

ERPNext's native document hooks do not encode this cross-document identity because Sales Invoice has no coupon lifecycle.

**Required.** Consumption keys on the **logical transaction**, never on a document. Define once: a logical promotion transaction id created at first evaluation, carried onto every derived document, with supplemental redemption uniqueness on `(entitlement_key, logical_transaction)`. Then:

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

`items` is the **client** list, passed alongside the authoritative `inv`. Under a future adapter that adds a free dish to `inv` without also mutating that client list, the dish would consume recipe stock but **never reach the kitchen**. This is a confirmed integration seam and prospective failure, not evidence that NexDine drops promotional dishes today. Hook parity cannot reach it — it is a fulfilment concern, not an accounting one.

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

The pure domain must not import NexDine. The NexDine adapter and translation live inside
`promotion_core`; they may consume only the approved public seams in O8. NexDine supplies the
ordinary restaurant context and executes its KOT/stock workflow, but contains no promotion
policy or calculation code.

### F3 — Native coupon lifecycle is incomplete across selling documents · HIGH

"Use ERPNext where it already owns the concept" is the right rule. ERPNext has explicit coupon lifecycle wiring on **two** selling documents, no coupon field on two, and calculation without an explicit validation/consumption lifecycle on a third:

| DocType | `coupon_code` field | Pricing calculation | Explicit lifecycle validation | Native counting |
|---|---|---|---|---|
| Sales Order | ✅ | ✅ | ✅ `sales_order.py:233` | ✅ `:448` used / `:482` cancelled |
| POS Invoice | ✅ | ✅ | ✅ `pos_invoice.py:230` | ✅ `:252` used / `:289` cancelled |
| **Quotation** | ✅ | ✅ through Pricing Rule arguments | ❌ no `validate_coupon_code` call | ❌ none — appropriate while it remains a quote |
| **Sales Invoice** | ❌ | coupon-specific lifecycle absent | ❌ | ❌ |
| **Delivery Note** | ❌ | coupon-specific lifecycle absent | ❌ | ❌ |

Quotation is not merely decorative: its `coupon_code` can participate in Pricing Rule calculation through the shared pricing-rule arguments (`pricing_rule.py:454-461`, `pricing_rule/utils.py:594-603`). But it has no explicit native coupon validation call, and no declared transition semantics when the quote becomes an order or invoice. It should not consume an allowance while it is only a quote; it should still resolve the code and reject invalid, expired, exhausted, or customer-mismatched coupons before presenting a promotional price.

The wider document-adapter surface already exists today: `posnext_promotions` registers `apply_min_max_price_discounts` on Sales Order, Quotation and Delivery Note. That hook is not itself native coupon lifecycle support.

**Required.** State per selling document whether the adapter resolves, calculates, validates, reserves, consumes, reverses, or merely carries coupon provenance. Sales Invoice should gain a persisted **Link to `Promotion Coupon`** (D4), never to native `Coupon Code` (I1); `pos_next` currently accepts `coupon_code` in the request and assigns it dynamically at `api/invoices.py:996`, but ships no Sales Invoice coupon field in its tracked customizations. **Under D4 the core builds this lifecycle on all five documents**, so native coverage of two is a reason to own the model rather than a gap to work around. Quotation should resolve, calculate and validate without consuming; the later document consumes exactly once for the logical transaction selected in O1/O2.

### F4 — Coupon → Pricing Rule cardinality · **downgraded to MEDIUM by D4**

Native `Coupon Code.pricing_rule` is `Link` → `Pricing Rule`, **`reqd: 1`, `unique: 0`**, so N
coupons may share one rule. Native `Coupon Code` has **no `company` and no `disabled` field** —
full list: `coupon_name, coupon_type, customer, coupon_code, pricing_rule, valid_from,
valid_upto, maximum_use, used, description, amended_from`. Under the superseded native-adoption
plan both had to map onto the Pricing Rule, so disabling one coupon disabled every coupon
sharing its rule and per-coupon company scope was impossible.

**D4 dissolves the hazard.** `Promotion Coupon` owns `disabled` and company scope as first-class
fields, so disabling one coupon is local to it regardless of rule sharing.

**What remains** — if `Promotion Coupon` still links a Pricing Rule for discount mechanics,
state the cardinality anyway:

- **Per-coupon rules.** Simple, but N legacy coupons become N Pricing Rules, each evaluated by
  ERPNext's engine on every cart on a live system. Report the resulting rule count and its
  evaluation cost before migrating.
- **Shared rules.** Scales, and is now safe for `disabled` and company scope, but editing a rule
  changes every coupon pointing at it — needs an explicit change-impact warning when authoring.
- **Core-owned discount definition, no Pricing Rule.** Most consistent with I2, since
  coupon-based Pricing Rules are inert anyway. Removes the question at the cost of a second
  discount-definition surface.

O3 decides this. It is no longer a migration-safety blocker — only a cost and usability choice.

### F5 — Three defects in ERPNext's coupon services · **reclassified by D4**

These are **no longer inherited** — under D4/I1 native services never run. They stay in the
register for two reasons: they are the evidence base for D4, and I3 forbids re-implementing
them. This codebase already re-implemented one.

**(a) Lost update.** `erpnext/accounts/doctype/pricing_rule/utils.py:756-772`:

```python
coupon = frappe.get_doc("Coupon Code", coupon_name)
if coupon.used < coupon.maximum_use:
    coupon.used = coupon.used + 1
    coupon.save(ignore_permissions=True)
```

Read-modify-write, no `for_update`, no atomic conditional `UPDATE`. Two tills redeeming the final allowance both read the same value and write the same increment. This is the native counter any NexDine POS Invoice carrying `coupon_code` would invoke. The same lost-update pattern also exists in two custom implementations — the uncalled copy at `posnext_promotions/api/coupon_engine.py:437` and the live `pos_next/.../pos_coupon.py:181` path.

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

**(c) Gift Card customer binding is never enforced — and adopting native is a regression.** `validate_coupon_code` (`utils.py:746-753`) checks only `valid_from`, `valid_upto` and `maximum_use`. `Coupon Code.customer` exists and is **never read** by that service. Native coupon validation therefore does not stop Customer B from presenting a Gift Card assigned to Customer A; only an independently customer-scoped Pricing Rule could happen to reject it.

> `posnext_promotions/api/coupon_engine.py:48-51` **enforces this today.** Under D4 the core keeps that control and carries it to every document — but it must be carried deliberately, since the surrounding code is being rewritten. Under the superseded native-adoption plan this control would have been silently lost; that near-miss is part of why D4 was reversed.

**Required, as I3 acceptance criteria for the core's own implementation.** The global allowance
now lives on `Promotion Coupon`, hardened from the start rather than patched into ERPNext:

| Native defect | Core requirement | Conformance test |
|---|---|---|
| (a) unlocked read-modify-write | atomic conditional `UPDATE` or row lock; the `Promotion Redemption` ledger is the authority and the counter is a projection reconcilable from it | two concurrent redemptions of the final allowance → exactly one winner |
| (b) `maximum_use = 0` validates then throws at submit | `0` means **unlimited**, consistently at every stage | a `max_uses = 0` coupon redeems repeatedly without error |
| (c) customer binding never checked | enforced before value is granted, on every supported document | Gift Card bound to A is rejected for B |

The ledger enforces idempotency, per-customer uniqueness, logical-transaction allocation and
cross-document audit. It must never become a second mutable global counter.

The trap is (a): `posnext_promotions/api/coupon_engine.py:437` already contains the identical
defect. Owning the model removes the obligation to inherit these — not the temptation to
rewrite them.

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

`nexdine_recipe_stock.before_submit` is **not** a component expander — it creates one aggregated Material Issue Stock Entry from `NexDine Recipe`. `hospitality_core` also creates submitted stock entries, but resolves a BOM and creates a Material Consumption entry per composite invoice row. If the same sellable item is configured in both systems, both hooks can consume ingredients. Calling both "expanders" hides the actual duplicate-consumption risk.

**Required, as stock-consumption ownership:** which app owns consumption per sellable item; whether one item can be configured in both systems; how exactly one owner is selected when both are installed; whether NexDine + `hospitality_core` is a supported shape or merely possible; how free-promotion provenance reaches Stock Entry rows and profitability reporting; how return, cancellation and failed-submit reversals stay idempotent; how serial, batch, UOM, warehouse and negative-stock policies are preserved.

If both are supported, the invariant is **one economic inventory consumption**, not "components expanded once". If not supported, remove the combination from the CI matrix and declare the incompatibility.

**Free prepared item policy (D5) in full.** Appears on the KOT exactly once; consumes its recipe exactly once; zero customer revenue; normal COGS; removed or cancellation-KOTed correctly when the promotion is withdrawn; applied rule and qualifying parents preserved for audit. Paid modifiers do not become free because their parent is free, and add-ons do not independently qualify as paid basket items unless a rule allows it. The evaluator needs explicit flags for whether add-on surcharges contribute to minimum amount, whether add-on quantities contribute to minimum quantity, whether a free parent includes default or paid modifiers, whether a free product can be independently customized, and whether a free dish added after the first KOT requires an incremental KOT.

### F8 — Currency precision and split allocation unspecified · MEDIUM

`posnext_promotions/overrides/pricing_rule.py:825-834` `_rate_precision` returns a hardcoded `2` for any payload without a `precision()` method — wrong for 3-decimal currencies (KWD, BHD, OMR, TND). F1 requires allocating a frozen monetary effect across siblings, which is a rounding-allocation problem that must sum exactly to the original.

**Required.** Resolve precision from document currency; state the allocation rule (largest-remainder or equivalent); test that sibling totals reconcile to the parent to the last minor unit in a 3-decimal currency.

### F9 — `Promotion Coupon` code uniqueness across companies unspecified · MEDIUM

Native `Coupon Code` and legacy `POS Coupon` both make the scanned code globally unique. The
new core model does not exist yet and explicitly adds company ownership, so carrying that global
constraint forward is a product decision, not an inherited fact.

**Required.** Choose before the DocType is created: globally unique normalized codes, or a
database-enforced composite uniqueness on `(company, normalized_code)`. Migration must detect
collisions under the chosen normalization and abort with a report rather than silently rename.

### F10 — No phase gates or rollback criteria · MEDIUM

The delivery plan defines a destination and a sequence but not what makes a phase *fail* or how to reverse a landed one. NexDine is live at 10+ outlets and the plan includes live metadata migration between apps.

**Required, per phase:** preconditions, an explicit abort trigger, a reversal procedure tested in CI, and a named decision-maker. Phase 7 additionally needs a stated difference-rate threshold above which enforcement does not proceed.

### F11 — NexDine offline offer cache · LOW

`nexdine/nexdine/api/nexdine_offline.py:357-377` `_fetch_active_offers` filters `valid_from <= today AND valid_upto >= today`, so a Pricing Rule with either NULL boundary — including the common open-ended `valid_upto = NULL` case — is silently excluded. The selected payload returns only `name, title, apply_on, rate_or_discount, discount_percentage, rate`: no validity fields, item, group, brand, customer, company, quantity, amount, stacking or schedule scope. `preload.ts` nevertheless attempts to store `o.valid_upto`, which is absent from that payload. No scoped rule can be evaluated faithfully offline.

**Latent, not a verified live checkout defect.** `pos/src/lib/offline/preload.ts:319-328` only `clear()`s and `bulkPut()`s `db.offers`. Outside that preload path, production references are schema declarations at `pos/src/lib/offline/db.ts:160,247,327`; tests also mention the table. Static search finds no production retrieval consumer, but cannot exclude an external consumer.

**Required.** Correct the NULL-bound date predicate now. Do **not** widen this legacy payload into a second, uncertified promotion contract: static absence of an in-repository reader does not prove that no external consumer exists, and a richer cache would create an unsafe transitional surface. Deprecate the cache and introduce complete eligibility scope only through the versioned certified offline bundle (§5.2).

**Do not** wire `posnext_promotions.api.offers.get_offers` straight into this cache — it would cache rules D6 declares online-only. Publish a certified bundle instead (§5.2).

### F12 — Native `Coupon Code.amended_from` · INFORMATIONAL

`coupon_code.json` declares `is_submittable: 0` while retaining `amended_from`. That is a native
schema curiosity, not a `promotion_core` requirement under D4. `Promotion Coupon` must define
its own draft/approval/activation lifecycle explicitly; it must not copy `amended_from` merely
because ERPNext carries it.

---

## 5. Coupon migration

### 5.1 Legacy `POS Coupon` → `Promotion Coupon`

Migration is core-owned (D3/D4). Static search finds no tracked custom-app writer that creates
or synchronizes native `Coupon Code`, but that does **not** prove site tables or submitted
Sales Orders/POS Invoices contain none. Phase 0 must inventory both native and legacy data. If
native usage exists, migration becomes a two-source reconciliation and installation must stop
until its mapping and rollback are approved.

Map each concept to the core model, keeping non-coupon mechanics on Pricing Rule per §3.7.

| Legacy `POS Coupon` | Core destination |
|---|---|
| `coupon_name`, `coupon_code`, `coupon_type`, `customer` | `Promotion Coupon` |
| `valid_from`, `valid_upto`, `maximum_use` | `Promotion Coupon` |
| `used` | **not migrated as authority** — reconciled from submitted documents into the ledger; retained only as a projection (I3) |
| `pricing_rule` | `Promotion Coupon.pricing_rule`, or the core's own discount definition |
| `disabled` | **`Promotion Coupon.disabled`** — a first-class field the core owns, which dissolves F4's shared-rule hazard |
| `company` | `Promotion Coupon.company`; rule applicability must agree |
| `campaign` | Linked Pricing Rule or core campaign relation |
| `discount_type`, percentage, amount | Linked Pricing Rule rate/discount fields |
| `min_amount`, `max_amount`, `apply_on` | Linked Pricing Rule thresholds |
| `one_use`, per-customer limits | `Promotion Coupon` fields + redemption-ledger uniqueness |
| referral relationship | Core-owned referral integration module — not consumer promotion code |
| excluded items/groups/brands | Pricing Rule scope/exclusion extensions |

Per legacy coupon:

1. Create one `Promotion Coupon` per legacy row via a deterministic migration key.
2. Create or match its Pricing Rule — **subject to O3; matching shares the rule (F4)**.
3. Map percentage/amount and eligibility onto the chosen rule definition, never onto native `Coupon Code`.
4. Map customer-bound gift cards to `Promotion Coupon.customer` and `coupon_type`.
5. Preserve original identifiers and submitted-document references in migration/audit fields.
6. Reconcile legacy `used` against submitted source documents. **Do not copy an unverified counter.**
7. Detect code/name collisions and **abort with a report** — never silently rename (F9).
8. Run in shadow/read compatibility mode for one release if deployed data requires it.
9. Remove legacy write paths, then remove `POS Coupon`, its controller, API CRUD, permission strings, fixtures and workspace links after the rollback window closes.

One active coupon definition remains on a governed site: `Promotion Coupon`, owned by the core.
Any existing native coupon records are migrated, archived or explicitly excluded by the Phase 0
decision; they are never silently ignored. I1 prevents native and custom lifecycle execution on
the same transaction.

**Before removing anything from `pos_next`:** `POS Offer` is **not** dead — it is referenced from `pos_next/pos_next/doctype/pos_coupon/pos_coupon.js` and `pos_next/pos_next/workspace/posnext/posnext.json`. `POS Offer Detail` and `POS Coupon Detail` appear unreferenced but still require row counts, Dynamic Links, fixtures, reports, exports, integrations and customer scripts to be checked first.

### 5.2 Coupon identity at API boundaries

ERPNext's `Coupon Code` names the document from `coupon_name` (`autoname: field:coupon_name`,
**UNIQUE reqd**) while the scanned value lives in a separate **UNIQUE** `coupon_code` field —
two distinct unique keys that tests commonly make identical. `Promotion Coupon` should **not**
repeat that design: name the document from a system-generated id and keep the scanned code as
one unique field, so document name and user-facing code never diverge.

The resolution rule below is core-owned.

- Document fields and ledger references carry the `Promotion Coupon` system-generated document name.
- One public `resolve_coupon(code, context)` service accepts the scanned code, resolves it under the O6 company/normalization policy, and returns its name only after core validity, company, customer, status and allowance checks pass.
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
| Upgrade: legacy `POS Coupon` → `Promotion Coupon` | Migration safety |
| Both POS apps | Only if officially supported |
| `nexdine` + `hospitality_core` | Only if O4 declares it supported |

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
| A transaction governed by `Promotion Coupon` rejects simultaneous native `coupon_code`; migration preflight reports existing native use | **I1** |
| A `Promotion Coupon` applied to POS Invoice does not trigger native validation or counting | I1, F5 |
| A `coupon_code_based` Pricing Rule is not silently expected to self-apply | **I2** |
| The core's own counter is atomic under concurrent redemption — the F5(a) defect is not re-implemented | **I3** |
| The consolidated Sales Invoice can still explain every discount despite `price_list_rate = 0` | F6 |
| A return or group cancellation reverses the entitlement and recipe stock exactly once | F1, F7 |
| Split sibling totals reconcile to the parent to the last minor unit in a 3-decimal currency | F8 |
| Migration aborts with a report on a code collision rather than renaming | F9 |
| Dine In, Take Away, Delivery and Aggregator orders receive only eligible rules | F2 |
| Branch, restaurant, room, price-list, company and warehouse scope cannot bleed | F2 |
| Offline-unsafe rules are unavailable **before** the cashier promises them | D6 |
| A tampered offline outcome is quarantined, never silently accepted or repriced | D6 |
| Every hook executes once regardless of `apps.txt` order | F6 |
| Promotion behavior works with only core-owned callbacks plugged into each approved neutral consumer seam | D8, O8 |
| Removing `promotion_core` leaves no import, endpoint, field or asset reference to it in a consumer repository | D8 |

---

## 8. Delivery

Parallel workstreams, not a linear sequence. **Every phase carries the gates F10 requires: preconditions, abort trigger, tested reversal, named decision-maker.**

| Phase | Content | Depends on |
|---|---|---|
| **0 — Facts and containment** | Inventory every managed and customer-managed site: installed apps, commit SHAs, promotion settings, coupon data, submitted promotion-bearing invoices, real consumer paths. If promotions is live, disable coupons, one-time offers, gift pools and free gifts until server enforcement exists; otherwise gate them for release. | — |
| **1 — Harness and characterization** | Adapt NexDine's CI. Publish exact test counts. Characterize current Sales Invoice and POS Invoice behaviour before refactoring. Add failing tests for confirmed fail-open defects. | **nothing — start now** |
| **2 — Independent containment fixes** | In the legacy `posnext_promotions` app only: one-time key lookup and semantic uniqueness; remove client-named rule readmission; coupon/redemption failures deny value; submit-time gift-stock validation; PIN lockout fails closed; replace destructive install behaviour; make the ERPNext patch observable; extend authorization to POS Invoice where the action model is clear. Remove/deprecate NexDine's inert offer-cache surface rather than expanding it; the replacement bundle belongs to `promotion_core`. | Phase 1 |
| **3 — Close the ADRs and seams** | O1–O4 and O6–O8. Freeze the single-app boundary in D8, approve neutral consumer seams, and design versioned metadata/data migrations. O5 independently decides whether Phase 0 is containment or a release gate. | Phase 1 |
| **4 — Core provider and ledger** | Inside `promotion_core`: one authoritative evaluator; calculation identity and explanation trace; quote/reserve/consume/reverse/offline-bundle; atomic constraints and idempotency keys. This avoids re-implementing F5(a); it does not patch ERPNext's unused native counter. Remove import-time monkey-patching and ad-hoc installed-app branches; isolate optional adapter discovery in one registry. | Phase 3 |
| **5 — Core-owned adapters** | Implement ERPNext, `pos_next`, NexDine and supported `hospitality_core` adapters inside `promotion_core`, including KOT, recipe, add-on, split, return, cancel and authorization behavior. Consumer repositories receive only the approved generic O8 seams and deletion of duplicated promotion endpoints, hooks, globals, UI and engine code. `pos_next`'s general `ignore_pricing_rule`/frontend-price remediation remains a separate POS fix. | Phase 4 |
| **6 — Offline certified subset** | Versioned snapshots and shared conformance vectors. Only explicitly certified deterministic classes. Quarantine mismatches; preserve the amount collected. Expand one capability at a time. | Phase 5 |
| **7 — Shadow and outlet rollout** | Shadow-evaluate old and new without changing charged totals. Classify every difference as rounding, stale data, evaluator mismatch or attempted manipulation. Pilot one controlled NexDine outlet, then expand with rollback, reconciliation and profitability reporting. | Phase 6 |

Containment may temporarily touch legacy promotion copies while they are deployed, but no new
feature is developed there. Delete the `pos_next` copy and retire `posnext_promotions` as soon
as `promotion_core` migration completes. The only lasting consumer changes are the generic O8
extension seams.

---

## 9. Definition of ready for implementation plans

- [x] Canonical coupon model chosen — **D4**
- [x] Neutral core ownership and package boundary accepted, no deferred re-home — **D3**
- [x] Composite/recipe free-item accounting policy — **D5**
- [x] Offline capability and commercial-binding policy — **D6**
- [x] Sole normative architecture specification identified — **D7**
- [x] Single permanent custom-app ownership accepted — **D8**
- [ ] **Logical transaction identity defined once, covering split, consolidation and SO→SI — O1**
- [ ] Per-document coupon support declared; Quotation resolved — **O2**
- [ ] Coupon → Pricing Rule cardinality declared, with the `disabled` mapping that follows — **O3**
- [ ] Recipe/composite stock-consumption ownership and supported combinations — **O4**
- [ ] Deployment inventory determines containment vs. release gate — **O5**
- [ ] Coupon code uniqueness policy for multi-company — **O6**
- [ ] Phase gates and rollback criteria — **O7**
- [ ] Minimal promotion-agnostic consumer extension seams approved — **O8**
- [ ] Superseded remediation spec deleted or reduced to a non-normative redirect — **D7**
- [ ] Generic input, output, lifecycle, capability and port contracts accepted — §3.2–3.4
- [ ] Standard ERPNext document-adapter coverage accepted — §3.5
- [ ] NexDine pricing/KOT/recipe orchestration contract accepted — F2
- [ ] Bill-split allocation policy including precision — F1, F8
- [ ] Add-on and free-prepared-item policy accepted — F7
- [ ] CI matrix has named repositories, branches/SHAs and test ownership — §7.1

Implementation planning is split into **domain**, **migration** and **integration-module**
workstreams inside the single `promotion_core` repository. Separate consumer changes, if any,
are limited to the generic O8 seams. No other repository implements promotion eligibility,
redemption, calculation, authoring or UI behavior.

---

## Appendix A — Evidence catalogue

Every non-obvious claim, with its source. The 19 rows below were re-derived on 2026-09-08. Repository-prefixed commands are run from that repository's root; exact counts are baseline-specific. “Verified” here means source-confirmed, not live-database-confirmed.

| Claim | Source |
|---|---|
| `nexdine` size | `git ls-files -z '*.py' \| xargs -0 cat \| wc -l` → 42,619 LOC; `git ls-files -z '*.py' \| tr -cd '\0' \| wc -c` → 368 files. NUL delimiters are required because tracked paths include `today's_sales`. |
| `nexdine` invoice bias | `git grep -o '"POS Invoice"' -- '*.py'` → 178; `'"Sales Invoice"'` → 18 |
| `nexdine` independence | `nexdine/hooks.py:11-12` `required_apps = ["hrms"]`; 4 files mention `pos_next`, none an import, hook or dependency |
| `nexdine` frontend | `pos/package.json` react ^19.0.0, zustand ^5.0.6, dexie ^4.4.2, vite ^6.2.0, vitest ^3, typescript ~5.7.2; `kotdisplay/package.json` vue ^3.3.4 |
| `nexdine` server-authoritative pricing | `nexdine_order.py:1395-1425`; `git grep -n ignore_pricing_rule -- '*.py'` → no match |
| `pos_next` client trust | `pos_next/api/invoices.py:786` `ignore_pricing_rule = 1`; `:839` "Trust frontend's price_list_rate" |
| Duplication | 39 shared function names; 2,201 LOC in `pos_next/api/offers.py`, `api/promotions.py`, `overrides/pricing_rule.py`, `doctype/pos_coupon/pos_coupon.py` |
| `Coupon Code` schema | `autoname: field:coupon_name`; `coupon_name` UNIQUE reqd; `coupon_code` UNIQUE; `pricing_rule` Link reqd **not unique**; no `company`, no `disabled`; `is_submittable: 0` with `amended_from` |
| Native coupon coverage | SO `sales_order.py:233,448,482`; POS Invoice `pos_invoice.py:230,252,289`; Quotation field plus shared Pricing Rule calculation (`quotation.py:62`, `pricing_rule.py:454-461`, `pricing_rule/utils.py:594-603`) but no explicit validation/counting; Sales Invoice and Delivery Note have no coupon field or lifecycle |
| Native counter defects | `pricing_rule/utils.py:746-753` validation, `:756-772` counting |
| Existing customer-binding control | `posnext_promotions/api/coupon_engine.py:48-51` |
| Consolidation | `pos_invoice_merge_log.py:160,234-238,350` |
| KOT from client list | `nexdine_order.py:745` |
| Split field copying | `nexdine_order.py:1003-1021` |
| Offline cache has no in-repository production reader | preload writes at `pos/src/lib/offline/preload.ts:319-328`; remaining production references are schema declarations at `pos/src/lib/offline/db.ts:160,247,327`; repository-wide TS/TSX search finds no retrieval path |
| Offline NULL-date/filter payload defects | `nexdine/nexdine/api/nexdine_offline.py:357-377` requires both nullable boundaries and does not select either validity field; `preload.ts:325` reads absent `o.valid_upto` |
| `POS Offer` still referenced | `pos_next/.../pos_coupon/pos_coupon.js`; `pos_next/pos_next/workspace/posnext/posnext.json` |
| Redemption autoname | `one_time_customer_offer_usage.json:3` `format:{customer}:{pricing_rule}`; `frappe/model/naming.py:565-583` returns the formatted name verbatim |
| Coupon-based Pricing Rules need the native field | `pricing_rule/utils.py:594-602` — applies a `coupon_code_based` rule only when `doc.coupon_code` is set and resolves to that rule (I2) |
| Native coupon behaviour is field-guarded | `pos_invoice.py:227,249,286`; `sales_order.py:231,446,480` — all guarded by `if self.coupon_code:` (I1) |
| No native coupon data exists to preserve | no app on this bench writes native `Coupon Code`; only `POS Coupon` is populated |
| NexDine CI | `.github/workflows/ci.yml`; GitHub Actions run `28940529115` completed successfully for full SHA `03ebd4b8a9b6259d0e139003fff57f50779f522d`; `git log -1 --date=iso 03ebd4b` → 2026-07-08 14:53:31 +0300 |

## Appendix B — Review provenance

This document supersedes the review-and-rebuttal exchange it grew from. Recorded so conclusions can be audited. Appendix A contains **19 claim rows**, several of which deliberately group multiple related source anchors; “20 evidence anchors” is therefore not used as a formal completeness count.

**Revision-size provenance.** The predecessor committed in PR #1 at `365793…` is 1,045 lines and this revision began at 618 lines. The reported 1,299-line predecessor was an uncommitted intermediate not recoverable from the reviewed Git history, so that exact before/after claim is not independently verifiable.

**Normative-source cleanup.** PR #1 still contains `2026-09-08-promotions-remediation-design.md`, whose Option B/Option C and hook-parity conclusions conflict with this document. D7 is not operationally complete until that file is deleted or replaced by a short redirect.

**Corrections applied to `BrainWise-DEV/Promotions` PR #1.** NexDine's POS is React/TypeScript, not Vue. LOC and invoice-reference figures were produced by a broken command (`find … -print -exec cat`, feeding filenames into the count, no `node_modules` exclusion) and are replaced by the published-method values in Appendix A. "Exactly one mention of `pos_next`" → "no imports, hooks, declared dependency or runtime calls". "No promotions surface at all" → "no dedicated promotions engine or authoring model". "Does nothing on NexDine" → "provides no complete NexDine transactional promotion lifecycle; on a composed site containing `pos_next`, even its sole POS Invoice calculation hook is suppressed". "Strict functional superset" → "broader implementation apparently intended to supersede the duplicated engine; contract tests required before deletion". `POS Offer` was wrongly marked dead — the reference count omitted desk JS and workspace JSON. The one-time key migration contradiction resolved to: enforce uniqueness on `customer + pricing_rule`, no name normalization. The stale NexDine appendix in `CODE_REVIEW.md` was rewritten, not annotated. **ADR-6 "hook parity" was withdrawn** — F3 and F6 make it unsafe.

**Corrections applied to the intermediate review.** Its replacement LOC (42,912) and invoice-reference (405/77) figures were as unreproducible as the ones they corrected; Appendix A supersedes both. Its "near-zero cost" characterization of the harness was PR #1's claim and is corrected in §7.1. Its treatment of NexDine's server-authoritative pricing as a passing observation is promoted to a scope decision in §2, because it removes NexDine from the critical path of `pos_next`'s trust-boundary work.

**D4 reversed after the rewrite.** An earlier revision made ERPNext `Coupon Code` canonical. That
was reversed: under any design the redemption ledger is the authority and `used` is a projection,
so adopting native bought a code and two dates while inheriting three defects unfixable without
patching core, a lifecycle covering 2 of 5 documents, and no company/disabled/per-customer/scope/
balance/stacking/offline semantics. No app populates native `Coupon Code`, so nothing was lost by
declining it. The reversal is safe because ERPNext's coupon behaviour is entirely guarded by
`if self.coupon_code:` — leaving that field unset (I1) yields exactly one coupon system per
document. Its cost is stated as I2: coupon-based Pricing Rules become inert under ERPNext's
engine, and the core owns coupon calculation end to end.

**Findings added during validation:** F1(b) and F1(c), F3, F4, F5(a)–(c), F6, F8, F9, F10, F12, and the F11 reclassification.

---

*All findings are static-analysis results at the baselines above. None has been reproduced against a live database — closing that gap is Phase 1.*
