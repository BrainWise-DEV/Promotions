# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Effective Friends & Family configuration: the most specific level wins.

Levels, most specific first: scheme, program, global (Friends and Family
Settings), default. Only the global level exists in Phase 1; ``program`` and
``scheme`` are accepted so callers use the final signature now.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe.utils import cint

from posnext_promotions.promotions import ff_constants as const
from posnext_promotions.promotions.friends_and_family import SETTINGS_DOCTYPE

@dataclass(frozen=True)
class Value:
	value: object
	source: str  # "scheme" | "program" | "global" | "default"


@dataclass(frozen=True)
class EffectiveConfig:
	allow_free_gift_with_ff: Value
	on_cap_breach_at_submit: Value
	unclassified_promotion_policy: Value


def _select(settings, fieldname: str, allowed: tuple, default: str) -> Value:
	raw = settings.get(fieldname)
	# A blank Select reads as None from the DB: treat it as unset.
	if raw and raw != const.USE_GLOBAL and raw in allowed:
		return Value(raw, "global")
	return Value(default, "default")


def _build() -> EffectiveConfig:
	settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)
	return EffectiveConfig(
		allow_free_gift_with_ff=Value(bool(cint(settings.get("allow_free_gift_with_ff"))), "global"),
		on_cap_breach_at_submit=_select(
			settings, "on_cap_breach_at_submit", const.CAP_BREACH_POLICIES, const.CAP_BREACH_POLICIES[0]
		),
		unclassified_promotion_policy=_select(
			settings, "unclassified_promotion_policy", const.UNCLASSIFIED_POLICIES, const.UNCLASSIFIED_POLICIES[0]
		),
	)


def resolve(program: str | None = None, scheme: str | None = None) -> EffectiveConfig:
	"""Effective config. ``program`` and ``scheme`` are ignored until Phase 3.

	Built from the Settings document on every call, never from a cache of its own:
	``frappe.get_cached_doc`` is cleared on save and on ``db.set_value``, and again after
	the commit, so every judge reads the checkbox the Settings hold (J1).
	"""
	return _build()

