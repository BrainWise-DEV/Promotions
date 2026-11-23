# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Signed snapshot of the discount Friends & Family replaced on a line.

The snapshot lives in a row field the client can write
(``posnext_fnf_replaced_discount``), so it is only trusted when it carries a
valid server signature bound to the line's item, qty and list price, the
customer, company and POS profile (Ruling 25), the program and an expiry. Anything else (unsigned, expired, tampered, signed for
other line values) is ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

import frappe
from frappe.utils import cint, cstr, flt
from frappe.utils.password import get_encryption_key

from posnext_promotions.promotions import ff_compatibility
from posnext_promotions.promotions import friends_and_family_gifts as gifts

VERSION = 2
DOMAIN = "pos-ff-snapshot-v2"
KEY_DOMAIN = b"pos-ff-snapshot|"


def _canonical(data: dict) -> str:
	return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _num(value) -> str:
	# Saved rows round-trip through the DB (1 → 1.0, 100 → 100.000000000):
	# one text form for the same number.
	return f"{flt(value):.6f}"


_SIG_RE = re.compile(r"[0-9a-f]{64}")


def _binding(doc) -> tuple[str, str, str]:
	"""``(customer, company, pos_profile)`` the snapshot is signed for; "" when absent.

	Company is the doc's, else its POS profile's, so a cart (no company) and the
	invoice built from it bind the same value.
	"""
	doc = doc or {}
	pos_profile = cstr(doc.get("pos_profile"))
	company = cstr(doc.get("company") or ff_compatibility._profile_values(pos_profile).get("company"))
	return cstr(doc.get("customer")), company, pos_profile


def _key() -> bytes | None:
	"""Signing key, or None (logged) when the site has no encryption key."""
	try:
		return hashlib.sha256(KEY_DOMAIN + cstr(get_encryption_key()).encode()).digest()
	except Exception:
		frappe.log_error(title="F&F snapshot: signing key unavailable")
		return None


def _signature(key: bytes, item, program: str, exp: int, data: dict, doc=None) -> str:
	message = "|".join(
		(
			DOMAIN,
			cstr(item.get("item_code")),
			_num(item.get("qty")),
			_num(item.get("price_list_rate")),
			*_binding(doc),
			cstr(program),
			str(exp),
			_canonical(data),
		)
	)
	return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def sign(item, data: dict, program: str = "", ttl_seconds: int = 86400, doc=None) -> str | None:
	"""Signed JSON snapshot of ``data`` for ``item`` sold on ``doc``; None when no key is available."""
	# Normalise first so verify (which sees parsed JSON) signs the same bytes.
	data = json.loads(_canonical(data))
	exp = int(time.time()) + cint(ttl_seconds)
	key = _key()
	if key is None:
		return None
	sig = _signature(key, item, program, exp, data, doc)
	return json.dumps({"v": VERSION, "exp": exp, "program": cstr(program), "data": data, "sig": sig})


def verify(item, raw, program: str = "", doc=None) -> dict | None:
	"""The snapshot ``data`` when ``raw`` is a valid, unexpired signature for ``item`` on ``doc``; else None.

	Version 1 snapshots (no customer / company / profile binding) are invalid.
	"""
	try:
		envelope = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
	except (TypeError, ValueError):
		return None
	if not isinstance(envelope, dict) or envelope.get("v") != VERSION:
		return None
	data, sig, exp = envelope.get("data"), envelope.get("sig"), envelope.get("exp")
	if not isinstance(data, dict) or not isinstance(exp, int):
		return None
	# Exactly 64 lowercase hex: compare_digest raises on non-ASCII str.
	if not isinstance(sig, str) or not _SIG_RE.fullmatch(sig):
		return None
	if cstr(envelope.get("program")) != cstr(program):
		return None
	key = _key()
	if key is None:
		return None
	if not hmac.compare_digest(_signature(key, item, program, exp, data, doc), sig):
		return None
	if exp <= int(time.time()):
		return None
	return data


def rules_apply_to(data: dict, doc) -> bool:
	"""Every Pricing Rule in the snapshot still applies to ``doc`` (Ruling 24).

	Same check as the cart / invoice rule map (:func:`ff_compatibility.rule_applies_to_doc`):
	exists, enabled, valid on the posting date, company, POS profile, customer /
	group / territory, one-time redemption. A held cart must not bring back a
	promotion that has expired or no longer applies to its customer or profile.
	No rule (a manual discount) passes.
	"""
	names = gifts._split_rule_names(data.get("pricing_rules"))
	if not names:
		return True
	fields = ff_compatibility.rule_fields()
	profile = ff_compatibility._profile_values(doc.get("pos_profile"))
	for name in names:
		row = frappe.get_cached_value("Pricing Rule", name, fields, as_dict=True)
		if not row or not ff_compatibility.rule_applies_to_doc(row, doc, profile):
			return False
	return True
