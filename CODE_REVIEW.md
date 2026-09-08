# Full Code Review — `posnext_promotions` @ `main` (70be440)

Scope: every Python and JS file in the app (~16.4k LOC), plus `hooks.py`, `patches.txt`,
`install.py` and the packaging config. Reviewed for correctness on the money path,
security, cross-app safety and dead weight — in that order.

Every finding below names `file:line` and the concrete way it breaks. Nothing here is
"consider extracting this".

> **ERRATA — 2026-09-08.** Findings 1 and 4 below are corrected by cross-repository
> analysis performed after this review was first published. Finding 1 was **overstated**;
> finding 4 was **understated** and is worse than described. Both corrections are marked
> inline. The architecture response lives in
> [`docs/superpowers/specs/2026-09-08-promotions-remediation-design.md`](docs/superpowers/specs/2026-09-08-promotions-remediation-design.md),
> which supersedes this document where they differ.

**Tooling evidence (run on a clean clone of `main`):**

| Check | Result |
|---|---|
| `python -m compileall` | pass |
| `ruff check .` | **10 errors** |
| `ruff format --check .` | **59 of 85 files would be reformatted** |
| `.github/` CI | **does not exist** — `.pre-commit-config.yaml` is never enforced |

So the repo's own pre-commit config fails on its own default branch.

---

## Severity summary

| # | Finding | Sev |
|---|---|---|
| 1 | Coupon redemption lives in `pos_next`'s API layer, not a lifecycle hook; failure is swallowed (**corrected**) | Critical |
| 2 | `apply_offers` re-admits client-named Pricing Rules past every gate | Critical |
| 3 | Coupon discount is computed from client-supplied totals | Critical |
| 4 | `::` vs `:` — the server-side one-time gate never fires, in **both** repos (**corrected, worse**) | Critical |
| 5 | Free-gift stock is checked only in the preview API, never at submit | High |
| 6 | `increment_coupon_usage` is a lost-update + mid-request `db.commit()` | High |
| 7 | `before_install` force-deletes Module Defs it does not own | High |
| 8 | ERPNext core is monkey-patched at import time, globally, silently | High |
| 9 | Authorization gate covers Sales Invoice only — POS Invoice is ungated | High |
| 10 | PIN lockout fails **open** when Redis errors | High |
| 11 | `pickle.loads` on cache-held authorization grants | Medium |
| 12 | `get_offers` swallows every exception and returns `[]` | Medium |
| 13 | `apply_min_max_price_discounts` swallows every exception | Medium |
| 14 | Min/Max hook self-disables on an `"pos_next" in get_installed_apps()` string test | Medium |
| 15 | Currency precision hardcoded to 2 in the POS path | Medium |
| 16 | Min/Max blended percentage is not clamped to `Item.max_discount` | Medium |
| 17 | Any user can self-enrol an authorization PIN | Medium |
| 18 | Grant TTL is refreshed on every consume | Low |
| 19 | `validate_coupon` returns the entire coupon document to the client | Low |
| 20 | Pricing Rule reads gated on *Promotional Scheme* permission | Low |
| 21 | `db.rollback()` + `throw(str(e))` in 9 whitelisted endpoints | Low |
| 22 | `check_coupon_code` raises `AttributeError` on a null code | Low |
| 23 | Lint, format and CI (see table above) | Low |
| 24 | Dead code: cleanup task, `setup_matrix_promotions`, coupon counters | Low |
| 25 | Tests scattered across four locations | Low |
| 26 | README's independence claim is contradicted by the code | Low |

---

## Critical

### 1. Coupon redemption is in the wrong layer, and its failure is swallowed

> **CORRECTED.** This finding originally claimed `POS Coupon.used` is never incremented.
> That was wrong: `pos_next/api/invoices.py:1391` does increment it. The real defects are
> narrower and are stated below.

`posnext_promotions/api/coupon_engine.py:437` and `:451` define `increment_coupon_usage` /
`decrement_coupon_usage`. Nothing in this app calls them — no `doc_events` entry exists in
`hooks.py:49-80`. They are dead code.

Enforcement exists only because `pos_next` supplies it, and it has three problems:

1. **Wrong layer.** `pos_next/api/invoices.py:1391` increments from the POS API, not from a
   Sales Invoice lifecycle hook. An invoice submitted from the desk, from a script, or
   through `hospitality_core`'s flows never increments the counter — the coupon limit
   silently does not apply to those paths.
2. **Failure is swallowed.** The caller wraps it in `try/except` that logs and continues, so
   a failed increment leaves the sale completed and the coupon un-capped.
3. **Lost update.** `pos_next/pos_next/doctype/pos_coupon/pos_coupon.py:181-201` is
   read-modify-write with no `for_update`, plus a `frappe.db.commit()` inside the invoice
   transaction. Two terminals redeeming the final allowance concurrently both read `used = 49`
   against `maximum_use = 50` and both write 50.

**Fix:** replace the mutable counter as the *authority* with an immutable redemption ledger,
keeping `used` as a denormalised display value; move consumption into the invoice transaction;
make the limit check atomic; and reverse idempotently on cancel. See ADR-2 and defect (b) in
the design spec.

### 2. `apply_offers` re-admits client-named Pricing Rules past every gate

`posnext_promotions/api/apply_offers.py:1386-1405`:

```python
if selected_offer_names:
    rule_map = {name: d for name, d in rule_map.items() if name in selected_offer_names}
    missing_selected = selected_offer_names - set(rule_map.keys())
    if missing_selected:
        extra_records = frappe.get_all("Pricing Rule", filters={"name": ["in", list(missing_selected)]}, ...)
        for record in extra_records:
            if record.coupon_code_based:
                continue
            rule_map[record.name] = record   # <-- no other check
```

`selected_offers` is a client argument on a `@frappe.whitelist()` endpoint
(`apply_offers.py:1083`). The main path above it (`:1341-1361`) carefully filters on
`coupon_code_based`, `one_time_per_customer` and prior redemption. This block re-adds any
rule the client names, checking only `coupon_code_based`. It does not check `disable`,
`valid_from`/`valid_upto`, `company`, `selling`, or `one_time_per_customer`.

**Failure scenario.** A cashier (or anyone with an authenticated session) reads a rule name
from `get_promotions`, then calls `apply_offers` with `selected_offers=["PRLE-0042"]` for a
Gift Pool rule that is disabled, expired, belongs to another company, or that this customer
already redeemed once. It lands in `rule_map`. `_apply_gift_pool_free_items`
(`apply_offers.py:538-560`) then iterates `rule_map` directly and grants free items for any
paid qty > 0 in the group — it never re-reads the rule's `min_qty`/`min_amt`/`disable`. The
cart comes back with free stock attached.

**Fix:** run `extra_records` through the same filter chain as `rule_records`, and re-assert
`disable = 0` plus the date window in the `get_all` filters.

---

### 3. Coupon discount is computed from client-supplied totals

`api/offers.py:1059` `calculate_coupon_discount(coupon_code, invoice_data, ...)` reads:

```python
grand_total = flt(invoice.get("grand_total") or 0)
net_total   = flt(invoice.get("net_total") or 0)
tax_amount  = flt(invoice.get("total_taxes_and_charges") or invoice.get("tax_amount") or 0)
```

straight out of the client payload, and hands them to `apply_coupon_discount`
(`coupon_engine.py:394-397`), where:

```python
base_amount = cart_total if coupon.apply_on == "Grand Total" else (net_total or cart_total)
...
discount = flt(base_amount) * flt(coupon.discount_percentage) / 100
```

The server never re-derives the total from Item Price. The same applies to the
`exclude_discounted` branch, which sums `rate * qty` off client-sent item rows.

**Failure scenario.** A 20%-off coupon with `min_amount = 500` and no `max_amount`. The
client posts `grand_total: 100000` for a 100 EGP cart and receives `discount: 20000`. Whether
that discount survives to the ledger depends entirely on the POS frontend and on ERPNext's
own invoice validation — the promotions API itself asserts nothing.

The same shape appears in `apply_offers.py:1160` where `price_list_rate` is taken as
`item.get("price_list_rate") or item.get("rate")` from the payload, with no comparison
against the profile's price list.

**Fix:** recompute `net_total`/`grand_total` server-side from `item_code` + `qty` + the POS
Profile's price list, and treat the client's numbers as a checksum to reject on mismatch,
not as input.

---

### 4. `::` vs `:` — the server-side one-time gate never fires, in both repos

> **CORRECTED — worse than originally reported.** This was first written as a fragile
> assumption about another repo's naming format. It is a live, confirmed defect, and it is
> present in `pos_next` as well as here.

The DocType's autoname is **one colon**:

```json
"autoname": "format:{customer}:{pricing_rule}"
```
*(`pos_next/pos_next/doctype/one_time_customer_offer_usage/one_time_customer_offer_usage.json:3`)*

Both call sites look up **two colons**:

- `posnext_promotions/api/apply_offers.py:1358`
- `pos_next/api/invoices.py:3157`

```python
frappe.db.exists("One Time Customer Offer Usage", f"{customer}::{record.name}")
```

That key can never exist, so the check is always falsy.

**Failure scenario.** A one-time-per-customer offer is redeemed. The server-side gate that is
supposed to block a second redemption evaluates falsy every time. Only the frontend's cached
`get_customer_one_time_redemptions` list (`api/offers.py:640`, which uses a proper field
filter and therefore works) prevents reuse — so any client that does not consult that cache,
or that is replayed offline, re-redeems freely.

**Root cause, and why fixing the two call sites is not enough.** The wrong format originates
in prose and was copied from it:

- `one_time_customer_offer_usage.json:3` `description`: *"the document name is the composite
  key `{customer}::{pricing_rule}`"*
- `pos_next/api/sales_invoice_hooks.py:119`: *"the doctype's composite name
  (`{customer}::{pricing_rule}`)"*

Both contradict the autoname three lines above one of them.

**Fix:** introduce a single `make_redemption_key(customer, pricing_rule)` helper, stop
formatting keys at call sites, add a unique database constraint on the semantic fields, treat
`name` as an implementation detail, correct both docstrings, and add a cross-repository
contract test. Migration must detect both one- and two-colon historical rows and check for
duplicates before normalising.

### 4b. `record_one_time_offer_usage` is unguarded against a missing DocType

Separately from the above: `api/one_time_usage.py:26-36` inserts into
`One Time Customer Offer Usage` with no existence check, while its cancel twin at `:40`
guards with `frappe.db.exists("DocType", ...)`. On a site without `pos_next` — which
`hooks.py:9` (`required_apps = ["erpnext"]`) and the README claim is supported — the insert
raises inside `on_submit` and the sale cannot be closed. Resolved either by declaring the
dependency or by consolidating ownership; see ADR-1.

## High

### 5. Free-gift stock is validated only in the preview API, never at submit

`_filter_out_of_stock_free_items` (`apply_offers.py:746`) is called once, from inside
`apply_offers` at `:1641`. `apply_offers` is a `@frappe.whitelist()` *preview* endpoint. There
is no `validate` or `before_submit` hook that re-checks free-item stock (`hooks.py:67-79`
registers only the free-bundle combiner, the min/max discount pass and the auth gate).

**Failure scenario.** Two tills preview a Gift Pool promotion at the same second, both see one
unit of the gift SKU available, both attach it, both submit. Stock goes negative, or ERPNext
throws at submit on the *second* till with a stock error the cashier cannot act on — the
promotion engine has already promised the gift to the customer standing there.

Offline mode makes this worse by design: the preview may be minutes or hours stale.

**Fix:** re-run the availability check on `Sales Invoice` `validate` for rows carrying
`is_free_item`, and drop or block there rather than only in the preview.

---

### 6. `increment_coupon_usage` is a lost update, and commits mid-request

`coupon_engine.py:437-449`:

```python
coupon = frappe.get_doc("POS Coupon", {"coupon_code": coupon_code.upper()})
coupon.used = (coupon.used or 0) + 1
coupon.db_set("used", coupon.used)
frappe.db.commit()
```

Read-modify-write with no `for_update=True` and no atomic `UPDATE ... SET used = used + 1`.

**Failure scenario (once finding 1 is fixed and this is actually called).** Two terminals
redeem the same coupon concurrently. Both read `used = 49` against `maximum_use = 50`, both
write 50. Two redemptions, one counted.

The `frappe.db.commit()` is separately dangerous: called from a `Sales Invoice` submit hook it
commits the *entire* in-flight transaction, including a partially written invoice. A later
failure in the same submit then cannot roll back cleanly. `decrement_coupon_usage` at `:451`
has both problems too.

**Fix:**

```python
frappe.db.sql(
    "UPDATE `tabPOS Coupon` SET used = used + 1 WHERE name = %s AND (maximum_use = 0 OR used < maximum_use)",
    coupon_name,
)
```

and check the affected row count instead of pre-reading. Drop the `commit()` — let the
document transaction own it.

---

### 7. `before_install` force-deletes Module Defs it does not own

`install.py:15-20`:

```python
def before_install():
    for module in ("POSNext Promotions", "POS Next Auth Gate"):
        if not frappe.db.exists("Module Def", module):
            continue
        frappe.delete_doc("Module Def", module, force=1, ignore_permissions=True)
```

`force=1` skips link validation.

**Failure scenario.** On a site where `pos_next` shipped `POS Next Auth Gate` (the comment at
`:11` says exactly this happened), installing this app deletes that Module Def while
`pos_next`'s DocTypes still point at it. Those DocTypes are now orphaned: they no longer
appear under any module, `bench migrate` on `pos_next` can fail on the missing link, and
`bench uninstall-app pos_next` will not clean them up.

The comment frames this as "reclaiming", but the operation is unconditional and destructive on
a doctype that may be shared.

**Fix:** only delete when the Module Def has no linked DocTypes from another app, or rename the
module rather than deleting it. At minimum, drop `force=1`.

---

### 8. ERPNext core is monkey-patched at import time, globally, silently

`posnext_promotions/__init__.py:33-56` mutates ERPNext in three places at package import:

```python
_promotional_scheme.price_discount_fields.append(_slab_field)   # :32
patch_get_other_conditions(pr_utils)                            # :42
_erpnext_pricing_rule.apply_price_discount_rule = _promo_apply_price_discount_rule  # :53
```

Three problems, in ascending order of severity:

1. **Silent failure.** `:43 except Exception: pass` — if `patch_get_other_conditions` breaks
   against a new ERPNext version, promotions quietly compute the wrong numbers with no log
   line at all. The other two log but still continue.
2. **Global blast radius.** Replacing `apply_price_discount_rule` changes pricing for *every*
   app on the bench and *every* selling document — Sales Order, Quotation, Delivery Note,
   subscriptions, the ERPNext desk — not just POS.
3. **Non-deterministic timing.** The patch lands whenever some code path first imports
   `posnext_promotions`. Frappe imports app modules lazily and per-worker. A background worker
   that prices a Sales Order without having imported this package uses *unpatched* ERPNext.
   Same site, same rule, different discount depending on which process handled the request.

The README's "place this app **after** `pos_next` in `sites/apps.txt` so this app's monkey-patch
wins" is an admission that correctness depends on file ordering.

**Fix:** move the patch into a Frappe lifecycle hook that runs deterministically per request
(`before_request` / `boot_session`) or, better, replace it with `override_doctype_class` on
Pricing Rule — which the app already uses (`hooks.py:39`) — so the behaviour is scoped and
explicit. Let the exceptions raise.

---

### 9. Authorization gate covers Sales Invoice only — POS Invoice is ungated

`hooks.py:67-79`: `before_submit: "...authorization.gate.enforce_document"` is registered on
`Sales Invoice` only. The same block registers `apply_min_max_price_discounts` on `POS Invoice`,
so the app clearly expects sites where POS Invoice is in use — and `hospitality_core` on this
bench posts POS Invoices.

**Failure scenario.** A site running stock ERPNext POS (or a hotel outlet posting POS Invoices)
configures a "Return requires manager approval" rule. Every return posted as a POS Invoice
submits with no approval, no PIN, and no row in POS Authorization Log. The control silently does
not exist on that document type.

**Fix:** register `enforce_document` on `POS Invoice` `before_submit` too, and make
`registry.for_doctype` the single source of which doctypes are gated.

---

### 10. PIN lockout fails **open** when Redis errors

`authorization/pin.py:201-227`:

```python
def is_locked_out(user):
    try:
        return bool(cache.get(cache.make_key(_lock_key(user))))
    except Exception:
        return False          # <-- not locked out

def register_failure(user):
    try:
        ...
    except Exception:
        frappe.log_error(...)
    return False              # <-- no lockout triggered
```

Both the counter and the lockout live only in Redis, and both degrade to "allow".

**Failure scenario.** Redis is unreachable, memory-pressured, or the key namespace is flushed.
`request_grant` (`api/authorization.py:96`) then accepts unlimited PIN attempts. A 4-digit PIN
(`DEFAULT_PIN_LENGTH = 4`) is 10,000 combinations; the `@rate_limit(limit=5, seconds=60)`
decorator is the only remaining brake, and it is per-endpoint rather than per-approver.

The rest of `pin.py` is well built — hashed via `update_password`, separate `__Auth` fieldname,
`delete_tracker_cache=False`, `AuthenticationError` deliberately not re-raised. This one
fail-open undoes it.

**Fix:** on cache failure, deny (`is_locked_out` returns `True`, `register_failure` returns
`True`) — refusing an approval is recoverable; an unmetered brute-force window is not. Or
persist the counter in the database.

---

## Medium

### 11. `pickle.loads` on cache-held authorization grants

`authorization/grants.py:18,40,44` serialises the grant with `pickle`. The grant is a flat dict
of strings (`action`, `approver`, `binding`, `requested_by`, `consumed_by`) — `json` covers it
exactly. Anything that can write to the Redis key namespace gets arbitrary code execution in the
Frappe worker.

**Fix:** `json.dumps` / `json.loads`. One-line change, no behaviour difference.

### 12. `get_offers` swallows every exception and returns `[]`

`api/offers.py:621-624`. A transient DB error, a missing column, a bad Item Group tree — any of
them make the endpoint return "this POS profile has no promotions". The cashier sees a clean
screen and charges full price. Silence is the wrong default on a money path.

**Fix:** log and re-raise, or return an explicit `{"error": ...}` the frontend can surface.

### 13. `apply_min_max_price_discounts` swallows every exception

`overrides/pricing_rule.py:760`. Registered on `validate` for Sales Invoice, Sales Order,
Quotation, Delivery Note and POS Invoice (`hooks.py:76-79`). Any failure inside the loop drops
**all** Min/Max discounts on the document, leaving only an orange `msgprint`.

Concretely: `_collect_min_max_rule_items` (`:846`) calls
`frappe.get_cached_doc("Pricing Rule", pr_name)` on names parsed out of `item.pricing_rules`. A
rule deleted after being attached to a cart raises `DoesNotExistError`, the broad handler eats
it, and every other Min/Max discount on that invoice silently vanishes.

**Fix:** catch per-rule inside the loop, not around the whole pass.

### 14. Min/Max hook self-disables on a string test against installed apps

`overrides/pricing_rule.py:714`:

```python
if method and "pos_next" in frappe.get_installed_apps():
    return
```

The app's own discount pass turns itself off whenever `pos_next` is present, deferring to
`pos_next.overrides.pricing_rule.apply_min_max_price_discounts`. There is no version check and
no assertion that the other implementation still exists.

**Failure scenario.** `pos_next` drops or renames that hook, or its implementation diverges.
`posnext_promotions` keeps deferring, and Min/Max discounts stop applying on Sales Order,
Quotation, Delivery Note and POS Invoice with no error.

**Fix:** decide ownership once. Either this app owns the hook (and `pos_next` removes its copy)
or it does not register the hook at all.

### 15. Currency precision hardcoded to 2 in the POS path

`overrides/pricing_rule.py:825-834`:

```python
def _rate_precision(item):
    getter = getattr(item, "precision", None)
    ...
    return 2
```

The POS payload is a plain `frappe._dict`, so it always takes the `return 2` branch, and
`_materialize_rate` rounds `rate`, `discount_amount` and `amount` to 2 decimals.

**Failure scenario.** Any 3-decimal currency (KWD, BHD, OMR, TND) — the POS preview shows a rate
rounded to 2dp while the submitted Sales Invoice recalculates at 3dp. Every discounted line
disagrees with what the cashier quoted, by up to 0.005 per unit.

**Fix:** resolve precision from the document currency
(`frappe.get_precision("Sales Invoice Item", "rate")` or the Currency's `smallest_currency_fraction_value`)
instead of defaulting to 2.

### 16. Min/Max blended percentage is not clamped to `Item.max_discount`

`overrides/pricing_rule.py:797-810` computes `blended_pct = total_discount / line_value * 100`
and assigns it to `item.discount_percentage` with no reference to `Item.max_discount`. The hook
runs on `validate`, i.e. *after* the controller's own validation pass — including ERPNext's
`validate_max_discount`.

The app's own offline JS comments the opposite invariant at
`public/pos/offer-strategies.js:70` — *"the server clamps against Item.max_discount
unconditionally"*. This path does not.

**Failure scenario.** An item capped at 20% carries a Min/Max rule with `rate` set well below
list price. `blended_pct` computes to 60%, is written after `validate_max_discount` has already
run, and the cap is not enforced.

Marked *plausible* rather than confirmed: it depends on ERPNext's exact validate ordering for the
document class in use, which was not executed here. Worth a targeted test either way — the
inconsistency with the JS comment is real regardless.

### 17. Any user can self-enrol an authorization PIN

`api/authorization.py:163-176`:

```python
if user == frappe.session.user:
    if pin_store.has_pin(user) and not pin_store.verify(user, current_pin):
        return {"success": False, ...}
else:
    frappe.has_permission("User", ptype="write", doc=user, throw=True)
```

When the caller has **no** PIN yet, the first branch's condition short-circuits and no check runs
at all.

**Failure scenario.** A user holds an approver Role (via `Has Role`) but was never issued a PIN —
which is how a manager keeps someone role-eligible but not yet able to approve
(`get_authorization_readiness` at `:77` exists precisely to report this "missing_pin" state). That
user calls `set_authorization_pin` on themselves, picks a PIN, and becomes an active approver with
no manager involvement.

**Fix:** first-time PIN enrolment should require `System Manager`, matching
`clear_authorization_pin` at `:182`.

---

## Low

### 18. Grant TTL is refreshed on every consume
`grants.py:96` — `consume()` rewrites the key with `_write`, which sets `ex=GRANT_TTL` again.
Repeated submits of the same document keep a 180-second grant alive indefinitely. Bounded by the
`consumed_by` reference pin, so the impact is small; still, the TTL should not reset.

### 19. `validate_coupon` returns the entire coupon document
`api/offers.py:1027` — `coupon.as_dict()` goes to the client, including `customer`, `company`,
`used`, `maximum_use` and every internal field. Return only what the cart renders.

### 20. Pricing Rule reads gated on *Promotional Scheme* permission
`api/promotions.py:248-268` — `get_promotion_details` calls
`check_promotion_permissions("read")`, which only checks `Promotional Scheme`, then returns a full
`Pricing Rule` document via `pr.as_dict()`. A role granted Promotional Scheme read but not Pricing
Rule read can read Pricing Rules.

### 21. `db.rollback()` + `throw(str(e))` in 9 whitelisted endpoints
`api/promotions.py:462, 568, 599, 619, 891, 966, 998, 1026, 1064`. `frappe.db.rollback()` discards
the whole request transaction — including anything a caller did before invoking this API — and
`frappe.throw(... str(e))` leaks internal exception text to the client. `frappe.throw` already
rolls back; the explicit call is redundant and wider than intended.

### 22. `check_coupon_code` raises `AttributeError` on a null code
`coupon_engine.py:15` — `coupon_code.upper()` before any null check. `validate_coupon`
(`offers.py:997`) validates `customer` and `company` but not `coupon_code`, so
`validate_coupon(None, cust, co)` is a 500, not a clean "invalid coupon".

### 23. Lint, format and CI
10 `ruff check` errors (7 unused imports, 1 unused local, 2 re-export style), 59 of 85 files fail
`ruff format --check`, and there is no `.github/` directory — so `.pre-commit-config.yaml` is
enforced only on developer machines that installed the hook. `ruff check --fix && ruff format .`
clears the first two; a 20-line GitHub Actions job makes it stick.

### 24. Dead code
- `tasks/cleanup_expired_promotions.py` (166 lines) — `hooks.py:82-83` says the schedule lives in
  `pos_next`, and there is no `scheduler_events` here. Nothing calls it.
- `increment_coupon_usage` / `decrement_coupon_usage` — see finding 1.
- `setup_matrix_promotions.py` (362 lines) — a demo-data seeder shipped in the runtime package;
  `frappe.only_for("System Manager")` gates it, but it does not belong in a production app.

Deleting all three removes ~600 lines and the maintenance question of whether they still work.

### 25. Tests scattered across four locations
`posnext_promotions/test_*.py` (7 files), `posnext_promotions/tests/` (2), `posnext_promotions/authorization/tests/` (7),
`posnext_promotions/api/test_authorization.py` (1). Pick one convention. With no CI (finding 23),
it is unknown whether all four locations are actually being run.

### 26. The README's independence claim is contradicted by the code
`README.md:5` — *"This app does **not** depend on `pos_next`."* Against that:

- `One Time Customer Offer Usage` — doctype owned by `pos_next`, used unguarded (finding 4)
- `POS Coupon` — doctype owned by `pos_next`; the entire coupon engine is built on it
- `public/pos/offer-strategies.js:5` — *"Loaded at runtime by pos_next's offer strategy registry"*
- `overrides/pricing_rule.py:714` — behaviour branches on whether `pos_next` is installed
- `README.md:19` — *"Place this app **after** `pos_next` in `sites/apps.txt`"*

The accurate statement is: no Python `import pos_next` and no `required_apps` entry, but a real
runtime dependency on `pos_next`'s doctypes, its JS registry and its position in `apps.txt`. The
README should say that; a reader provisioning a standalone site is currently misled.

---

## Appendix: using this in NexDine

NexDine runs `pos_next` + `hospitality_core` on ERPNext. Adding `posnext_promotions` to that
bench introduces the following concrete conflicts. Findings 4, 8, 9 and 14 above all bear on this
too.

### A. Duplicated `doc_events` with `pos_next`

`pos_next/hooks.py:154-181` already registers:

| Hook | `pos_next` | `posnext_promotions` |
|---|---|---|
| Sales Invoice `validate` | `overrides.pricing_rule.apply_min_max_price_discounts` | `overrides.pricing_rule.apply_min_max_price_discounts` |
| Sales Invoice `on_submit` | `sales_invoice_hooks.record_one_time_offer_usage` | `api.one_time_usage.record_one_time_offer_usage` |
| Sales Invoice `on_cancel` | `sales_invoice_hooks.release_one_time_offer_usage` | `api.one_time_usage.release_one_time_offer_usage` |
| SO / Quotation / DN / POS Invoice `validate` | `apply_min_max_price_discounts` | `apply_min_max_price_discounts` |

Frappe merges `doc_events` across apps, so **both** run. The min/max duplication is defused by the
`"pos_next" in get_installed_apps()` guard (finding 14) — a runtime string test, not a design.
The one-time-usage pair is not guarded: both handlers insert into the same table on every submit
(the second is absorbed by `ignore_if_duplicate=True`) and **both delete on cancel**. Redundant
today, ambiguous ownership tomorrow.

### B. Desk JS collides on shared global function names

Both apps register `doctype_js` for `Pricing Rule` and `Promotional Scheme`, and both files
declare module-scope globals with the same names:

| Global | `pos_next` | `posnext_promotions` |
|---|---|---|
| `pn_toggle_min_max` | `public/js/pricing_rule.js:14` | `public/js/pricing_rule.js` |
| `pn_sync_min_max` | `public/js/promotional_scheme.js:19` | `public/js/promotional_scheme.js` |

Frappe loads both files into the same form bundle. Two `function pn_sync_min_max(frm)` declarations
in one scope means the later definition silently wins for **both** apps' `refresh` handlers. Which
one wins is decided by app order in `sites/apps.txt` — the exact ordering dependency the README
already asks for. The `pos_next` bodies are not equivalent to the `posnext_promotions` ones, so the
Promotional Scheme form behaves differently depending on install order.

**Fix before shipping to NexDine:** namespace the functions per app
(`posnext_promotions_sync_min_max`), or delete the `pos_next` copies as part of adopting this app.

### C. `override_doctype_class` on Pricing Rule is a single slot

`posnext_promotions/hooks.py:39` claims `Pricing Rule`. `pos_next/hooks.py:138` claims
`Sales Invoice`. No clash today. But the slot is exclusive per doctype across the whole bench: if
NexDine ever needs its own Pricing Rule controller, or a fourth app claims it, the install fails or
one override silently loses. Worth recording as an owned resource in the NexDine architecture
notes.

### D. Free items × `hospitality_core` composite items

`hospitality_core/hooks.py` runs `composite_item_utils.process_composite_items_in_invoice` on both
`POS Invoice` and `Sales Invoice` `on_submit`. It walks `doc.items`, and for anything with
`is_composite_item` creates a Material Consumption stock entry for the recipe ingredients.

It does not check `is_free_item`.

> **CORRECTED.** This was originally framed as a `hospitality_core` defect. It is not — a free
> dish genuinely does consume ingredients and genuinely does incur COGS, so consuming stock at a
> zero-revenue line is the economically correct behaviour. What is missing is a *stated policy*,
> not a fix.

The policy is now stated in ADR-4 of the design spec: zero customer revenue, normal stock
consumption, normal COGS, components expanded exactly once, and `is_free_item` provenance
propagated for reporting without suppressing stock or accounting entries. The open risks are
double expansion (both apps walking the same rows) and reversal on return or cancel — both of
which need tests before Gift Pool or GWP is enabled on a NexDine outlet.

### E. Room-charge posting and header-level discounts

`hospitality_core.api.pos_bridge.process_sales_invoice_room_charge` posts on `Sales Invoice`
`on_submit`. `apply_offers` returns `additional_discount_percentage` / `discount_amount` /
`apply_discount_on` (`apply_offers.py:1660-1664`) for the **frontend** to apply to the invoice
header. If the frontend applies them after the folio amount has been computed, or applies them at
all on a room-charge invoice, the folio and the invoice disagree. Needs an explicit test on the
room-charge path with a transaction-scope promotion active.

### F. POS Invoice is the gap

Restating finding 9 in NexDine terms: `hospitality_core` uses **POS Invoice** for stock ERPNext POS
(its `hooks.py` comment says POS Next uses Sales Invoice, POS Invoice is retained for the other
path). The authorization gate runs on Sales Invoice `before_submit` only. Any NexDine outlet on the
POS Invoice path gets promotions and min/max discounts, but **no** approval gate on returns,
discount overrides or price edits.

### Verdict for NexDine

Usable, but not as-is. In order:

1. Fix findings 1, 2, 3, 4 — the money path. Non-negotiable before any till runs it.
2. Fix 9 and F — extend the gate to POS Invoice, or state in writing that NexDine is Sales-Invoice-only.
3. Resolve A and B — pick one owner for the duplicated hooks and namespace the desk JS.
4. Decide D — the accounting treatment for free composite items.
5. Then 5, 8 and 10.

---

*Review performed against `main` @ `70be440`, 2026-09-08.*
