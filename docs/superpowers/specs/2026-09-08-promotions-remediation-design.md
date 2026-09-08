# Promotions Remediation — Cross-Repository Architecture Design

**Date:** 2026-09-08 · **Baseline:** `posnext_promotions` @ `70be440`, `pos_next` @ working tree
**Scope:** `posnext_promotions`, `pos_next`, `nexdine`, `hospitality_core`
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
> **Ownership decision:** ⚠️ **REOPENED by NexDine — see ADR-1.** The prior recommendation
> (`posnext_promotions` as a `pos_next` companion) is **no longer viable**: `nexdine` is a
> 43,282-LOC restaurant ERP that does **not** use `pos_next` and has **no** promotions surface
> of its own. Binding promotions to `pos_next` would force every NexDine outlet to install an
> entire competing POS to get coupons. Option A is eliminated on that ground alone.
>
> **Deployment status is no longer unknown for NexDine.** Its README states it has been
> "running at scale, serving over 10+ outlets for the past 10 months." Phase 0's inventory
> must cover those outlets.
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

### NexDine changes the target

`nexdine` is not `pos_next` + `hospitality_core`, as an earlier draft of this analysis
assumed. It is an independent 43,282-LOC restaurant ERP (370 Python files, its own Vue POS at
`/pos`, kitchen display, analytics) with `required_apps = ["hrms"]` and **exactly one mention
of `pos_next` anywhere — in a comment.**

Two facts follow, and both are load-bearing:

1. **NexDine has no promotions surface at all.** Zero coupon, offer, promotion or discount
   DocTypes or modules. That is why promotions is wanted there.
2. **NexDine is POS Invoice-based** — 163 `POS Invoice` references against 13 for
   `Sales Invoice`. `posnext_promotions` is built almost entirely around Sales Invoice.

The consequence is stark. On a NexDine site, `posnext_promotions` registers **one** hook on
POS Invoice (`apply_min_max_price_discounts` on `validate`), and that hook self-disables via
`overrides/pricing_rule.py:714` whenever `pos_next` is installed. **On this bench, the
promotions app does nothing on NexDine at all.**

---

## 2. ADR-1 — DocType ownership and dependency direction

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

### The NexDine constraint

`nexdine` does not use `pos_next` and never will — it is a competing POS, not a layer on one.
It needs promotions. Therefore:

> **Any option that makes `posnext_promotions` depend on `pos_next` makes promotions
> unavailable to NexDine without installing a second, competing POS app.**

That eliminates Option A. It is recorded below so the reasoning is not lost.

### ~~Option A — Companion (`posnext_promotions` requires `pos_next`)~~ — ELIMINATED

Promotions declares `required_apps = ["erpnext", "pos_next"]`; `pos_next` keeps `POS Coupon`
and `One Time Customer Offer Usage`.

- **For:** smallest diff; no coordinated release; no metadata migration.
- **Against — fatal:** NexDine must install `pos_next` to get coupons. A 43k-LOC restaurant
  ERP pulling in an entire competing POS for two DocTypes is not a shippable architecture.
  The 2,201-LOC duplicate engine and 39 duplicated functions also remain, so every cross-app
  defect is fixed in two places rather than deleted once.

### Option B — Consolidation (`pos_next` requires `posnext_promotions`)

Promotions takes `POS Coupon` and `One Time Customer Offer Usage` via two re-home patches
(precedent: `patches/v2_3_0/rehome_gift_pool_item_doctype.py`). `pos_next` deletes its
duplicate engine, its promotion DocTypes, its duplicate `doc_events` and its `pn_*` JS
globals. The three dead DocTypes are dropped.

- **For:** −2,201 LOC. One engine, one coupon table, one truth. **NexDine installs promotions
  alone and gets everything**, with no `pos_next`. These conflicts resolve *by deletion*: the
  `::` bug's second call site, duplicated `record_one_time_offer_usage`, the `pn_*` global
  collision, the `apps.txt` ordering requirement, the `"pos_next" in get_installed_apps()`
  branch, and the split-brain coupon counter.
- **Against:** `pos_next` can no longer be installed alone. Coordinated two-repo release and
  live metadata migration. Promotions must additionally become POS-Invoice-native — see
  **ADR-6**, which it is not today.

### Option C — `promotion_core` extracted now, not deferred

A third neutral app owning coupons, redemption and the eligibility/calculation engine.
`pos_next`, `posnext_promotions` and `nexdine` all depend on it.

- **For:** no app depends on a competing POS; each consumer takes only what it needs; the
  cleanest boundary, and the one that survives a fourth POS product.
- **Against:** the largest migration — moving live metadata between modules while preserving
  table names, updating hooks/fixtures/roles/workspaces, defining which app installs and
  uninstalls the shared core, and testing removal and reinstall across app combinations.
  Three repos released in step rather than two.

### Recommendation

**Option B now, with Option C as the declared destination** — noting that B and C are the
same metadata migration performed once or twice. If a fourth consumer of promotions is
foreseeable, do **C directly and skip B**: the incremental cost over B is mostly naming and
one extra repository, whereas redoing B as C later pays the migration twice.

NexDine's existence is itself evidence that a fourth consumer is plausible.

### Sequencing

Neither B nor C is a first production fix. Phase 1's harness and Phase 2's fail-open defects
precede it. What NexDine changes is the *destination*, not the order — Option A can no longer
be the interim shape, so the interim remains the status quo (promotions installed alongside
`pos_next`, dependency undeclared) until the migration lands. That is acceptable only because
Phase 2 removes the exploitable paths first.

### Decision criteria

1. **Is `nexdine` a committed consumer of `posnext_promotions`?**
   Yes (the premise of this revision) → B or C; A is off the table.
   No → A becomes viable again and the prior staged recommendation stands.
2. **Is a third POS product likely to need promotions?** Yes → go straight to C.

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

## 5. ADR-4 — Composite / recipe free items (`hospitality_core` **and** `nexdine`)

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

*Scope correction:* the policy applies to **two** independent expanders —
`nexdine.nexdine.hooks.nexdine_recipe_stock` and
`hospitality_core.api.composite_item_utils.process_composite_items_in_invoice`. On a site
running both, "expanded exactly once" is an integration test, not a unit test.

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

## 7. ADR-6 — POS Invoice parity (NexDine blocker)

`posnext_promotions` is Sales-Invoice-native. NexDine is POS-Invoice-native. Today the app
registers **one** hook on POS Invoice, and it self-disables.

| Capability | Sales Invoice | POS Invoice | Effect on NexDine |
|---|---|---|---|
| Min/Max cross-cart discounts | `validate` | `validate` — **but returns early when `pos_next` is installed** (`overrides/pricing_rule.py:714`) | **does not run** |
| Authorization gate | `before_submit` | **absent** | no approval on returns, overrides or price edits |
| One-time redemption recording | `on_submit` | **absent** | one-time offers never recorded → infinitely reusable |
| One-time release on cancel | `on_cancel` | **absent** | ledger never reversed |
| Free-bundle qty combining | `validate` | **absent** | free product bundles mis-priced |

**Net: on a NexDine site with `pos_next` also installed, `posnext_promotions` does nothing.**

### Required

1. **Doctype-agnostic lifecycle registration.** A single table of `(doctype, event, handler)`
   derived from one source, so Sales Invoice and POS Invoice cannot drift. The current
   asymmetry exists because each hook was added by hand.
2. **Delete the `"pos_next" in frappe.get_installed_apps()` branch** (`overrides/pricing_rule.py:714`).
   Under ADR-1 Option B or C there is exactly one implementation, so the guard has nothing to
   defer to. It is the reason the one hook NexDine does get is inert.
3. **POS Invoice free-item and stock semantics** differ from Sales Invoice — POS Invoice
   carries its own stock and consolidation path via POS Invoice Merge Log. Submit-time gift
   validation (defect e) must be written against both, not ported.
4. **NexDine's own recipe expansion.** `nexdine.nexdine.hooks.nexdine_recipe_stock.before_submit`
   is NexDine's composite/recipe consumption — **separate from `hospitality_core`'s**. ADR-4's
   free-composite policy applies to it independently, and on a site running both, ADR-4's
   "expanded exactly once" acceptance condition has two candidate expanders.

### NexDine-side defects found

**N1 — open-ended promotions are invisible offline.** `nexdine/nexdine/api/nexdine_offline.py:356-374`
`_fetch_active_offers` filters:

```python
filters={"disable": 0, "valid_from": ["<=", today], "valid_upto": [">=", today]}
```

A Pricing Rule with a `NULL` `valid_upto` — an open-ended promotion, the common case — fails
`>= today` and is silently excluded from the offline cache. Same for `NULL valid_from`. Fix:
`["in", [None, ...]]` handling or an explicit `or_filters` on null.

**N2 — the offline offer payload cannot express eligibility.** The same function returns only
`name, title, apply_on, rate_or_discount, discount_percentage, rate`. No items, item groups,
brands, `min_qty` or `min_amt`. Any rule scoped to anything narrower than "everything" cannot
be evaluated correctly offline from this payload. This is the natural integration seam:
`posnext_promotions.api.offers.get_offers` already produces the complete, pre-expanded payload
this needs (ADR-3's offline-safe subset).

---

## 8. Defect register

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
| j | **POS Invoice hook gap** — 1 of 5 capabilities registered, and that one self-disables (ADR-6) | Critical **for NexDine** | Site is POS-Invoice-based | Confirmed | Everything, on NexDine | No |
| N1 | `_fetch_active_offers` excludes `NULL valid_upto` (`nexdine/nexdine/api/nexdine_offline.py:356-374`) | Medium | Open-ended promotion | Confirmed | NexDine offline offers | No |
| N2 | Offline offer payload carries no eligibility scope (same function) | Medium | Any scoped rule | Confirmed | NexDine offline offers | Partly |

Medium and low findings 11–26 carry forward from `CODE_REVIEW.md` unchanged.

### Fix notes

**a — make it an invariant, not a fix.** One `make_redemption_key(customer, pricing_rule)`
helper; no key formatting at call sites; unique DB constraint on semantic fields; treat `name`
as an implementation detail; duplicate checking on the semantic fields before adding the
constraint; cross-repository contract tests; and sweep
documentation for copied `::` claims. **The root cause is the docstring at
`pos_next/api/sales_invoice_hooks.py:119` and the `description` at
`one_time_customer_offer_usage.json:3`, both of which state the wrong format.** Correct them
or the next copy repeats the bug.

**Confirmed at framework level, 2026-09-08.** `frappe/model/naming.py:565-583`
`_format_autoname` strips only the `format:` prefix and substitutes only `{...}` params,
returning the result verbatim — its docstring states it is *"independent of remaining string
or separators."* So the name always carries exactly one colon, for every row ever created.
**Two-colon rows cannot exist**, so migration needs duplicate detection only, not format
detection.

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

## 9. Harness

Deliberately boring and reproducible. **CI must report exact discovered and executed test
counts** — "command exited successfully" is insufficient when 227 tests are spread across four
directories.

**Do not invent this.** `nexdine/.github/workflows/ci.yml` is a complete, working Frappe v15
harness: MariaDB 10.6 and Redis 7 services, Python 3.12 and Node 20, pip and yarn caching,
`bench init --frappe-branch version-15`, sequential `bench get-app` / `install-app` for
payments, erpnext, hrms and the app under test, `bench build`, then
`bench --site test_site run-tests --app <app>` with `allow_tests true`, plus a separate
frontend job. Copy it and change the app list per matrix. This retires the largest single
blocker in the project at near-zero cost.

### Application matrices

| Matrix | Purpose |
|---|---|
| ERPNext + promotions | Standalone. **Required** under ADR-1 Option B or C — it is the shape NexDine installs |
| ERPNext + `pos_next` (+ promotions under B/C) | Protects current `pos_next` behaviour |
| ERPNext + `pos_next` + promotions | Sales Invoice composition |
| ERPNext + hrms + `nexdine` + promotions | **NexDine composition — POS Invoice path.** The one that matters for this revision |
| ERPNext + hrms + `nexdine` + `pos_next` + promotions | Both POS products on one site (current bench shape) |
| ERPNext + `nexdine` + `hospitality_core` + promotions | Two composite/recipe expanders — ADR-4 "expanded exactly once" |
| Any composed site, alternate `apps.txt` ordering | Detects order-dependent hooks and assets |

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

## 10. Composed-site orchestration

Multiple apps must not independently attach business logic to invoice events. On the current
bench, **`nexdine`, `pos_next`, `hospitality_core` and `posnext_promotions` can all be
installed together**, and `nexdine` alone registers 8 handlers across POS Invoice's 6 events.

One orchestrator per lifecycle event, each handler idempotent and owning a distinct
responsibility — and registered for **both** invoice doctypes (ADR-6):

```
invoice.validate
  → normalize document
  → authorize protected actions
  → evaluate promotions
  → validate promotion stock
  → calculate composite / recipe consumption

invoice.on_submit
  → consume coupon / redemption
  → write promotion audit
  → record composite / recipe consumption
```

**Ordering.** `apps.txt` currently reads `… pos_next, brainwise_fleet, nexdine,
hospitality_core, payments` — promotions is not installed on this bench at all. Any design
that depends on that ordering is a defect; ADR-2's provider registration and ADR-6's
doctype-agnostic table exist to remove the dependency.

**JavaScript.** Remove the global `pn_*` functions. Namespaced modules or one form script
importing registered contributors. Asset inclusion explicit through hooks and build
manifests, never `apps.txt` order. NexDine's POS is a separate Vue app at `nexdine/pos` — it
does not consume `pos_next`'s offer-strategy registry, so `public/pos/offer-strategies.js`
needs a second, framework-neutral delivery path or NexDine gets no offline promotion logic.

**Composite / recipe expansion.** Two independent expanders exist —
`nexdine.nexdine.hooks.nexdine_recipe_stock` and
`hospitality_core.api.composite_item_utils.process_composite_items_in_invoice`. ADR-4's
"components expanded exactly once" must be tested on a site running both.

---

## 11. Delivery

Parallel workstreams, not a linear sequence.

### Phase 0 — Establish facts; contain if needed

**Partially answered:** NexDine's README states it has been *"running at scale, serving over
10+ outlets for the past 10 months."* Those outlets are live systems. What remains unknown is
whether `posnext_promotions` is installed on any of them — it is **not** installed on this
bench (`sites/apps.txt` does not list it), which is weak evidence it is not yet deployed
anywhere, but not proof.

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

## 12. Definition of production-ready

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

## 13. Open decisions

| # | Decision | Owner | Blocks |
|---|---|---|---|
| 1 | **ADR-1** — Option B or C. Option A is eliminated by NexDine | Product | Everything downstream |
| 1b | Is a third POS product likely to need promotions? Yes → go straight to C | Product | Whether the metadata migration is paid once or twice |
| 1c | **ADR-6** — commit to POS Invoice parity | Engineering | Whether NexDine can use the app at all |
| 2 | Deployment inventory — **NexDine's README claims 10+ outlets for 10 months**, so at least that stack is live; confirm whether promotions is installed on any of them | Deployment owner | Phase 0 branch |
| 3 | Endpoint classification for ADR-5 | Engineering | Authorization hardening scope |
| 4 | Offline manual-rate-override ceilings and authorization rules | Product + Finance | Phase 3 |
| 5 | Rounding-difference tolerance for sync reconciliation | Finance | Phase 3 |

---

*Derived from `CODE_REVIEW.md` (26 findings) plus cross-repository analysis of `pos_next` and
`hospitality_core` on this bench. All findings verified by reading and static analysis; none
reproduced against a live database.*
