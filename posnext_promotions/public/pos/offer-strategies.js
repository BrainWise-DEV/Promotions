/**
 * Offline offer strategies contributed by POSNext Promotions to the POS cart.
 *
 * Loaded at runtime by pos_next's offer strategy registry (see
 * POS/src/utils/offerStrategies.js) when `frappe.boot.posnext_promotions` is
 * set. Plain ESM on purpose: no Vue, no Pinia, no bundler. The host imports it
 * by URL, so nothing here may import from pos_next either — everything it needs
 * arrives through the registration argument and the `ctx` passed to `apply`.
 *
 * These functions are the OFFLINE mirror of the server pipeline:
 * - Accumulative → apply_accumulative_discount_rules (promotions/engine.py)
 * - Gift Pool / GWP → apply_offers / gift_pool.py
 * When the server changes, change this too.
 */

const ACCUMULATIVE = "Accumulative";
const DISCOUNT_SOURCE = "accumulative_promotion";
const PROMOTION_TYPE_GIFT_POOL = "Gift Pool";
const PROMOTION_TYPE_GWP = "GWP";

/**
 * Total percentage an Accumulative offer grants, given what's in the cart.
 *
 * The server pre-expands each scope row's values (item groups to their
 * descendants, template items to their variants), so matching here is a plain
 * lookup — every scope row represented in the cart contributes its percentage
 * and the sum lands on every eligible line.
 *
 * @param {Object} offer - The offer, carrying accumulative_scopes
 * @param {Array} eligibleItems - Lines that would receive the discount
 * @returns {number} Percentage off list price, 0 when the offer doesn't qualify
 */
export function computeAccumulativeDiscount(offer, eligibleItems) {
	const scopes = offer.accumulative_scopes || [];
	const field = offer.accumulative_scope_field;
	if (scopes.length === 0 || !field) return 0;

	let total = 0;
	let scopesPresent = 0;

	for (const scope of scopes) {
		const values = scope.values || [];
		if (!eligibleItems.some((item) => values.includes(item[field]))) continue;
		scopesPresent += 1;
		total += Number.parseFloat(scope.discount_percentage) || 0;
	}

	const minScopes = Math.max(Number.parseInt(offer.min_scopes_required, 10) || 1, 1);
	if (scopesPresent < minScopes) return 0;

	const cap = Number.parseFloat(offer.max_accumulated_discount_percentage) || 0;
	if (cap > 0) total = Math.min(total, cap);

	return Math.min(Math.max(total, 0), 100);
}

/**
 * Apply an Accumulative offer's summed percentage to every eligible line.
 * @returns {boolean} True if any line was discounted
 */
function applyAccumulativeDiscount(offer, eligibleItems, ctx) {
	const total = computeAccumulativeDiscount(offer, eligibleItems);
	if (total <= 0) return false;

	let applied = false;

	for (const item of eligibleItems) {
		// Skip lines another rule already claimed, matching the server pass.
		if (item.pricing_rules && item.pricing_rules.length > 0) continue;
		if (item.is_already_discounted) continue;

		// Honour the item's own ceiling when the cached record carries one;
		// the server clamps against Item.max_discount unconditionally.
		const itemMax = Number.parseFloat(item.max_discount) || 0;
		item.discount_percentage = itemMax > 0 ? Math.min(total, itemMax) : total;
		item.pricing_rules = [offer.name];
		item.is_accumulative_discount = 1;
		item.is_already_discounted = 1;
		item.discount_source = DISCOUNT_SOURCE;
		ctx.recalculateItem(item);
		applied = true;
	}

	return applied;
}

export const accumulativeStrategy = {
	// Accumulative claims its lines exclusively, so it has to run before the
	// plain price-discount path — otherwise a first-come offer takes the line
	// and the accumulated total never lands.
	order: 100,

	apply: applyAccumulativeDiscount,

	claimsLine(item) {
		return Boolean(item?.is_accumulative_discount);
	},

	// The one case where Auto Discount stacks instead of being suppressed: the
	// two percentages sum. Mirrors ITEM_STATE_ACCUMULATIVE in the Promotion
	// Interaction Matrix (api/promotion_exclusions.py).
	allowsAutoDiscountStacking(item) {
		return Boolean(item?.is_accumulative_discount);
	},
};

function asPricingRules(pr) {
	if (Array.isArray(pr)) return [...pr];
	if (!pr) return [];
	return String(pr)
		.split(",")
		.map((s) => s.trim())
		.filter(Boolean);
}

function appendPricingRule(item, offerName) {
	const prArr = asPricingRules(item.pricing_rules);
	if (!prArr.includes(offerName)) prArr.push(offerName);
	item.pricing_rules = prArr;
}

/**
 * Apply Gift Pool product discount offline: paid items in a group grant
 * free_qty units spread across that group's pool SKUs.
 *
 * Parity notes (gift_pool.allocate_gift_pool_free_items):
 * - Distribution is round-robin over pool codes in row order.
 * - free_qty is once per cart (paidQty gates eligibility only).
 * - Existing free-row qty is set (absolute), not accumulated — same as
 *   re-apply semantics on the host free-item path.
 * - Offline has no stock map, so OOS skip (online) is not mirrored here.
 */
function applyOfflineGiftPool(offer, eligibleItems, ctx) {
	const invoiceItems = ctx.invoiceItems;
	const poolRows = Array.isArray(offer.gift_pool_items) ? offer.gift_pool_items : [];
	if (!poolRows.length) return false;

	const pools = {};
	for (const row of poolRows) {
		const group = row.item_group;
		const code = row.item_code;
		if (!group || !code) continue;
		if (!pools[group]) pools[group] = [];
		if (!pools[group].includes(code)) pools[group].push(code);
	}

	let applied = false;
	const referenceItem = eligibleItems[0];
	const uomKey = referenceItem?.uom || referenceItem?.stock_uom || "Nos";

	for (const [itemGroup, poolCodes] of Object.entries(pools)) {
		const poolSet = new Set(poolCodes);
		const sample = poolRows.find((row) => row.item_group === itemGroup);
		const groupSet = new Set(
			sample?.matching_item_groups?.length ? sample.matching_item_groups : [itemGroup]
		);
		const paidItems = eligibleItems.filter(
			(item) => groupSet.has(item.item_group) && !poolSet.has(item.item_code)
		);
		const paidQty = paidItems.reduce(
			(sum, item) => sum + (Math.floor(item.quantity || item.qty || 0) || 0),
			0
		);
		if (paidQty <= 0) continue;

		const giftQty = Math.max(1, Number(sample?.free_qty) || 1);
		const counts = {};
		for (let i = 0; i < giftQty; i++) {
			const giftCode = poolCodes[i % poolCodes.length];
			counts[giftCode] = (counts[giftCode] || 0) + 1;
		}

		for (const item of paidItems) {
			appendPricingRule(item, offer.name);
		}

		for (const [giftCode, freeItemsToGive] of Object.entries(counts)) {
			const poolRow = poolRows.find((row) => row.item_code === giftCode);
			const existingFreeRow = invoiceItems.find(
				(r) =>
					r.is_free_item &&
					r.item_code === giftCode &&
					(r.uom || r.stock_uom) === uomKey
			);
			if (existingFreeRow) {
				existingFreeRow.quantity = freeItemsToGive;
				existingFreeRow.free_qty = freeItemsToGive;
				appendPricingRule(existingFreeRow, offer.name);
			} else {
				invoiceItems.push({
					item_code: giftCode,
					item_name: poolRow?.item_name || giftCode,
					rate: 0,
					price_list_rate: 0,
					quantity: freeItemsToGive,
					discount_amount: 0,
					discount_percentage: 0,
					tax_amount: 0,
					amount: 0,
					stock_qty: 0,
					uom: uomKey,
					stock_uom: uomKey,
					conversion_factor: 1,
					is_free_item: 1,
					free_qty: freeItemsToGive,
					pricing_rules: [offer.name],
					warehouse: referenceItem?.warehouse,
				});
			}
			applied = true;
		}
	}

	return applied;
}

/**
 * Offline GWP for a single SKU: cashier must scan paid + free units.
 * Buy 2 get 1 free needs qty 3, then splits 1 unit onto a free row.
 */
function applyOfflineGwpSameItem(offer, eligibleItems, ctx) {
	const { recalculateItem, invoiceItems } = ctx;
	const freeQty = Math.floor(Number.parseFloat(offer.free_qty) || 0);
	const minQty = Number.parseFloat(offer.min_qty) || 0;
	const maxQty = Number.parseFloat(offer.max_qty) || 0;
	if (freeQty <= 0) return false;

	const paidItems = eligibleItems.filter((item) => !item.is_free_item);
	const codes = [...new Set(paidItems.map((item) => item.item_code).filter(Boolean))];
	if (codes.length !== 1) {
		return false;
	}

	const itemCode = codes[0];
	const matching = invoiceItems.filter(
		(item) => item.item_code === itemCode && !item.is_free_item
	);
	const existingFree = invoiceItems.filter(
		(item) => item.item_code === itemCode && item.is_free_item
	);
	const totalQty = [...matching, ...existingFree].reduce(
		(sum, item) => sum + (Math.floor(item.quantity || item.qty || 0) || 0),
		0
	);
	const paidAfterFree = totalQty - freeQty;
	if (totalQty < minQty + freeQty || paidAfterFree <= 0) return false;
	if (minQty > 0 && paidAfterFree < minQty) return false;
	if (maxQty > 0 && paidAfterFree > maxQty) return false;

	const referenceItem = matching[0];
	if (!referenceItem) return false;
	const uomKey = referenceItem.uom || referenceItem.stock_uom;
	if ((Number.parseFloat(referenceItem.quantity) || 0) > freeQty) {
		referenceItem.quantity = (Number.parseFloat(referenceItem.quantity) || 0) - freeQty;
		recalculateItem(referenceItem);
	}

	for (const item of matching) {
		appendPricingRule(item, offer.name);
	}

	const existingFreeRow = invoiceItems.find(
		(r) =>
			r.is_free_item &&
			r.item_code === itemCode &&
			(r.uom || r.stock_uom) === uomKey
	);
	if (existingFreeRow) {
		existingFreeRow.quantity = freeQty;
		existingFreeRow.free_qty = freeQty;
		existingFreeRow.gwp_same_item_row = 1;
		existingFreeRow.discount_source = "gwp";
	} else {
		invoiceItems.push({
			item_code: itemCode,
			item_name: referenceItem.item_name || itemCode,
			rate: 0,
			price_list_rate: 0,
			quantity: freeQty,
			discount_amount: 0,
			discount_percentage: 0,
			tax_amount: 0,
			amount: 0,
			stock_qty: 0,
			uom: uomKey,
			stock_uom: referenceItem.stock_uom || uomKey,
			conversion_factor: referenceItem.conversion_factor || 1,
			is_free_item: 1,
			free_qty: freeQty,
			discount_source: "gwp",
			gwp_same_item_row: 1,
			pricing_rules: [offer.name],
			warehouse: referenceItem.warehouse,
		});
	}
	return true;
}

/** Entry point called by pos_next's registry after it imports this module. */
export function register({ registerOfferStrategy, registerProductOfferStrategy }) {
	registerOfferStrategy(ACCUMULATIVE, accumulativeStrategy);

	if (typeof registerProductOfferStrategy === "function") {
		registerProductOfferStrategy(PROMOTION_TYPE_GIFT_POOL, {
			order: 50,
			apply: applyOfflineGiftPool,
		});
		registerProductOfferStrategy(PROMOTION_TYPE_GWP, {
			order: 50,
			apply: applyOfflineGwpSameItem,
		});
	}
}
