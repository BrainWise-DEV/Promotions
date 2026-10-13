# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family customer eligibility.

A customer is entitled to the program when it is live (see
:func:`posnext_promotions.promotions.friends_and_family.get_active_program`) and
the customer matches the criterion configured on **Friends and Family Settings**:

- **Customer Group** — the customer's group is one of the Eligible Customer
  Groups, or a descendant of one (Customer Group is a tree).
- **Customer Flag** — the ``Friends & Family Member`` checkbox on the Customer.
- **Customer Group or Flag** — either of the above.

Disabled customers are never eligible. The server is the authority: the discount
engine must call :func:`get_program_for_customer` with the invoice's customer and
posting date rather than trust a client-side flag, so a cashier cannot force F&F
onto an ineligible customer.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.query_builder import DocType, Order
from frappe.utils import cint, cstr
from frappe.utils.nestedset import get_descendants_of

from posnext_promotions.promotions.friends_and_family import (
	FriendsAndFamilyProgram,
	get_active_program,
	get_settings,
)

BASIS_GROUP_OR_FLAG = "Customer Group or Flag"
BASIS_GROUP = "Customer Group"
BASIS_FLAG = "Customer Flag"
BASES = (BASIS_GROUP_OR_FLAG, BASIS_GROUP, BASIS_FLAG)

# Custom Field on Customer, created by install.after_migrate.
FLAG_FIELD = "custom_is_friends_and_family"
# Roles allowed to tick / untick the flag (same roles that manage the settings).
FLAG_MANAGER_ROLES = ("System Manager", "Sales Manager")

CUSTOMER_FLAG_FIELD_SPEC = {
	"fieldname": FLAG_FIELD,
	"module": "POSNext Promotions",
	"label": "Friends & Family Member",
	"fieldtype": "Check",
	"default": "0",
	"insert_after": "customer_group",
	"in_standard_filter": 1,
	"description": "Eligible for the Friends & Family discount when the program allows the Customer Flag. "
	"Only a Sales Manager or System Manager can change this.",
}

REASON_NO_CUSTOMER = "no_customer"
REASON_PROGRAM_INACTIVE = "program_inactive"
REASON_CUSTOMER_NOT_FOUND = "customer_not_found"
REASON_CUSTOMER_DISABLED = "customer_disabled"
REASON_CUSTOMER_GROUP = "customer_group"
REASON_CUSTOMER_FLAG = "customer_flag"
REASON_NOT_ELIGIBLE = "not_eligible"


@dataclass(frozen=True)
class CustomerEligibility:
	customer: str | None
	eligible: bool
	reason: str
	customer_group: str | None = None
	# The configured group that matched (the customer's own group or an ancestor).
	matched_customer_group: str | None = None
	# The live program, when there is one — set even for an ineligible customer
	# so callers can tell "program off" from "customer not covered".
	program: FriendsAndFamilyProgram | None = None

	@property
	def message(self) -> str:
		if self.eligible:
			return self.program.user_message or _("Customer is eligible for Friends & Family")
		if self.reason == REASON_NO_CUSTOMER:
			return _("Select a customer to check Friends & Family eligibility")
		if self.reason == REASON_PROGRAM_INACTIVE:
			return _("The Friends & Family program is not active")
		if self.reason == REASON_CUSTOMER_NOT_FOUND:
			return _("Customer {0} not found").format(self.customer)
		if self.reason == REASON_CUSTOMER_DISABLED:
			return _("Customer {0} is disabled").format(self.customer)
		return _("Customer {0} is not eligible for Friends & Family").format(self.customer)

	def to_client_payload(self) -> dict:
		return {
			"customer": self.customer,
			"eligible": self.eligible,
			"reason": self.reason,
			"message": self.message,
			"customer_group": self.customer_group,
			"matched_customer_group": self.matched_customer_group,
			"program_active": self.program is not None,
			"discount_percentage": self.program.discount_percentage if self.eligible else 0,
			"program": self.program.to_client_payload() if self.eligible else None,
			# Customer-facing text (Friends and Family Message "ff_activated").
			"customer_message": self.customer_message,
		}

	@property
	def customer_message(self) -> dict | None:
		if not self.eligible:
			return None
		from posnext_promotions.promotions.friends_and_family_messages import (
			EVENT_FF_ACTIVATED,
			get_customer_message,
		)

		return get_customer_message(EVENT_FF_ACTIVATED, percent=self.program.discount_percentage)


def get_eligibility_basis(settings) -> str:
	basis = cstr(settings.get("eligibility_basis"))
	return basis if basis in BASES else BASIS_GROUP_OR_FLAG


def get_configured_customer_groups(settings) -> list[str]:
	return [cstr(row.customer_group) for row in settings.get("eligible_customer_groups") or [] if row.customer_group]


def has_flag_field() -> bool:
	return frappe.get_meta("Customer").has_field(FLAG_FIELD)


def match_customer_group(customer_group: str | None, eligible_groups) -> str | None:
	"""The eligible group covering ``customer_group`` (itself or nearest ancestor), else ``None``."""
	eligible_groups = [g for g in eligible_groups or [] if g]
	if not customer_group or not eligible_groups:
		return None
	if customer_group in eligible_groups:
		return customer_group

	bounds = frappe.db.get_value("Customer Group", customer_group, ["lft", "rgt"], as_dict=True)
	if not bounds:
		return None

	CustomerGroup = DocType("Customer Group")
	ancestors = (
		frappe.qb.from_(CustomerGroup)
		.select(CustomerGroup.name)
		.where(CustomerGroup.name.isin(eligible_groups))
		.where(CustomerGroup.lft < bounds.lft)
		.where(CustomerGroup.rgt > bounds.rgt)
		.orderby(CustomerGroup.lft, order=Order.desc)
		.limit(1)
		.run(pluck="name")
	)
	return ancestors[0] if ancestors else None


def get_customer_eligibility(customer: str | None, on_date=None) -> CustomerEligibility:
	"""Whether ``customer`` gets Friends & Family on ``on_date`` (today when omitted), and why."""
	customer = cstr(customer).strip() or None
	if not customer:
		return CustomerEligibility(customer=None, eligible=False, reason=REASON_NO_CUSTOMER)

	program = get_active_program(on_date)
	if program is None:
		return CustomerEligibility(customer=customer, eligible=False, reason=REASON_PROGRAM_INACTIVE)

	flag_available = has_flag_field()
	fields = ["name", "customer_group", "disabled"]
	if flag_available:
		fields.append(FLAG_FIELD)
	row = frappe.db.get_value("Customer", customer, fields, as_dict=True)
	if not row:
		return CustomerEligibility(
			customer=customer, eligible=False, reason=REASON_CUSTOMER_NOT_FOUND, program=program
		)

	result = dict(customer=row.name, customer_group=row.customer_group or None, program=program)
	if cint(row.disabled):
		return CustomerEligibility(eligible=False, reason=REASON_CUSTOMER_DISABLED, **result)

	settings = get_settings()
	basis = get_eligibility_basis(settings)

	if basis in (BASIS_GROUP, BASIS_GROUP_OR_FLAG):
		matched = match_customer_group(row.customer_group, get_configured_customer_groups(settings))
		if matched:
			return CustomerEligibility(
				eligible=True, reason=REASON_CUSTOMER_GROUP, matched_customer_group=matched, **result
			)

	if basis in (BASIS_FLAG, BASIS_GROUP_OR_FLAG) and flag_available and cint(row.get(FLAG_FIELD)):
		return CustomerEligibility(eligible=True, reason=REASON_CUSTOMER_FLAG, **result)

	return CustomerEligibility(eligible=False, reason=REASON_NOT_ELIGIBLE, **result)


def is_customer_eligible(customer: str | None, on_date=None) -> bool:
	return get_customer_eligibility(customer, on_date).eligible


def get_program_for_customer(customer: str | None, on_date=None) -> FriendsAndFamilyProgram | None:
	"""The program to apply for this customer, or ``None``. The engine's single entry point."""
	eligibility = get_customer_eligibility(customer, on_date)
	return eligibility.program if eligibility.eligible else None


def get_eligibility_rules_payload(settings=None) -> dict:
	"""Eligibility criteria for an offline POS to pre-check a cached customer.

	Customer groups are pre-expanded to their descendants so the client can do a
	plain lookup on ``customer.customer_group``. The server still re-checks.
	"""
	settings = settings or get_settings()
	basis = get_eligibility_basis(settings)
	groups: set[str] = set()
	if basis != BASIS_FLAG:
		for group in get_configured_customer_groups(settings):
			groups.add(group)
			groups.update(get_descendants_of("Customer Group", group, ignore_permissions=True))
	return {
		"basis": basis,
		"customer_groups": sorted(groups),
		"flag_field": FLAG_FIELD if basis != BASIS_GROUP else None,
	}


# ---- Customer hooks ---------------------------------------------------------


def guard_flag_changes(doc, method=None):
	"""Customer.validate — only managers may change the Friends & Family flag.

	Keeps eligibility out of the cashier's hands: a POS user who can edit the
	customer still cannot make them a Friends & Family member.
	"""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
		return
	if not doc.meta.has_field(FLAG_FIELD):
		return

	before = doc.get_doc_before_save()
	old_value = cint(before.get(FLAG_FIELD)) if before else 0
	if cint(doc.get(FLAG_FIELD)) == old_value:
		return
	if set(frappe.get_roles()) & set(FLAG_MANAGER_ROLES):
		return

	frappe.throw(
		_("Only a Sales Manager or System Manager can change the Friends & Family Member flag"),
		frappe.PermissionError,
	)


def ensure_customer_flag_field():
	"""Create the Friends & Family Member checkbox on Customer (idempotent)."""
	if not frappe.db.exists("DocType", "Customer"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields({"Customer": [CUSTOMER_FLAG_FIELD_SPEC]}, update=True)


# ---- approved email domains (company e-mail eligibility, future OTP flow) ----

def normalize_email_domain(value) -> str:
	domain = (value or "").strip().lower()
	while domain.startswith("@"):
		domain = domain[1:]
	return domain.strip().rstrip(".")


def get_approved_domains(settings=None) -> list[str]:
	"""Enabled approved domains from Friends and Family Settings."""
	settings = settings or frappe.get_cached_doc("Friends and Family Settings")
	return [
		normalize_email_domain(row.email_domain)
		for row in (settings.get("approved_domains") or [])
		if cint(row.get("enabled")) and row.get("email_domain")
	]


def get_approved_domain_for_email(email, settings=None) -> str | None:
	"""Matching approved domain for ``email`` (exact domain or a sub-domain of it), else None."""
	email = (email or "").strip().lower()
	if email.count("@") != 1:
		return None
	local, domain = email.split("@")
	domain = normalize_email_domain(domain)
	if not local or not domain:
		return None
	for approved in get_approved_domains(settings):
		if domain == approved or domain.endswith("." + approved):
			return approved
	return None


def is_approved_email(email, settings=None) -> bool:
	"""True when the e-mail belongs to an enabled approved company domain.

	Not yet part of ``get_customer_eligibility`` (Group / Flag rules are
	unchanged); intended for the e-mail + OTP enrolment flow.
	"""
	return get_approved_domain_for_email(email, settings) is not None

