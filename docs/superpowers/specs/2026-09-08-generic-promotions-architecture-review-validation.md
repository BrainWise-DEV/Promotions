# Validation of the Generic Promotions Architecture Review

**Date:** 2026-09-08 · **Validates:** `2026-09-08-generic-promotions-architecture-review.md`
**Method:** every factual claim re-derived from source at `nexdine@03ebd4b`, `pos_next` working tree,
`posnext_promotions@70be440`, and ERPNext v15 on this bench. Commands published inline.
**Evidence level:** static source review. No claim below was reproduced against a live database.

---

## Verdict

**Accept the review. Its architectural conclusion is right and its corrections to PR #1 are
right.** All ten factual corrections are confirmed; all four blocking findings are confirmed
against source. PR #1 should not proceed to implementation planning in its current form.

Two qualifications, and six gaps.

**Qualification 1 — two replacement figures are as unreproducible as the ones they correct.**
The review is right that PR #1's LOC and invoice-reference counts were wrong. Its own
substitutes (42,912 LOC; 405/77 references) do not reproduce on this bench either. The review's
own remedy applies to itself: publish the command or drop the precise figure. Reproducible
values with method are given in §1.

**Qualification 2 — the review states a fact in its verified conclusions whose consequence it
never applies.** NexDine already re-derives prices server-side and never sets
`ignore_pricing_rule`. That makes the trust-boundary problem `pos_next`-specific rather than
generic, and it materially shrinks the critical path. See Gap 4.

---

## 1. Validation of the ten factual corrections to PR #1

| # | Correction | Verdict | Evidence |
|---|---|---|---|
| C1 | NexDine `/pos` is React 19 + TypeScript, not Vue; KOT Display is Vue | **CONFIRMED** | `pos/package.json`: react ^19.0.0, react-dom ^19.0.0, zustand ^5.0.6, dexie ^4.4.2, vite ^6.2.0, vitest ^3, typescript ~5.7.2. `kotdisplay/package.json`: vue ^3.3.4 |
| C2 | PR #1's LOC figure is wrong | **CONFIRMED — see below** | PR #1's `43,282` came from `find … -name '*.py' -print -exec cat {} +`, where `-print` fed every *filename* into the count and `node_modules` was not excluded |
| C3 | "Exactly one mention of `pos_next`" is false | **CONFIRMED** | `git grep -li pos_next` → 4 files (`docs/2026-04-14-offline-mode-design.md`, `nexdine/nexdine/api/nexdine_qz.py`, `pos/src/components/offline/CacheStatus.tsx`, `pos/src/lib/offline/mutex.ts`), 14 occurrences |
| C4 | 163 / 13 not reproducible | **CONFIRMED — see below** | PR #1's figure came from an unfiltered working-tree grep including `node_modules` |
| C5 | "No promotions surface at all" is too broad | **CONFIRMED** | `additional_discount_percentage` / `discount_amount` in `nexdine_offline.py`, `nexdine_order.py`, `nexdine_pos/api.py`; a real TS totals engine at `pos/src/lib/totals/engine.ts` with `engine.test.ts` and `fallback.ts` |
| C6 | "Does nothing" needs narrowing | **CONFIRMED** | With NexDine alone the POS Invoice Min/Max handler runs; `overrides/pricing_rule.py:714` suppresses it only when `pos_next` is installed. The import-time ERPNext patch also applies regardless |
| C7 | "Strict functional superset" overstates | **CONFIRMED** | 39 shared function *names* and 8 extra modules establish surface, not behavioural equivalence. Contract tests must precede deletion |
| C8 | `POS Offer` is not safe to drop | **CONFIRMED — PR #1 was wrong** | `git grep -l "POS Offer"` → `pos_next/pos_next/doctype/pos_coupon/pos_coupon.js`, `pos_next/pos_next/workspace/posnext/posnext.json`, `README.md`. PR #1 searched Python and `POS/src` but **not desk JS or workspace JSON**. `POS Offer Detail` and `POS Coupon Detail` are genuinely unreferenced |
| C9 | One-time key migration contradiction | **CONFIRMED** | `CODE_REVIEW.md` said "detect both one- and two-colon rows"; the spec proved via `frappe/model/naming.py:565-583` that two-colon names cannot be generated. Both statements shipped |
| C10 | Stale NexDine appendix | **CONFIRMED** | `CODE_REVIEW.md`'s appendix still reasons from "NexDine = `pos_next` + `hospitality_core`". An errata banner is insufficient because the appendix draws architectural conclusions from the discarded premise |

### C2 — reproducible LOC

| Method | Result |
|---|---|
| `git ls-files '*.py' \| xargs cat \| wc -l` (tracked only) | **27,543** across **368** files |
| `find . -path '*/node_modules/*' -prune -o -name '*.py' -print0 \| xargs -0 cat \| wc -l` | 35,017 |
| PR #1's (broken) command | 43,282 |
| Review's figure | 42,912 — not reproducible here |

**Recommendation:** state *"368 tracked Python files, 27,543 lines (`git ls-files '*.py' | xargs cat | wc -l`)"* or drop the figure. The argument does not depend on it.

### C4 — reproducible invoice-reference counts

| Method | POS Invoice | Sales Invoice | Ratio |
|---|---|---|---|
| `git grep -o '"POS Invoice"' -- '*.py'` (quoted DocType strings, tracked) | **178** | **18** | ~10:1 |
| `git grep -o 'POS Invoice'` (raw, all tracked files) | 828 | 207 | 4:1 |
| PR #1 | 163 | 13 | not reproducible |
| Review | 405 | 77 | not reproducible |

The quoted-string method is the meaningful one — it counts DocType references rather than prose — and it supports the qualitative conclusion more strongly than either published figure.

---

## 2. Validation of the four blocking findings

### BF1 — coupon models are incompatible · **CONFIRMED. The most important finding in the review, and PR #1 missed it entirely.**

| Document | `coupon_code` | Maintained by |
|---|---|---|
| **POS Invoice** | **native ERPNext** `Link` → `Coupon Code` | ERPNext: `validate_coupon_code` (`pos_invoice.py:230`), `update_coupon_code_count(…, "used")` (`:252`), `"cancelled"` (`:289`) |
| **Sales Invoice** | no native field; `posnext_promotions` adds `coupon_code` as **`Data`** holding a `POS Coupon` code | this app |

Verified: `pos_invoice.json` declares `coupon_code` as `Link`/`Coupon Code`; `sales_invoice.json` declares no coupon field; `posnext_promotions/.../custom/sales_invoice.json` adds `coupon_code` as `Data`.

**This invalidates PR #1's ADR-6 as written.** "Register the same hooks on POS Invoice" cannot work: writing a `POS Coupon` code into POS Invoice's `coupon_code` hands ERPNext a string it resolves as a `Coupon Code` document name. NexDine also already has a *working* ERPNext coupon lifecycle on that field, so the naive approach yields two coupon systems on one document.

The review's demand for a canonical-coupon-model ADR before anything else is correct.

### BF2 — hook parity is not consumer integration · **CONFIRMED**

`nexdine_order.py:745`:

```python
_run_kot_with_failure_surface(inv, customer, table, items, past_item, comments)
```

`items` is the client list, passed alongside the authoritative `inv`. A promotion-added free line exists on the invoice and consumes recipe stock but never reaches the kitchen. Real, and it is a fulfilment defect, not an accounting one — which is why hook parity does not reach it.

### BF3 — bill splitting is absent from the design · **CONFIRMED**

`_copy_item_fields` (`nexdine_order.py:1003-1021`) copies 15 fields:
`item_code, item_name, qty, rate, price_list_rate, base_price_list_rate, comment, custom_course, cost_center, custom_is_addon, custom_addon_group_id, custom_addon_group, custom_addon_group_name, custom_addon_surcharge, custom_is_default_selection`.

**None** is `is_free_item`, `pricing_rules`, `discount_percentage`, `discount_amount`, or any promotion or calculation identity. Splitting a bill destroys promotion provenance. PR #1 did not consider splitting at all.

### BF4 — recipe/composite ownership underspecified · **CONFIRMED**

`nexdine_recipe_stock.before_submit` creates a separate Material Issue Stock Entry; `hospitality_core` uses a different mechanism. PR #1 called both "expanders", which hides the real risk. The review's reframing to **stock-consumption ownership**, with the invariant *one economic inventory consumption*, is correct and strictly better.

---

## 3. Gaps — material issues absent from the review

### Gap 1 — POS Invoice consolidation is the accounting document, and it is not in the design · **HIGH**

The review mentions consolidation twice, neither time architecturally. But on the POS Invoice path the durable accounting document is a **Sales Invoice** created later by POS Invoice Merge Log (`pos_invoice_merge_log.py:350` `frappe.new_doc("Sales Invoice")`, `:160` `consolidated_invoice`). NexDine references merge logs in 10 files.

Three consequences, none addressed:

**(a) Promotions' Sales Invoice hooks fire a second time.** `posnext_promotions` registers `record_one_time_offer_usage` on Sales Invoice `on_submit` and `apply_min_max_price_discounts` on `validate`. Consolidation submits a Sales Invoice, so both run again over rows already priced on the POS Invoice. Under ADR-6's parity proposal, a one-time redemption is recorded once on POS Invoice submit and again on consolidation. Today this is idempotent only by accident — the autoname collision and `ignore_if_duplicate=True`.

**(b) Discount provenance is destroyed at consolidation.** `merge_pos_invoice_into` (`:234-238`):

```python
item.rate = item.net_rate
item.amount = item.net_amount
item.base_amount = item.base_net_amount
item.price_list_rate = 0
```

`price_list_rate` is **zeroed** and the discount is baked into `rate`. The consolidated Sales Invoice retains no record of list price or discount. PR #1's definition-of-ready — *"every applied discount is explainable from a stored calculation version"* — is **unachievable on the POS Invoice path** unless the calculation identity is carried across the merge explicitly.

**(c) `apply_min_max_price_discounts` silently no-ops on consolidated invoices**, because `_apply_discount` (`overrides/pricing_rule.py:783`) returns early when `base_rate <= 0`. Correct behaviour, reached by accident, undocumented, and one refactor away from becoming a double-discount.

**Required:** an ADR covering which document is authoritative for promotion state; how calculation identity, `is_free_item` and rule provenance survive the merge; which hooks are suppressed on consolidated invoices; and how returns via `consolidated_credit_note` reverse entitlements exactly once.

### Gap 2 — ERPNext's own coupon counter carries the same lost-update defect · **HIGH**

The review cites `update_coupon_code_count` as the mechanism that makes POS Invoice coupons work. It does not note that the mechanism is defective (`erpnext/accounts/doctype/pricing_rule/utils.py:756-772`):

```python
coupon = frappe.get_doc("Coupon Code", coupon_name)
if coupon.used < coupon.maximum_use:
    coupon.used = coupon.used + 1
    coupon.save(ignore_permissions=True)
```

Read-modify-write, no `for_update`. **This is the counter NexDine relies on today**, so the concurrency defect PR #1 raised for `pos_next` exists in three places, and the third is ERPNext core. If the canonical ADR resolves toward ERPNext `Coupon Code` (the review's preferred option 1), the redemption ledger must be the authority — the review says so, but not that it is *repairing an existing live defect*, not merely modernising.

**Also:** `if coupon.used < coupon.maximum_use` with `maximum_use = 0` takes the else branch and throws *"Allowed quantity is exhausted"*. Whether an unlimited-use ERPNext coupon is usable on POS Invoice needs an explicit test.

### Gap 3 — currency precision and rounding across splits · **MEDIUM**

Neither document addresses precision. `posnext_promotions/overrides/pricing_rule.py:825-834` `_rate_precision` returns a hardcoded `2` for any payload lacking a `precision()` method — wrong for 3-decimal currencies. Splitting compounds this: BF3 requires allocating a frozen monetary effect across siblings, which is a rounding-allocation problem (largest-remainder or equivalent) that must sum exactly to the original. The split ADR needs a stated allocation rule and a test that sibling totals reconcile to the parent to the last minor unit.

### Gap 4 — NexDine is already trust-safe, which shrinks the critical path · **MEDIUM (scope reduction)**

The review lists this as a verified conclusion but its delivery plan does not exploit it.

`_populate_invoice_items` (`nexdine_order.py:1395-1425`) sets `invoice.items = []`, rebuilds every row from server-side `Item Price` lookups, and **throws** when no price exists. NexDine **never sets `ignore_pricing_rule`** (`git grep -n ignore_pricing_rule -- '*.py'` → no match), so ERPNext's pricing engine remains live.

That is the opposite of `pos_next`, which sets `ignore_pricing_rule = 1` (`pos_next/api/invoices.py:786`) and comments *"Trust frontend's price_list_rate"* (`:839`).

**Consequence:** the trust-boundary remediation is a **`pos_next` defect**, not a generic redesign. NexDine already satisfies the invariant. Phase 3 should be re-scoped as `pos_next`-specific, and NexDine can receive promotions **without waiting for it** — which reorders the delivery plan in the product's favour. Presenting it as cross-cutting overstates the work and delays the consumer that is ready.

### Gap 5 — the offline reclassification is right, and one consequence is missing · **LOW**

Confirmed: `db.offers` is inert. `pos/src/lib/offline/preload.ts` only `clear()`s and `bulkPut()`s it (`:319-328`); the sole other references are schema declarations in `db.ts:160,247,327`. No read consumer exists. Reclassifying N1/N2 as latent is correct, and warning against wiring `get_offers` straight into the cache is correct.

Missing: because the cache is inert, **N1 and N2 are free to fix now**, before any ADR closes. Correcting the NULL-date filter and widening the payload cannot regress behaviour that nothing consumes. They belong in Phase 2, not behind the offline bundle design.

### Gap 6 — no rollback or abort criteria · **MEDIUM**

Both documents define a destination and a phase sequence. Neither defines what makes a phase fail, nor how to reverse a landed phase. Given NexDine is live at 10+ outlets and the plan includes live metadata migration between apps, the plan needs, per phase: preconditions, an explicit abort trigger, a reversal procedure, and who decides. The shadow-rollout phase in particular needs a stated difference-rate threshold above which enforcement does not proceed.

---

## 4. Corrections to the review itself

1. **Publish or drop the LOC and reference figures** (§1). The review's own standard, applied to its own numbers.
2. **The CI claim checks out** — `03ebd4b` is dated `2026-07-08 14:53:31 +0300`, matching the review.
3. **"NexDine has no complete promotions engine" is the right formulation** and the review already uses it in most places; one instance still reads as absolute.
4. **Gap 4 should be promoted from a bullet in Verified Conclusions to a scope decision**, because it changes the delivery order.

---

## 5. Changes required in PR #1

Accepted in full, plus the gaps above:

- Replace "Vue POS" with "React/TypeScript POS" throughout; KOT Display is Vue.
- Replace LOC and reference counts with the published-method figures in §1, or drop them.
- Replace "exactly one mention of `pos_next`" with "no imports, hooks, declared dependency or runtime calls into `pos_next`".
- Replace "no promotions surface at all" with "no dedicated promotions engine or authoring model".
- Narrow the "does nothing" claim per C6.
- Replace "strict functional superset" with "broader implementation intended to supersede the duplicated engine; contract tests required before deletion".
- Un-mark `POS Offer` as dead; keep `POS Offer Detail` and `POS Coupon Detail` as candidates pending row counts, Dynamic Links, fixtures and reports.
- Resolve the one-time key contradiction to a single conclusion: enforce uniqueness on `customer + pricing_rule`; no name normalisation unless manually named or imported rows are demonstrated.
- **Delete and rewrite** the NexDine appendix in `CODE_REVIEW.md` rather than annotating it.
- **Withdraw ADR-6 as written.** Hook parity is unsafe until the canonical coupon ADR closes (BF1) and consolidation is designed (Gap 1). Replace it with the adapter contract.
- Adopt Option C directly. Do not perform Option B and repeat its migration.

---

## 6. Recommended next decisions, in order

1. **Canonical coupon model ADR** (BF1). Blocks everything on the POS Invoice path.
2. **Consolidation / authoritative-document ADR** (Gap 1). Blocks any hook design.
3. **Re-scope the trust boundary as `pos_next`-only** (Gap 4). Unblocks NexDine ahead of `pos_next`.
4. **Harness from `nexdine/.github/workflows/ci.yml`.** Independent of every ADR; start now.
5. **Phase 2 fixes, plus N1/N2** (Gap 5), which are free while the offer cache is inert.
6. **Split, KOT and stock-ownership ADRs** (BF2, BF3, BF4).
7. **Phase gates and rollback criteria** (Gap 6) before any live migration.
