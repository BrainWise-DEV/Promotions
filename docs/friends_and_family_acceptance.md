# Friends & Family — سيناريوهات اختبار القبول (PN-133)

الملف ده بيغطي سيناريوهات القبول لبرنامج **Friends & Family (F&F)** (PN-124 → PN-132) على **POS** والـ Promotions شغالة.
كل حالة مكتوب فيها المدخلات بالظبط والأرقام المتوقعة. أسماء الحقول والزراير بالإنجليزي زي ما هي ظاهرة في الـ Desk / POS.

> نفس السيناريوهات دي متغطية أوتوماتيك في
> `posnext_promotions/posnext_promotions/doctype/friends_and_family_settings/test_friends_and_family_acceptance.py`
> (رقم الحالة هنا = رقم الـ test هناك).

---

## 0. التجهيز

### 0.1 بيانات الديمو (على dev site بس)

الـ seed helper بيعمل كل البيانات المطلوبة، وتقدر تشغّله أكتر من مرة من غير ما يكرر حاجة (idempotent).
**مش هيشتغل** غير لو `developer_mode` = 1، ولازم يكون اللي بيشغّله System Manager.

```bash
cd ~/frappe-bench
bench --site <dev-site> execute posnext_promotions.promotions.friends_and_family_demo.setup_demo_data
# لو الـ POS Profile بيستخدم Price List غير الـ default في Selling Settings:
bench --site <dev-site> execute posnext_promotions.promotions.friends_and_family_demo.setup_demo_data \
  --kwargs "{'company': 'Brainwise', 'price_list': 'Standard Selling'}"
```

> ⚠️ ممنوع تشغيله على أي site إنتاج (Miraaya وغيرها). وكمان تشغيله تاني بيرجّع **Friends and Family Settings** للقيم اللي تحت.

اللي بيتعمل (الـ prefix الافتراضي `FF Demo`):

| النوع | الاسم | ملاحظات |
|---|---|---|
| Customer Group | `FF Demo Staff Group` | جروب مؤهل (Eligible) |
| Customer Group | `FF Demo Public Group` | جروب عادي |
| Customer | `FF Demo Staff` | مؤهل بالجروب |
| Customer | `FF Demo Flagged` | مؤهل بالـ flag **Friends & Family Member** (وجروبه Public) |
| Customer | `FF Demo Public` | **مش** مؤهل |
| Brand | `FF Demo Brand A` / `FF Demo Brand X` | ‏X مستثنى (Excluded) |
| Item Group | `FF Demo Products` | |

| Item | Price List Rate | Brand | Promotion (Pricing Rule) | الغرض |
|---|---|---|---|---|
| `FF Demo Laptop` | 1000 | A | — | covered + caps |
| `FF Demo Phone` | 500 | A | — | covered |
| `FF Demo Cable` | 100 | A | 10% | البرومو أقل → F&F يكسب |
| `FF Demo Charger` | 200 | A | 40% | البرومو أعلى → البرومو يكسب |
| `FF Demo Mouse` | 100 | A | 30% | تعادل → F&F يكسب |
| `FF Demo Gift Card` | 300 | A | — | **Excluded Item** |
| `FF Demo Headset` | 400 | X | — | **Excluded Brand** |

POS Coupons (لو POS Next متسطب): `FFDEMO15` (15%) و`FFDEMO40` (40%)، الاتنين Percentage / Apply On = Net Total.

### 0.2 إعدادات الـ Desk (لو هتجهّز يدوي أو عايز تراجع)

افتح **Friends and Family Settings** (Desk → Search → "Friends and Family Settings"):

| Section | Field | القيمة |
|---|---|---|
| General | **Enable Program** | ✔ |
| General | **Discount Percentage** | 30 |
| Program Dates | **Start Date** / **End Date** | فاضيين |
| Discount Caps | **Max Discount per Order** | 0 (من غير حد) |
| Discount Caps | **Max Discount per Customer per Month** | 0 (من غير حد) |
| Customer Eligibility | **Eligible When** | Customer Group or Flag |
| Customer Eligibility | **Eligible Customer Groups** | `FF Demo Staff Group` |
| Discount Scope | **Discount Scope** | Entire Cart |
| Exclusions | **Excluded Items** | `FF Demo Gift Card` |
| Exclusions | **Excluded Brands** | `FF Demo Brand X` |

اضغط **Save**. من زرار **View** في نفس الصفحة هتلاقي: **Audit Log**، **Settings Changes**، **Version History**، **Performance Report**.

- الـ flag على العميل: افتح **Customer** → checkbox **Friends & Family Member** (بيعدّله Sales Manager / System Manager بس، الكاشير لأ).
- البرومو: **Pricing Rule** → Apply On = Item Code، Selling ✔، Price or Product Discount = Price، Rate or Discount = Discount Percentage.
- الـ POS: استخدم POS Profile عادي (على brainwise.dev: `profile 1`) والـ Price List بتاعه لازم فيها الأسعار اللي فوق.

### 0.3 قواعد عامة لكل الحالات

- الحسبة: F&F = **Price List Rate × 30%** على كل سطر مغطّى. الـ Rate الجديد = Price List Rate × 0.7.
- **مفيش stacking أبدًا**: كل سطر عليه خصم واحد بس، الأحسن بالفلوس. لو حصل تعادل، F&F يكسب.
- بعد **Submit** لأي فاتورة، اتأكد من **Friends and Family Log** (Desk → "Friends and Family Log"، فلتر Reference Name = رقم الفاتورة).
- ملاحظة بيئة (brainwise.dev): app الـ Wallet بيعمل **Wallet Transaction** "Loyalty Credit" submitted مع كل Sales Invoice. علشان تعمل Cancel للفاتورة لازم تعمل Cancel للـ Wallet Transaction ده الأول.

---

## 1. حالات الـ Backend / Desk (Sales Invoice أو POS Invoice، Is POS ✔)

اعمل الفاتورة من **Sales Invoice → New**، علّم **Include Payment (POS)** واختار الـ **POS Profile**، أو من شاشة POS. المبالغ تحت قبل الضرايب.

### الحالة 1 — عميل مؤهل بياخد F&F تلقائي

| Customer | Items |
|---|---|
| `FF Demo Staff` | Laptop × 1، Phone × 2 |

المتوقع:
- Laptop: **Discount (%)** = 30، **Rate** = 700.
- Phone: Rate = 350 (لكل واحدة).
- **Net Total** = 1400 (بدل 2000).
- الرسالة = نص **Friends and Family Message** للـ event `ff_activated` لو متعرّف، وإلا "Customer is eligible for Friends & Family".
- بعد Submit: في الـ Log سطرين **Discount Applied** (Laptop 300، Phone 300)، Discount Source = `friends_and_family`. المجموع 600.

**1b — مؤهل بالـ flag:** نفس الحالة مع `FF Demo Flagged` و Laptop × 1 → Rate = 700.
بعد كده غيّر **Eligible When** لـ `Customer Group` واعمل Save، وكرر → Rate = 1000 (الـ flag مبقاش بيتحسب). رجّعها `Customer Group or Flag`.

### الحالة 2 — عميل مش مؤهل

`FF Demo Public` + Laptop × 1 → Rate = **1000**، مفيش خصم، ومفيش ولا سطر في الـ Log.

**2b — تغيير العميل في السلة (PN-130):** في الـ POS ضيف Laptop للعميل `FF Demo Staff` (هيبقى 700)، وبعدين غيّر العميل لـ `FF Demo Public` → السطر يرجع **1000** و0%، من غير أي أثر لـ F&F. نفس الكلام لو البرنامج اتقفل (**Enable Program** ✘) والسلة مفتوحة.

### الحالة 3 — الكاشير مايقدرش يفرض F&F أو النسبة

1. ادخل بيوزر **Sales User** بس (كاشير)، افتح `FF Demo Public` وعلّم **Friends & Family Member** وSave → لازم تطلع رسالة **Not permitted**.
2. لعميل مؤهل، غيّر يدوي Discount (%) على Laptop لـ 50 وSave → السيستم يرجّعها **30** (Rate 700).
3. نفس الحاجة لعميل مش مؤهل (`FF Demo Public`) → يرجع **1000**.

### الحالة 4 — صنف مستثنى (Excluded Item)

`FF Demo Staff` + Gift Card × 1 + Laptop × 1:
- Gift Card: Rate = **300** (من غير خصم).
- Laptop: Rate = 700.
- Log: **Excluded** للـ Gift Card، **Discount Applied** للـ Laptop.

### الحالة 5 — سلة مختلطة فيها Brand مستثنى

`FF Demo Staff` + Laptop × 1 + Headset × 1:
- Laptop 700، Headset **400**.
- Net Total = **1100**.
- Log: Headset = **Excluded**.

**5b — كل الأصناف مستثناة:** Gift Card + Headset → مفيش خصم، Net Total = **700**. الـ reason = `no_covered_items`، والرسالة فيها "every item in the cart is excluded". Log: **Excluded** بس.

### الحالة 6 — البرومو أقل من F&F → F&F يكسب + رسالة

`FF Demo Staff` + Cable × 1 (برومو 10%):
- Discount (%) = **30**، Rate = **70**، و**Pricing Rules** على السطر فاضية (البرومو اتشال، مفيش stacking).
- رسالة المقارنة (EN + AR) فيها "Friends & Family" / "الأصدقاء والعائلة".
- Log بعد Submit: Discount Applied، Comparison Outcome = `fnf_better`، Competing Source = `promotion`، Competing Amount = **10**.
- اعمل **Save** الأول وبعدين **Submit** (مش Submit على طول) → المقارنة لازم تفضل موجودة في الـ Log (ده كان bug واتصلّح في PN-133).

### الحالة 7 — البرومو أعلى → البرومو يكسب + رسالة

`FF Demo Staff` + Charger × 1 (برومو 40%):
- Discount (%) = **40**، Rate = **120**، والـ Pricing Rule لسه على السطر.
- الرسالة بتقول إن البرومو أحسن.
- Log: **Comparison Lost**، Discount Source = `promotion`.

### الحالة 8 — تعادل → F&F يكسب

`FF Demo Staff` + Mouse × 1 (برومو 30%) → Rate = **70** بـ F&F. Log: Comparison Outcome = `tie_fnf_wins`.

### الحالة 9 — خصم على الفاتورة كلها (Additional Discount) — مفيش stacking

`FF Demo Staff` + Laptop × 1، **Apply Additional Discount On** = Net Total:
- **Additional Discount Percentage** = 10 → السيستم يشيله (يبقى 0)، F&F يفضل، Net Total = **700**.
- Additional Discount Percentage = 50 → F&F يتشال (Rate 1000)، Net Total = **500**. Log: **Order Comparison** والـ winner = `order_manual`.

### الحالة 10 — Coupon قصاد F&F

السلة: `FF Demo Staff` + Laptop × 1 (بقت 700 بـ F&F).
- كوبون `FFDEMO15` → **مرفوض**، reason = `better_discount_exists`، والرسالة: "Friends & Family discount (30%) is better than coupon FFDEMO15 (15%)" + نسخة عربي.
- كوبون `FFDEMO40` → **مقبول**: Laptop Discount (%) = 40، Rate = **600**. Log: Discount Source = `coupon`.
- `FFDEMO15` لعميل مش مؤهل (`FF Demo Public`) → مقبول عادي (15%).

**10b — سلة مختلطة في الـ POS (كوبون على مستوى الفاتورة):** `FF Demo Flagged` + Laptop + Phone + Cable + Charger + Mouse + Headset + Gift Card (Subtotal **2600**). من غير كوبون: F&F على 4 أصناف = **510**، والـ Charger بالبرومو (**80**)، الإجمالي **2010**.
- `FFDEMO40` → الكوبون (40% على الأصناف اللي مالهاش خصم تاني = 2400 → **960**) أحسن من F&F (510)، فـ F&F يتشال من كل السطور، والـ Charger يفضل بالبرومو. Discount: Promotion 80 + Coupon **960**، الإجمالي **1560**، وبعد Save الفاتورة لازم تفضل Discount Amount = **960** (مش 1008 — الكوبون مايتحسبش على سطر عليه برومو).
- `FFDEMO15` → 360 أقل من 510 → مرفوض بـ `better_discount_exists`، والسلة تفضل **2010**.

### الحالة 11 — Max Discount per Order

في الـ Settings: **Max Discount per Order** = 100، Save.
- `FF Demo Staff` + Laptop × 1 → Rate = **900** (الخصم 100 بدل 300). الـ caps: cap_hit = `order`، fnf_uncapped_total = 300، fnf_applied_total = 100. الرسالة: "…capped at the per-order maximum of 100.00 (full discount would be 300.00)". Log: **Discount Applied** + **Cap Hit**.
- Laptop + Phone (300 + 150) → إجمالي الخصم **100**، متوزّع بالنسبة 2:1 (تقريبًا 66.67 على اللابتوب و33.33 على الموبايل).

رجّع الحد لـ 0 بعدها.

### الحالة 12 — Max Discount per Customer per Month + المرتجع بيرجّع الرصيد

> الاستهلاك الشهري بيتحسب من كل الفواتير الـ submitted للعميل في الشهر ده. علشان الأرقام تطلع زي ما هي تحت، اعمل عميل جديد (مثلًا `FF Demo Cap Test`) وعلّم عليه **Friends & Family Member**، أو راجع **monthly_used** الأول.

**Max Discount per Customer per Month** = 500:
1. فاتورة 1: Laptop → Rate **700** (استهلك 300، فاضل 200).
2. فاتورة 2: Laptop → Rate **800** (اتخصم 200 بس). cap_hit = `monthly`، والرسالة فيها "reduced to 200.00".
3. فاتورة 3: Phone → Rate **500** (من غير خصم). الرسالة: "Monthly Friends & Family limit of 500.00 reached…". Log: **Cap Hit**.
4. اعمل **Return / Credit Note** لفاتورة 1 → سطر المرتجع عليه F&F Amount = **−300**، والاستهلاك يبقى **200**.
5. فاتورة 4: Laptop → Rate **700** تاني (رجع 300 رصيد).

رجّع الحد لـ 0 بعدها.

### الحالة 13 — نهاية البرنامج / إيقافه → F&F يقف فورًا

1. اعمل Draft لـ `FF Demo Staff` + Laptop (هيبقى 700) وماتعملوش Submit.
2. في الـ Settings: **Start Date** = من 30 يوم، **End Date** = امبارح، Save.
3. فاتورة جديدة لنفس العميل → **1000**. الـ POS: `friends_and_family.eligible` = false.
4. افتح الـ Draft بتاع خطوة 1 واعمل Save → يرجع **1000**. وبعد Submit مفيش Discount Applied في الـ Log.
5. **Start Date** = بكرة و End Date فاضي → 1000. **Enable Program** ✘ → 1000.

### الحالة 14 — Cancel / Return في الـ Log والتقرير

1. فاتورة A: `FF Demo Staff` + Laptop × 1 (F&F 300).
2. فاتورة B: Phone × 2 (F&F 300).
3. Return من B بموبايل واحد (−150).
4. Cancel لـ A (اعمل Cancel للـ Wallet Transaction الأول على brainwise.dev).

المتوقع في **Friends and Family Log**:
- A: **Discount Applied** 300 + **Discount Reversed** −300، والاتنين **Is Cancelled** ✔.
- المرتجع: **Discount Returned** −150.

في **Friends and Family Performance** (View = Invoices، اليوم، Customer = `FF Demo Staff`):
- A مش ظاهرة. B والمرتجع ظاهرين.
- **F&F Discount (net)** = **150**، **F&F Orders** = 1، **Returns** = 1.

### الحالة 15 — تغيير الإعدادات والأهلية بيتسجّل

- غيّر **Discount Percentage** لـ 25 وSave → في الـ Log سطر **Settings Changed** وفي Details `discount_percentage`.
- علّم **Friends & Family Member** على `FF Demo Public` (بيوزر Sales Manager) → Log: **Eligibility Applied**، reason = `customer_flag`. فاتورة Laptop ليه → Rate = **750**.
- رجّع النسبة 30 وشيل الـ flag.

---

## 2. حالات واجهة الـ POS (PN-130)

الـ POS بياخد كل حاجة من رد `posnext_promotions.api.apply_offers.apply_offers`. الحالات دي مكتوبة على **عقد الـ payload** (شكله ثابت ومش هيتغير)، فتقدر تشتغل عليها أول ما PN-130 ينزل.

### 2.1 عقد الـ payload

```text
response.items[]                      ← السطور بعد التسعير (rate / discount_percentage / discount_amount)
response.friends_and_family = {
  eligible, discount_percentage, applied_count,
  message, reason,
  items[] = { item_code, idx, applies, discount_percentage, discount_amount, rate, reason,
              competing_source, competing_amount, fnf_uncapped_amount, capped,
              best_discount{ winner, outcome, candidates[], suppressed[], message_en, message_ar } },
  caps = { active, max_discount_per_order, max_discount_per_customer_month, monthly_used,
           monthly_remaining, allowance, fnf_uncapped_total, fnf_applied_total, capped,
           cap_hit, monthly_remaining_after, message, message_en, message_ar },
  cap_hit
}
response.best_discount = { winner, lines[], order, fnf_total, suppressed[], stacking:false }
```

قيم مهمة:
- `reason` (على مستوى السطر): `applied`، `excluded`، `out_of_scope`، `other_discount_better`، `cap_reached`، `order_discount_better`، `customer_ineligible`، `program_inactive`، `stripped`.
- `best_discount.outcome`: `fnf_only`، `fnf_better`، `tie_fnf_wins`، `other_better`، `no_fnf`.
- `winner`: `friends_and_family`، `promotion`، `coupon`، `manual`، `order_promotion`، `order_coupon`، `order_manual`.
- لعميل مش مؤهل أو برنامج مقفول: `friends_and_family.eligible = false` (البلوك موجود بنفس الشكل)، وأي سطر كان عليه F&F بيرجع بالـ Price List Rate.

### 2.2 الحالات

| # | الخطوات | المتوقع في الـ UI | الحقل |
|---|---|---|---|
| P1 | اختار `FF Demo Staff` وضيف Laptop | Badge "Friends & Family 30%" على العميل/السلة؛ السطر 1000 مشطوبة ← 700؛ رسالة `ff_activated` ظاهرة (لو متعرّفة) | `eligible`، `discount_percentage`، `items[].applies`، `message` |
| P2 | اختار `FF Demo Public` | مفيش badge؛ السعر 1000 | `eligible=false` |
| P3 | P1 وبعدين غيّر العميل لـ Public | السطر يرجع 1000 فورًا من غير F&F | `response.items[]` |
| P4 | Staff + Gift Card + Headset + Laptop | Gift Card وHeadset عليهم "Excluded" ومن غير خصم؛ Laptop 700 | `items[].reason = excluded` |
| P5 | Staff + Gift Card + Headset بس | رسالة إن كل الأصناف مستثناة | `reason = no_covered_items` |
| P6 | Staff + Cable | Breakdown: F&F 30 بدل Promotion 10؛ رسالة `message_en` / `message_ar` | `best_discount.outcome = fnf_better` |
| P7 | Staff + Charger | السطر 120 (Promotion 40%)؛ رسالة إن البرومو أحسن | `winner = promotion` |
| P8 | Staff + Mouse | F&F (تعادل) | `outcome = tie_fnf_wins` |
| P9 | Staff + Laptop + كوبون FFDEMO15 | الكوبون يترفض برسالة "Friends & Family discount (30%) is better than coupon FFDEMO15 (15%)" | `validate_coupon` → `reason = better_discount_exists` |
| P10 | Staff + Laptop + كوبون FFDEMO40 | الكوبون يتقبل؛ 600 | `line_updates[]` |
| P11 | Max Discount per Order = 100، Staff + Laptop | 900 + رسالة الحد (EN/AR) | `caps.cap_hit = order`، `caps.message_*` |
| P12 | Max Discount per Customer per Month = 500 | الرصيد المتبقي (`monthly_remaining`) ورسالة الحد لما يخلص | `caps.cap_hit = monthly` |
| P13 | End Date = امبارح أو Enable Program ✘ | الـ badge يختفي والأسعار ترجع | `eligible=false` |
| P14 | Additional Discount 50% على Laptop | F&F يتشال؛ الـ breakdown يوضح إن خصم الفاتورة أحسن | `items[].reason = order_discount_better` |

---

## 3. التشغيل الأوتوماتيك

```bash
cd ~/frappe-bench
# اختبارات القبول (end-to-end)
bench --site brainwise.dev run-tests --skip-before-tests \
  --module posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_friends_and_family_acceptance
# POS Profile تاني (اختياري)
FF_ACCEPTANCE_POS_PROFILE="profile 1" bench --site brainwise.dev run-tests --skip-before-tests \
  --module posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_friends_and_family_acceptance
```

الاختبارات بتعمل بيانات بـ prefix `_Test FF Acc` وبتعمل rollback بعد كل حالة، فمش بتسيب فواتير أو Logs على الـ site.

## Settings UX, customer messages & approved domains (2026-10-13)

**Bulk pickers (Friends and Family Settings form).** Every include / exclude table
(Applicable / Excluded Brands, Item Groups, Items) has two grid buttons:

- **Get All …** — Brands: all; Item Groups: leaf groups by default (prompt lets you take all);
  Items: enabled sales items without variants (templates skipped). Duplicates are skipped and
  a confirm dialog appears when more than 50 rows would be added.
- **Select Multiple** — `MultiSelectDialog` with filters (Item: item group / brand; Item Group:
  parent / is group). Selected values are appended without duplicates.

**Friends and Family Message Event** (named by `event_code`, snake_case, set once): `event_name`
(label), `event_code`, `description` (when it fires), `placeholders`, `is_standard` (read-only),
`enabled`. The 8 standard events the engine fires are created/updated on migrate (labels and
descriptions only — no message text): `ff_activated`, `coupon_lower_ff_applied`,
`coupon_higher_applied`, `tie`, `item_excluded`, `monthly_cap_reached`, `email_change_old_email`,
`email_change_new_email`. Standard events cannot be deleted (disable them instead).

**Friends and Family Message**: `event` (Link → Message Event, one message per event), fetched
read-only event name / description / placeholders, `enabled`, `message_ar` (required),
`message_en`. **No messages are seeded.** A message is used only if both it and its event are enabled.

Helper: `promotions.friends_and_family_messages.get_customer_message(event_code, context=None)` →
`{event, ar, en, message}` or `None`. `{percent}` = Settings %, `{brand}` = default Company name;
any other `{key}` comes from `context`; unknown placeholders stay as typed.

Adding a custom event: create a *Friends and Family Message Event* (e.g. `birthday_offer`), create a
*Friends and Family Message* for it, then call from code or a hook:
`get_customer_message("birthday_offer", {"customer_name": doc.customer_name})`.

Example (optional) Arabic text for `ff_activated`:
"خصمك الحصري بنسبة {percent}% لبرنامج الأصدقاء والعائلة أصبح مفعّلًا الآن. استمتع بتسوقك في {brand}!"

Wired as an **additional** `customer_message` key (null when no record) (existing cashier `message` / `message_en` /
`message_ar` are unchanged):

| Event | Where |
|---|---|
| ff_activated | eligibility payload when the customer is eligible |
| coupon_lower_ff_applied / coupon_higher_applied / tie | `best_discount` decision + summary when the competitor is a (line or order) coupon |
| item_excluded | line result when F&F lost to an existing promotion / manual (sale) discount |
| monthly_cap_reached | caps payload when the monthly cap is exhausted |
| email_change_* | not wired — no e-mail / OTP flow exists yet |

The POS UI does not render `customer_message` yet.

**Approved Domains** (Settings child table *Friends and Family Approved Domain*: `email_domain`,
`company`, `enabled`). Domains are normalised to lowercase without a leading `@`; duplicates and
invalid domains are rejected on save. **No domains are seeded** (example: `miraaya.com` – Miraaya).
`friends_and_family_eligibility.is_approved_email(email)` (exact domain only) is exposed.
See "Approved Domains enforced on the customer's email" below — superseded 2026-10-20.

QA: open `/app/friends-and-family-settings` → use the grid buttons, save; `/app/friends-and-family-message`
→ create the records you need (none by default); in a console, `is_approved_email("x@miraaya.com")` → True, `"x@gmail.com"` → False.

## Apply with F&F (Promotional Scheme) & Allow F&F (POS Profile) (2026-10-18)

### A. Promotional Scheme → **Apply with F&F** (`apply_with_ff`, Check, default 0)

| Scheme check | Line is covered by F&F | Result |
|---|---|---|
| ☑ checked | yes | Scheme % vs F&F % (compared as money saved, so a fixed-amount slab competes fairly). **Larger wins, F&F wins a tie.** The loser is detached (same as PN-128). |
| ☐ unchecked | yes | **F&F only.** The scheme is skipped for that line even if its % is bigger. `best_discount.outcome = fnf_exclusive`, the scheme is listed in `suppressed`, and its Pricing Rule is returned in `suppressed_pricing_rules`. |
| any | no (excluded / out of scope / customer not eligible / program off / cap exhausted) | The scheme applies as usual. |

Notes: only promotions that come from a Promotional Scheme are affected (the line's `pricing_rules`
are traced to `Pricing Rule.promotional_scheme`); plain Pricing Rules, coupons and manual discounts
still compete as before. If one line carries several schemes, a single unchecked scheme makes the
line "F&F only". New schemes default to **unchecked**, so tick the box on every scheme that should
compete with F&F (existing schemes are **not** back-filled).

QA: scheme 10% / 40% with the box unticked + F&F 30% → the line gets 30% (F&F), not 40%. Tick the
box → 40% scheme wins; scheme 30% → tie → F&F; scheme 10% → F&F.

### B. POS Profile → **Allow F&F** (`allow_friends_family`, Check, default 1)

Unticked = F&F is completely off on that profile:

* no F&F discount on cart lines or on the saved invoice (a stale F&F stamp is stripped and the
  discount it replaced comes back),
* no F&F badge / messages in the POS (`check_friends_and_family_eligibility` →
  `eligible: false`, `reason: profile_not_allowed`; `apply_friends_and_family_discount` and the
  `friends_and_family` block of `apply_offers` → not applicable),
* `get_friends_and_family_program(pos_profile=…)` → `{"active": false}`,
* coupons are no longer compared with F&F on that profile (`validate_coupon(pos_profile=…)`),
* nothing is written to the Friends and Family Log for those invoices.

Default is **1** (ticked): existing profiles — and callers that send no `pos_profile` — keep working.
The server derives the profile from the invoice (`pos_profile` on the POS Invoice / `apply_offers`
payload); the POS front-end also sends it on the F&F / coupon calls.

QA: `/app/pos-profile/<profile>` → untick **Allow F&F** → add an item for an F&F customer in the POS:
no badge, full price; save/submit the invoice: no F&F rows in `/app/friends-and-family-log`.
Tick it again → F&F applies.

Automated: `bench --site <dev-site> run-tests --app posnext_promotions --module posnext_promotions.posnext_promotions.doctype.friends_and_family_settings.test_friends_and_family_scheme_profile`
(known pre-existing failures unrelated to this change: `test_friends_and_family_best_discount.test_invoice_header_discount_weaker_than_fnf_is_removed`, `test_friends_and_family_acceptance.test_10_coupon_vs_fnf`).

## Approved Domains enforced on the customer's email (2026-10-20)

When **at least one enabled** Approved Domain is listed, a customer who matches *Eligible When*
(Group / Flag) must ALSO pass the email rule; an empty list restricts nothing.

- Email checked: `Customer.email_id`, trimmed, case-insensitive. Its domain must be **exactly** an
  approved domain — `ali@miraaya.com` ✔, `ali@mail.miraaya.com` ✘ (no sub-domains), `ali@gmail.com` ✘,
  no email ✘ → reason `email_domain_not_approved`.
- When POSNext customer email verification is on (any enabled *POS Settings* with
  *Enable Customer Email Verification*) the email must also be OTP-verified and unchanged since:
  `custom_email_verified = 1` and `custom_verified_email = email_id` → else `email_not_verified`.
  POSNext already clears the verified flag when `email_id` changes.
- Non-members keep `not_eligible` (no email message for them).
- Payload: `reason` + translated `message`; eligible payloads add `email_domain`. The offline rules
  payload adds `approved_domains` / `requires_verified_email`. The apply/apply_offers F&F block
  carries the same `message`.
- Settings save normalises rows (`" @Miraaya.COM "`, `https://www.miraaya.com/` → `miraaya.com`) and
  rejects invalid hostnames (`miraaya`, `user@miraaya.com`, spaces, `-bad.com`) and duplicates.
- POS: on selecting such a customer a warning toast shows the message, and the cart F&F box shows
  it while there are items. After the cashier verifies the email (OTP) in the customer dialog, F&F is
  re-checked and the cart re-priced.

| # | Setup | Expected |
|---|---|---|
| D1 | No approved domains; `FF Demo Staff` | F&F as before |
| D2 | Domain `miraaya.com`; Staff email `x@gmail.com` | No F&F; "needs an email on an approved company domain" |
| D3 | Staff email `x@miraaya.com`, verification on, not verified | No F&F; "verify the email … (OTP) first" |
| D4 | D3 then verify via OTP in POS | F&F applied without reselecting the customer |
| D5 | Verified, then email changed to `y@miraaya.com` | No F&F until verified again |
