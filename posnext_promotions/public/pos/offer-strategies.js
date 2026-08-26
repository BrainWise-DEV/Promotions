/**
 * Offline offer strategies contributed by POSNext Promotions to the POS cart.
 *
 * Loaded at runtime by pos_next's offer strategy registry (see
 * POS/src/utils/offerStrategies.js) when `frappe.boot.posnext_promotions` is
 * set. Plain ESM on purpose: no Vue, no Pinia, no bundler. The host imports it
 * by URL, so nothing here may import from pos_next either — everything it needs
 * arrives through the registration argument and the `ctx` passed to `apply`.
 *
 * These functions are the OFFLINE mirror of the server pipeline. They must
 * produce the same numbers as apply_accumulative_discount_rules in
 * posnext_promotions/promotions/engine.py; when that changes, change this too.
 */

const ACCUMULATIVE = "Accumulative";
const DISCOUNT_SOURCE = "accumulative_promotion";

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

/** Entry point called by pos_next's registry after it imports this module. */
export function register({ registerOfferStrategy }) {
	registerOfferStrategy(ACCUMULATIVE, accumulativeStrategy);
}
