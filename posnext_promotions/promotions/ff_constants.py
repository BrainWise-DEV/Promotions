# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Option lists for Friends & Family, defined once.

DocType Select options must equal these tuples (checked by test_ff_config).
"""

USE_GLOBAL = "Use Global"

PROMOTION_KINDS = ("bxgy", "item_level", "free_item", "gwp", "cart_total_gift", "brand_gift", "price", "unclassified")

CAP_BREACH_POLICIES = ("Allow and log", "Block submit")

COMPAT_POLICIES = ("Compete", "Block F&F on line", "Both apply", "F&F wins, remove gift", "Gift wins, cancel F&F")

# A rule the classifier cannot place: gift policies make no sense for it.
UNCLASSIFIED_POLICIES = ("Compete", "Block F&F on line")

# How F&F and a POS Coupon combine on one cart (K1 round 1); the first is the default.
# The coupon itself is always a line discount, before tax.
COUPON_COMBINATIONS = (
	"Better of the two for the whole cart",
	"Better per line (unused coupon share is not used)",
	"Better per line, coupon fully used",
)
COUPON_WHOLE_CART, COUPON_PER_LINE, COUPON_PER_LINE_FULL = COUPON_COMBINATIONS
