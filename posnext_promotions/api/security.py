# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Authorization guards for posnext_promotions whitelisted endpoints.

Delegates to ``pos_next.api.security`` when available, with an identical
standalone fallback so this app stays protected regardless of deploy order.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

try:
	from pos_next.api.security import (
		SUPERVISOR_ROLES,
		ensure_desk_user,
		ensure_pos_supervisor,
		ensure_pos_user,
		is_pos_supervisor,
	)
except ImportError:
	SUPERVISOR_ROLES: frozenset[str] = frozenset({"Nexus POS Manager", "System Manager"})

	def is_pos_supervisor(user: str | None = None) -> bool:
		user = user or frappe.session.user
		if not user or user == "Guest":
			return False
		if user == "Administrator":
			return True
		roles = frappe.get_roles(user) or ()
		return bool(SUPERVISOR_ROLES.intersection(roles))

	def ensure_desk_user(user: str | None = None) -> str:
		user = user or frappe.session.user
		if not user or user == "Guest":
			frappe.throw(_("Authentication required"), frappe.AuthenticationError)
		if user == "Administrator":
			return user
		user_info = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
		if not user_info or not cint(user_info.get("enabled")):
			frappe.throw(_("User account is disabled"), frappe.PermissionError)
		if user_info.get("user_type") != "System User":
			frappe.throw(_("Desk user access required"), frappe.PermissionError)
		return user

	def ensure_pos_supervisor(user: str | None = None) -> str:
		user = ensure_desk_user(user)
		if not is_pos_supervisor(user):
			frappe.throw(_("Supervisor permission required"), frappe.PermissionError)
		return user

	def ensure_pos_user(pos_profile: str | None, user: str | None = None) -> str:
		user = ensure_desk_user(user)
		if not pos_profile:
			frappe.throw(_("POS Profile is required"), frappe.ValidationError)
		if is_pos_supervisor(user):
			return user
		if not frappe.db.exists("POS Profile User", {"parent": pos_profile, "user": user}):
			frappe.throw(
				_("You do not have access to POS Profile {0}").format(pos_profile),
				frappe.PermissionError,
			)
		return user


__all__ = [
	"SUPERVISOR_ROLES",
	"ensure_desk_user",
	"ensure_pos_supervisor",
	"ensure_pos_user",
	"is_pos_supervisor",
]
