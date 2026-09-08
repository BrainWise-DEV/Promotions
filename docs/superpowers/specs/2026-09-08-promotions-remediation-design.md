# Promotions Remediation — Cross-Repository Architecture Design

**Date:** 2026-09-08 · **Baseline:** `posnext_promotions` @ `70be440`, `pos_next` @ working tree
**Scope:** `posnext_promotions`, `pos_next`, `hospitality_core` (NexDine composition)
**Companion:** [`CODE_REVIEW.md`](../../../CODE_REVIEW.md) — the 26-finding review this derives from

---

## Decision block

> **Status:** Architecture decisions required. Implementation not yet authorized.
>
> **Deployment assumption:** Unknown. Repository history cannot answer this — Frappe apps
> are commonly installed directly from branches, and the absence of tags, releases or CI
> proves nothing. **Treat as potentially deployed until every environment is inventoried.**
> If confirmed undeployed, containment becomes a release gate rather than an emergency patch.
>
> **Ownership decision:** ⚠️ **OPEN — see ADR-1.** Evidence gathered after the initial
> recommendation materially changed its inputs. Two options remain live; the decision is
> a business call about whether a POS with no promotions is a product you ship.
>
> **Trust-boundary decision:** `pos_next` owns invoice pricing acceptance.
> `posnext_promotions` may provide promotion calculations but cannot independently make a
> submitted invoice authoritative.
>
> **Offline decision:** Coupons, one-time offers, gift pools and free gifts require
> connectivity initially. Offline receipts bind at the collected amount; discrepancies are
> reconciled through an exception workflow and are **never** silently charged back to the
> customer.
>
> **Composite policy:** A free composite item carries zero revenue, consumes stock, books
> COGS normally, expands components exactly once, and preserves free-item provenance for
> reporting without suppressing stock or accounting entries.

---

## 1. Problem statement

This is a `pos_next` trust-boundary problem whose defects surface in `posnext_promotions`.

`pos_next/api/invoices.py:786` disables ERPNext's pricing engine at submit:

```python
# Disable automatic pricing rules (we handle discounts manually from POS)
invoice_doc.ignore_pricing_rule = 1
invoice_doc.flags.ignore_pricing_rule = True
```

and `:839` accepts the browser's prices:

```python
# NORMAL FLOW: Trust frontend's price_list_rate if provided and valid
```

Every "client-supplied value" defect in the review is downstream of these two decisions.
Hardening a promotions endpoint to re-derive totals while `update_invoice` still trusts
frontend `price_list_rate` moves the trust boundary by exactly one endpoint and no further.

**Offline operation does not require trusting arbitrary browser prices. It requires
preserving a price promised while disconnected.** Those are different invariants, and
conflating them is what produced the current design.

---

## 2. ADR-1 — DocType ownership and dependency direction (OPEN)

### Measured facts

Both apps ship a promotions engine. `posnext_promotions` is a strict functional superset.

| Module | `pos_next` | `posnext_promotions` |
|---|---|---|
| `api/offers.py` | 647 | 1145 |
| `api/promotions.py` | 969 | 1139 |
| `overrides/pricing_rule.py` | 378 | 871 |
| `api/apply_offers.py` | — | 1667 |
| `api/coupon_engine.py` | — | 463 |
| `api/promotion_exclusions.py` | — | 407 |
| `promotions/engine.py` | — | 644 |
| `promotions/schedule.py` | — | 250 |
| `api/gwp.py` / `gift_pool.py` / `product_free.py` / `promotions/scope.py` | — | 654 |

- **39 function names are implemented in both apps.**
- **2,201 LOC in `pos_next`** is superseded by `posnext_promotions`.
- `posnext_promotions` has **8 modules `pos_next` has no equivalent of**.

DocType reference counts (excluding each doctype's own directory):

| DocType | Python refs | Vue refs | Status |
|---|---|---|---|
| `POS Offer` | 0 | 0 | **dead** |
| `POS Offer Detail` | 0 | 0 | **dead** |
| `POS Coupon Detail` | 0 | 0 | **dead** |
| `POS Coupon` | 22 | 4 | live |
| `One Time Customer Offer Usage` | 5 | 0 | live |

The four Vue references are `checkPermission("POS Coupon", …)` in
`POS/src/composables/usePermissions.js:112-147` — permission strings, not schema.

Precedent for re-homing a DocType between these two apps already exists:
`posnext_promotions/patches/v2_3_0/rehome_gift_pool_item_doctype.py` moved
`POS Gift Pool Item` off `pos_next`.

### Option A — Companion (`posnext_promotions` requires `pos_next`)

`pos_next` keeps `POS Coupon` and `One Time Customer Offer Usage`. Promotions declares
`required_apps = ["erpnext", "pos_next"]` and the README stops claiming independence.

- **For:** smallest immediate diff; `pos_next` stays independently installable; no
  coordinated release; no metadata migration.
- **Against:** the 2,201 LOC duplicate engine and all 39 duplicated functions remain. Every
  cross-app defect in the review is a duplication artefact and must be *fixed* in two places
  rather than deleted once. Ongoing synchronisation cost is permanent.

### Option B — Consolidation (`pos_next` requires `posnext_promotions`)

Promotions takes `POS Coupon` and `One Time Customer Offer Usage` via two re-home patches.
`pos_next` deletes its duplicate engine, its promotion doctypes, its duplicate `doc_events`
and its `pn_*` JS globals. The three dead doctypes are dropped.

- **For:** −2,201 LOC. One engine, one coupon table, one truth. These review findings and
  NexDine conflicts resolve **by deletion, not by new code**: the `::` bug's second call
  site, duplicated `record_one_time_offer_usage`, the `pn_sync_min_max` / `pn_toggle_min_max`
  global collision, the `apps.txt` ordering requirement, the
  `"pos_next" in frappe.get_installed_apps()` branch, and the split-brain coupon counter.
- **Against:** `pos_next` can no longer be installed without promotions. Requires a
  coordinated two-repo release and live metadata migration.

### The premise to re-examine

The original recommendation for A reasoned that *"reversing the dependency would couple the
base POS product to an optional promotions engine."* The measurement above shows promotions
is **not optional to `pos_next`** — `pos_next` ships its own. The real choice is **one engine
or two**, and two is the direct cause of every cross-app defect found.

### Decision criterion

**Do you ship a till with no discounts, coupons or offers at all?**

- **Yes** → Option A. The independence is load-bearing and its costs are worth paying.
- **No** → Option B. The independence is theoretical and is being paid for in defects.

### Deferred: `promotion_core`

A third neutral app owning coupons, redemption and the eligibility/calculation engine, with
all three apps depending on it, is the clean long-term shape. It is a migration project —
moving live metadata between modules, preserving table names, updating hooks/fixtures/roles/
workspaces, defining which app installs and uninstalls the shared core, and testing removal
and reinstall. **Revisit only after the money path is tested and stable.** Not a first
production fix.

---

## 3. ADR-2 — Trust boundary

`pos_next` owns invoice pricing acceptance. `posnext_promotions` registers a provider:

```python
class PricingProvider:
    def quote(self, document, context): ...
    def validate_offline_result(self, document, snapshot): ...
    def reserve_entitlements(self, document, result): ...
    def consume_entitlements(self, document, result): ...
    def reverse_entitlements(self, document): ...
```

`pos_next` owns orchestration and decides when a provider's result is required. This
replaces the import-time monkey-patch and removes app-order-dependent behaviour.

The client may send only **intent**: coupon code, "apply best offers", an explicit choice
between mutually exclusive eligible offers, and an authorization token. The server derives
eligible rules, quantities, monetary bases, company/date validity, customer eligibility,
stacking and exclusivity, discount values, free-item quantities, stock requirements and
redemption availability.

Responses carry an explanation trace so the UI stays useful:

```json
{
  "applied": ["RULE-001"],
  "rejected": [{"rule": "RULE-002", "reason": "minimum_quantity_not_met"}],
  "discount": 25,
  "free_items": [],
  "calculation_version": "3"
}
```

**"Same evaluator" means same semantics and conformance vectors** — not literally executing
the same Python in the browser. Where implementations differ, a corpus of golden calculation
vectors must prove equivalence.

---

## 4. ADR-3 — Offline capability and commercial binding

> Offline receipts bind commercially, but only features explicitly declared offline-safe may
> contribute to their price. Everything else requires connectivity. Synced invoices are never
> silently repriced after the customer has paid; discrepancies enter an exception workflow.

### Capability classification

| Capability | Offline policy | Reason |
|---|---|---|
| Cached base price | Allowed, versioned snapshot | Deterministic |
| Tax calculation | Allowed, versioned configuration | Deterministic if inputs complete |
| Simple percentage / fixed-price rule | Disabled initially; certifiable later | May become provably deterministic |
| Manual rate override | Within cached policy + offline authorization rules | High abuse potential |
| Coupon | **Online only** | Scarce, shared entitlement |
| One-time offer | **Online only** | Needs current redemption state |
| Gift pool | **Online only** | Scarce, shared entitlement |
| Free gift | **Online only** initially | Needs current stock and eligibility |
| Accumulative / cross-cart | **Online only** | Depends on authoritative history |
| Manager PIN approval | Online unless a secure offline design exists | Lockout and audit need coordination |

The UI must **prevent** these operations, not warn after the cashier has promised them. On
connectivity loss: unsafe applied promotions are removed before payment with a stated reason,
coupon and gift controls become unavailable, automatic unsafe offers stop applying, reconnect
may recompute the basket, and loss *during* payment follows an explicit documented rule rather
than opportunistic fallback.

### Versioned pricing snapshots

**Online:** browser sends basket intent → server evaluates authoritatively → server returns a
short-lived quote → browser displays it → submit references the quote → server verifies the
basket and consumes the quote. The quote binds customer, company, POS Profile, price list,
item codes/quantities/UOMs, warehouse, eligible rule versions, coupon, calculated outcome,
expiry and engine version. Any post-quote mutation invalidates it.

**Offline:** before disconnection the device receives a server-recorded snapshot — snapshot ID
and immutable hash, issue/expiry timestamps, company and POS Profile, price-list rows, taxes
and rounding policy, allowed deterministic rules, offline discount ceilings, manual-edit
limits, evaluator version. At checkout the client records snapshot ID, exact calculation
inputs and outcome, receipt timestamp, device and user identity, offline sequence number and
applied offline-safe rule IDs.

At sync the server recalculates **using that historical snapshot**, not today's prices:

| Outcome | Action |
|---|---|
| Exact match | Submit normally |
| Rounding-only difference within policy | Submit, record reconciliation |
| Policy-permitted variance | Submit at receipt price, create exception |
| Impossible or manipulated | Quarantine for manager resolution |
| Any case | **Never silently alter the amount already collected** |

A signed snapshot does not make the browser trustworthy — a cashier can modify browser state.
**The signature protects the snapshot; recomputation protects the outcome.**

### Offline integrity — beyond retry deduplication

`pos_next/api/invoices.py:1048` `_ensure_offline_uniqueness` protects against ordinary retry
using a client-generated `offline_id`. That is deduplication, **not tamper resistance**. The
offline design must additionally consider registered device identity, cashier identity, a
monotonic device sequence, snapshot version, local-receipt vs server-sync timestamps, hash
chaining between offline receipts, replay and backdating protection, maximum offline duration,
maximum offline sales value, maximum per-line discount, and manager review after suspicious
gaps or sequence resets. This need not be heavyweight cryptography initially, but the
distinction must be explicit in the design.

---

## 5. ADR-4 — Composite free items (`hospitality_core`)

> A free composite item has zero customer revenue but remains a real inventory movement and
> incurs normal COGS. Components expand exactly once, stock is consumed exactly once, and
> free-item provenance propagates for reporting and audit **without** suppressing stock or
> accounting entries.

Propagating `is_free_item` must mean *"this stock consumption originated from a free
promotional parent"* — never *"ignore the component."*

**Acceptance:** parent selling rate and net revenue are zero; components carry no selling
revenue; stock ledger entries exist for stock components; COGS books normally; components are
not expanded by both apps; returns reverse stock and COGS correctly; cancellation reverses
exactly once; promotion profitability reports can associate component cost with the promotion;
serial/batch selection remains mandatory where applicable.

*Correction to the review:* `CODE_REVIEW.md` framed this as a `hospitality_core` defect. It is
not. It is an unstated policy, now stated.

---

## 6. ADR-5 — Promotion authoring authority

The management endpoints are **not unauthenticated** — they rely on Frappe DocType
permissions. The unresolved question is whether `Promotional Scheme.write` is the correct
authority for creating commercial liabilities from a POS session
(`posnext_promotions/api/promotions.py:13-34`).

**Endpoint inventory required before threat modelling.** Do not describe all 34 whitelisted
endpoints as "CRUD". Classify each as read-only, calculation, mutation, administrative, or
lookup/helper, then threat-model only the meaningful surfaces.

**Proposed role model:** Promotion Viewer (inspect, simulate) · Promotion Editor (create and
edit drafts) · Promotion Approver (approve and activate) · Promotion Administrator (cancel,
archive, exceptional changes) · Cashier (apply eligible promotions only; cannot author).

**Additional controls:** company-level user permissions; separate checks for Promotional
Scheme, Pricing Rule and POS Coupon rather than one shared gate; allowlisted mutable fields;
revalidation of linked items, warehouses, customers and companies; before/after audit logging;
approval required for *activation* rather than creation; offline snapshot invalidation when an
active rule changes; rate-limited mutation endpoints; and assurance that bulk helpers cannot
become indirect mutation paths.

---

## 7. Defect register

Severity · exploit prerequisites · confidence · affected flows. **All findings are
verified-by-reading and static analysis. None has been reproduced against a live database** —
closing that gap is Phase 1.

| # | Defect | Sev | Prerequisites | Confidence | Affected flow | Blocked by ADR-3? |
|---|---|---|---|---|---|---|
| a | `::` vs `:` — server-side one-time gate always falsy. Present in **both** repos: `posnext_promotions/api/apply_offers.py:1358` and `pos_next/api/invoices.py:3157` | Critical | Authenticated POS session | **Confirmed** — autoname is `format:{customer}:{pricing_rule}` | One-time offers | No |
| b | Coupon redemption in API layer not lifecycle hook; failure swallowed (`pos_next/api/invoices.py:1391`); promotions' copy dead (`coupon_engine.py:437`); unlocked read-modify-write; mid-request `db.commit()` | Critical | Concurrency, or non-POS submit path | Confirmed | Coupons | No |
| c | `apply_offers` re-admits client-named rules past `disable`/dates/company/one-time (`apply_offers.py:1386-1405`) | Critical | Authenticated session, a known rule name | Confirmed | All promotions | No |
| d | Coupon discount from client totals (`offers.py:1059` → `coupon_engine.py:394`) | Critical | Authenticated session | Confirmed | Coupons | **Yes** |
| e | Gift stock checked in preview only (`apply_offers.py:746`) | High | Concurrency or stale offline preview | Confirmed | Gift pool, GWP, free items | Partly |
| f | `before_install` force-deletes Module Defs (`install.py:15-20`) | High | Install on a site with prior `pos_next` | Confirmed | Install / migrate | No |
| g | Auth gate on Sales Invoice only; POS Invoice ungated (`hooks.py:67-79`) | High | Site using POS Invoice — **NexDine does** | Confirmed | Returns, overrides | No |
| h | PIN lockout fails open on cache error (`pin.py:201-227`) | High | Redis degraded | Confirmed | Authorization | No |
| i | Import-time ERPNext patch, `except Exception: pass` (`__init__.py:43`) | High | Any | Confirmed by probe: patch applies, but failure is indistinguishable from success | All pricing | No |

Medium and low findings 11–26 carry forward from `CODE_REVIEW.md` unchanged.

### Fix notes

**a — make it an invariant, not a fix.** One `make_redemption_key(customer, pricing_rule)`
helper; no key formatting at call sites; unique DB constraint on semantic fields; treat `name`
as an implementation detail; migration detection for both one- and two-colon historical rows
with duplicate checking before normalisation; cross-repository contract tests; and sweep
documentation for copied `::` claims. **The root cause is the docstring at
`pos_next/api/sales_invoice_hooks.py:119` and the `description` at
`one_time_customer_offer_usage.json:3`, both of which state the wrong format.** Correct them
or the next copy repeats the bug.

**b — immutable redemption ledger** (customer, pricing rule, source doctype, source document,
status, redeemed timestamp, reversed timestamp). `used` becomes a denormalised display value,
not the authority. On submit, inside the invoice transaction: lock or atomically reserve →
revalidate enabled/date/company/customer/limit → insert unique redemption → finish submission →
roll back everything on any failure. On cancel, reverse idempotently. No explicit commits
inside DocType utilities.

**c — delete the readmission path.** A client-named rule is a *preference*, not authorization.
Reload from the database and run the full eligibility pipeline.

**d — recalculate from invoice rows** after prices, quantities, taxes and existing pricing
rules are normalised. Define precisely what each coupon base means: net item total before tax,
net after item discounts, grand total before coupon, eligible-category subtotal, whether free
items contribute, whether tips / delivery / service charges contribute. Ambiguous "Grand Total"
and "Net Total" labels are a future accounting-defect source.

**e — validate at submission.** Must consider warehouse, batch and serial requirements,
projected quantity, negative-stock policy, concurrent transactions, product bundles and
composite items, UOM conversion, and hospitality ingredient consumption. Where strict
reservation is unavailable, normal ERPNext stock validation must still be allowed to abort.
**Do not catch and continue.**

**f — install preflight, not deletion.** Detect conflicts, report the owning app and affected
DocTypes, abort with a precise remediation message, migrate metadata only through a versioned
patch. Never delete unknown metadata to make an install pass.

**g — one shared gate** invoked from both Sales Invoice and POS Invoice lifecycle hooks. The
policy explicitly lists protected actions, applicable doctypes, draft/save/submit/cancel
points, document binding, offline behaviour and failure behaviour. Keep the existing grant
properties: session-bound, document-bound, action-bound, single-use, audited.

**h — fail closed.** Reject approval and tell the cashier the authorization service is
unavailable; or use a database-backed fallback counter; or permit only a stronger authenticated
manager flow while Redis is down. A four-digit PIN must never become unlimited-attempt because
infrastructure degraded.

**i — remove the patch** in favour of a documented extension point (see ADR-2). If temporarily
unavoidable: validate the supported ERPNext version, apply from an explicit initialization
hook, log success *and* failure, export health-check status, fail installation or startup when
the patch cannot apply, and add an assertion test proving the expected function is installed.
`except Exception: pass` is not acceptable for correctness-critical pricing.

---

## 8. Harness

Deliberately boring and reproducible. **CI must report exact discovered and executed test
counts** — "command exited successfully" is insufficient when 227 tests are spread across four
directories.

### Application matrices

| Matrix | Purpose |
|---|---|
| ERPNext + promotions | Proves the standalone claim — **delete this matrix if ADR-1 resolves to A or B**, and change the README rather than building fake optionality |
| ERPNext + `pos_next` | Protects current POS behaviour |
| ERPNext + `pos_next` + promotions | Primary composition |
| ERPNext + `pos_next` + promotions + `hospitality_core` | NexDine integration |
| Same composed site, alternate app ordering | Detects order-dependent hooks and assets |

### Layers

Static (compile, ruff, import probe) · Unit (pure calculation) · Frappe integration (real
DocTypes and transactions) · Concurrency (two simultaneous redemptions; two simultaneous gift
submissions) · Lifecycle (draft, submit, cancel, amend, return) · Browser (POS preview must
match submitted invoice) · Upgrade (install an older release, migrate, verify data) ·
Composition (every hook runs exactly once).

**Do not combine the 59-file formatting cleanup with money-path changes.** Establish a
formatting baseline in its own commit, then enforce it on new changes.

### Adversarial tests — required before declaring the money path safe

- A disabled rule sent by name is rejected
- An expired rule sent by name is rejected
- A rule belonging to another company is rejected
- Browser totals altered by ±1 or ±1,000 do not change the server result
- Two simultaneous uses of the final coupon allowance produce exactly one success
- Two simultaneous first-time redemptions produce exactly one success
- Cancellation reverses exactly one redemption; repeated cancellation is harmless
- Returns do not create additional allowance
- A gift going out of stock after preview fails at submit
- Redis failure denies PIN approval
- Both Sales Invoice and POS Invoice create audit rows
- Hospitality free composite items book no customer revenue and no unintended cost
- Every hook executes once regardless of app order
- Patch failure is visible in health checks and logs

---

## 9. NexDine composition

Three apps must not independently attach business logic to invoice events. One orchestrator
per lifecycle event, each handler idempotent and owning a distinct responsibility:

```
invoice.validate
  → normalize document
  → authorize protected actions
  → evaluate promotions
  → validate promotion stock
  → calculate hospitality consumption

invoice.on_submit
  → consume coupon / redemption
  → write promotion audit
  → record hospitality consumption
```

**JavaScript:** remove the global `pn_*` functions. Use namespaced modules or one form script
importing registered contributors. Asset inclusion must be explicit through hooks and build
manifests, **never dependent on `apps.txt` order**.

**Composite free items:** implement ADR-4.

---

## 10. Delivery

Parallel workstreams, not a linear sequence.

### Phase 0 — Establish facts; contain if needed

Deployment inventory: production and staging site list; `bench --site … list-apps` from every
managed site; installed app versions and commit hashes; whether any POS Profile has promotions
enabled; whether submitted invoices carry promotion or coupon fields; whether the app was
distributed to customer-managed installations.

- **If deployed:** containment ships before the harness — disable the four unsafe promotion
  classes, freeze new promotion rollout, preserve enough logging to identify affected invoices.
- **If confirmed undeployed:** Phase 0 becomes a pre-release gate; harness construction starts
  first.
- **Either way:** no production release may expose the four unsafe promotion classes before
  their server-side enforcement is complete.

**Minimum containment invariants:** never accept discount totals from the browser · never
accept a client-named rule as proof of eligibility · never treat a logging failure as
permission to grant value · reject rather than continue when redemption enforcement cannot
complete.

### Phase 1 — Decisions and harness, in parallel

ADRs 1–5 resolved. Harness built: reproducible bench install, exact test discovery count,
application matrices, static checks, initial characterization tests. **The harness does not
wait for every ADR.**

### Phase 2 — Independent fail-open fixes

Defects a, b, c, e, f, g, h, i plus ADR-5 authorization hardening. **None of these is blocked
by ADR-3.** They establish independent security invariants; some are later absorbed by the
evaluator. The large upstream design must not become a reason to leave small fail-open defects
untouched.

### Phase 3 — `pos_next` pricing contract

Authoritative online quote · versioned calculation result · submission verification ·
historical offline snapshots · capability classification · reconciliation queue.

### Phase 4 — Consolidated evaluator

One semantic evaluator · online server implementation · certified deterministic offline subset ·
shared test vectors · stable provider interface.

### Phase 5 — Composed system

Deterministic lifecycle orchestration · no duplicate doc events · no global JS collisions ·
hospitality composite policy · app-order permutation tests · upgrade and migration coverage.

### Phase 6 — Shadow rollout

**Online:** run old and new calculations, keep using the old result, record differences,
classify rounding versus semantic discrepancies, enforce the new result only once the
difference rate is understood.

**Offline:** permit only the approved subset initially, generate reconciliation reports, expand
one promotion type at a time.

---

## 11. Definition of production-ready

The app may run on a till only when **all** of the following hold:

- [ ] Server results do not depend on client totals or client eligibility claims
- [ ] Concurrent limits are enforced transactionally
- [ ] Free stock is validated at submission
- [ ] Both invoice types have equivalent authorization coverage
- [ ] Infrastructure failures do not weaken authorization
- [ ] Supported app combinations install and migrate from clean CI images
- [ ] Hook and asset behaviour does not depend on app ordering
- [ ] All tests run with a published count
- [ ] Every applied discount is explainable from a stored calculation version
- [ ] Failures deny value rather than log and continue

---

## 12. Open decisions

| # | Decision | Owner | Blocks |
|---|---|---|---|
| 1 | **ADR-1** — one engine or two. Criterion: *do you ship a till with no discounts, coupons or offers at all?* | Product | Phase 2 shape; whether defects are fixed or deleted |
| 2 | Deployment inventory | Deployment owner | Phase 0 branch |
| 3 | Endpoint classification for ADR-5 | Engineering | Authorization hardening scope |
| 4 | Offline manual-rate-override ceilings and authorization rules | Product + Finance | Phase 3 |
| 5 | Rounding-difference tolerance for sync reconciliation | Finance | Phase 3 |

---

*Derived from `CODE_REVIEW.md` (26 findings) plus cross-repository analysis of `pos_next` and
`hospitality_core` on this bench. All findings verified by reading and static analysis; none
reproduced against a live database.*
