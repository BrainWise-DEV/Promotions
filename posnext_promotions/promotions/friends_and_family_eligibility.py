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

**Approved email domains are mandatory.** A customer who matches the Group /
Flag rule (an *F&F member*) must ALSO have an email (``Customer.email_id``)
whose domain is exactly one of the enabled Approved Domains (case-insensitive;
sub-domains must be listed as their own rows), and that email must be
OTP-verified through POSNext and unchanged since (``custom_email_verified`` = 1
and ``custom_verified_email`` = ``email_id``) — always, independent of
``POS Settings.enable_customer_email_verification``. With no enabled domain
nobody gets F&F (``no_approved_domains``). Non-members are never asked for an
email or verification.

Disabled customers are never eligible. The server is the authority: the discount
engine must call :func:`get_program_for_customer` with the invoice's customer and
posting date rather than trust a client-side flag, so a cashier cannot force F&F
onto an ineligible customer.
"""

from __future__ import annotations

import re
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
	profile_allows_ff,
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
# The POS Profile has "Allow F&F" unticked: F&F is off for the whole profile.
REASON_PROFILE_NOT_ALLOWED = "profile_not_allowed"
REASON_CUSTOMER_NOT_FOUND = "customer_not_found"
REASON_CUSTOMER_DISABLED = "customer_disabled"
REASON_CUSTOMER_GROUP = "customer_group"
REASON_CUSTOMER_FLAG = "customer_flag"
REASON_NOT_ELIGIBLE = "not_eligible"
# Approved-domain gate (customer matched Group / Flag but the email fails).
REASON_EMAIL_DOMAIN_NOT_APPROVED = "email_domain_not_approved"
REASON_EMAIL_NOT_VERIFIED = "email_not_verified"
# F&F is on but Settings has no enabled Approved Domain: nobody gets F&F.
REASON_NO_APPROVED_DOMAINS = "no_approved_domains"
EMAIL_REASONS = (REASON_EMAIL_DOMAIN_NOT_APPROVED, REASON_EMAIL_NOT_VERIFIED, REASON_NO_APPROVED_DOMAINS)

# POSNext customer email OTP verification (read defensively; pos_next is optional).
VERIFIED_FLAG_FIELD = "custom_email_verified"
VERIFIED_EMAIL_FIELD = "custom_verified_email"


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
	# Approved domain the customer's email matched (when the domain gate is on).
	email_domain: str | None = None
	# Matches the Group / Flag rule (whether or not the email rule then passed).
	# POSNext uses it to offer email OTP verification for F&F.
	ff_member: bool = False

	@property
	def message(self) -> str:
		if self.eligible:
			# Configured "ff_activated" Friends and Family Message, else a generic note.
			customer_message = self.customer_message or {}
			return customer_message.get("message") or _("Customer is eligible for Friends & Family")
		if self.reason == REASON_NO_CUSTOMER:
			return _("Select a customer to check Friends & Family eligibility")
		if self.reason == REASON_PROGRAM_INACTIVE:
			return _("The Friends & Family program is not active")
		if self.reason == REASON_PROFILE_NOT_ALLOWED:
			return _("Friends & Family is not allowed on this POS Profile")
		if self.reason == REASON_CUSTOMER_NOT_FOUND:
			return _("Customer {0} not found").format(self.customer)
		if self.reason == REASON_CUSTOMER_DISABLED:
			return _("Customer {0} is disabled").format(self.customer)
		if self.reason == REASON_EMAIL_DOMAIN_NOT_APPROVED:
			return _(
				"No Friends & Family: customer {0} needs an email on an approved company domain"
			).format(self.customer)
		if self.reason == REASON_EMAIL_NOT_VERIFIED:
			return _(
				"No Friends & Family: verify the email of customer {0} (OTP) first"
			).format(self.customer)
		if self.reason == REASON_NO_APPROVED_DOMAINS:
			return _("Friends & Family has no approved email domains configured")
		return _("Customer {0} is not eligible for Friends & Family").format(self.customer)

	def to_client_payload(self) -> dict:
		return {
			"customer": self.customer,
			"eligible": self.eligible,
			"reason": self.reason,
			"message": self.message,
			"customer_group": self.customer_group,
			"matched_customer_group": self.matched_customer_group,
			"email_domain": self.email_domain,
			"ff_member": self.ff_member,
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


def get_customer_eligibility(
	customer: str | None, on_date=None, pos_profile: str | None = None
) -> CustomerEligibility:
	"""Whether ``customer`` gets Friends & Family on ``on_date`` (today when omitted), and why.

	With ``pos_profile`` the profile's **Allow F&F** check is enforced first: when
	it is off nothing applies, whoever the customer is (``program`` stays ``None``
	so every caller treats it as "no program").
	"""
	customer = cstr(customer).strip() or None
	if not profile_allows_ff(pos_profile):
		return CustomerEligibility(customer=customer, eligible=False, reason=REASON_PROFILE_NOT_ALLOWED)
	if not customer:
		return CustomerEligibility(customer=None, eligible=False, reason=REASON_NO_CUSTOMER)

	program = get_active_program(on_date)
	if program is None:
		return CustomerEligibility(customer=customer, eligible=False, reason=REASON_PROGRAM_INACTIVE)

	flag_available = has_flag_field()
	fields = ["name", "customer_group", "disabled", "email_id"]
	if flag_available:
		fields.append(FLAG_FIELD)
	if has_verification_fields():
		fields += [VERIFIED_FLAG_FIELD, VERIFIED_EMAIL_FIELD]
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

	member_reason = None
	if basis in (BASIS_GROUP, BASIS_GROUP_OR_FLAG):
		matched = match_customer_group(row.customer_group, get_configured_customer_groups(settings))
		if matched:
			member_reason = REASON_CUSTOMER_GROUP
			result["matched_customer_group"] = matched

	if (
		member_reason is None
		and basis in (BASIS_FLAG, BASIS_GROUP_OR_FLAG)
		and flag_available
		and cint(row.get(FLAG_FIELD))
	):
		member_reason = REASON_CUSTOMER_FLAG

	if member_reason is None:
		return CustomerEligibility(eligible=False, reason=REASON_NOT_ELIGIBLE, **result)

	email_failure, email_domain = check_customer_email(row, settings)
	if email_failure:
		return CustomerEligibility(eligible=False, reason=email_failure, ff_member=True, **result)
	return CustomerEligibility(
		eligible=True, reason=member_reason, email_domain=email_domain, ff_member=True, **result
	)


def is_customer_ff_member(customer: str | None, pos_profile: str | None = None) -> bool:
	"""Customer matches the live program's Group / Flag rule (email rule not applied).

	POSNext calls this to allow email OTP verification for F&F members even when
	its general verification switch is off.
	"""
	return bool(get_customer_eligibility(customer, pos_profile=pos_profile).ff_member)


def is_customer_eligible(customer: str | None, on_date=None, pos_profile: str | None = None) -> bool:
	return get_customer_eligibility(customer, on_date, pos_profile).eligible


def get_program_for_customer(
	customer: str | None, on_date=None, pos_profile: str | None = None
) -> FriendsAndFamilyProgram | None:
	"""The program to apply for this customer, or ``None``. The engine's single entry point."""
	eligibility = get_customer_eligibility(customer, on_date, pos_profile)
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
		# Approved-domain gate: empty = no email restriction.
		"approved_domains": get_approved_domains(settings),
		"requires_verified_email": True,
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


# ---- approved email domains -------------------------------------------------

_HOSTNAME_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_email_domain(value) -> str:
	"""``" @Miraaya.COM "`` / ``"https://www.miraaya.com/"`` → ``"miraaya.com"``.

	Lowercase, trimmed, without leading ``@``, protocol, ``www.``, path or
	trailing dot. Validity is checked separately (:func:`is_valid_email_domain`).
	"""
	domain = cstr(value).strip().lower()
	domain = re.sub(r"^[a-z][a-z0-9+.-]*://", "", domain)
	domain = domain.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
	domain = domain.lstrip("@").strip().rstrip(".")
	if domain.startswith("www."):
		domain = domain[4:]
	return domain


def is_valid_email_domain(domain) -> bool:
	"""Hostname with at least one dot, letters-only TLD, no ``@`` / spaces."""
	domain = cstr(domain)
	if not domain or len(domain) > 253 or "." not in domain:
		return False
	labels = domain.split(".")
	if not all(_HOSTNAME_LABEL.match(label) for label in labels):
		return False
	return labels[-1].isalpha() and len(labels[-1]) >= 2


def email_domain_of(email) -> str | None:
	"""Domain part of a syntactically plausible email, normalised; else None."""
	email = cstr(email).strip().lower()
	if email.count("@") != 1 or any(ch.isspace() for ch in email):
		return None
	local, domain = email.split("@")
	domain = domain.rstrip(".")
	if not local or not is_valid_email_domain(domain):
		return None
	return domain


def get_approved_domains(settings=None) -> list[str]:
	"""Enabled approved domains from Friends and Family Settings (normalised)."""
	settings = settings or frappe.get_cached_doc("Friends and Family Settings")
	domains = []
	for row in settings.get("approved_domains") or []:
		if cint(row.get("enabled")) and row.get("email_domain"):
			domain = normalize_email_domain(row.email_domain)
			if domain and domain not in domains:
				domains.append(domain)
	return domains


def get_approved_domain_for_email(email, settings=None) -> str | None:
	"""The approved domain ``email`` belongs to — exact match only (no sub-domains)."""
	domain = email_domain_of(email)
	if not domain:
		return None
	return domain if domain in get_approved_domains(settings) else None


def is_approved_email(email, settings=None) -> bool:
	"""True when the email's domain is exactly an enabled approved domain."""
	return get_approved_domain_for_email(email, settings) is not None


def has_verification_fields() -> bool:
	"""POSNext's customer email verification fields exist on Customer."""
	meta = frappe.get_meta("Customer")
	return meta.has_field(VERIFIED_FLAG_FIELD) and meta.has_field(VERIFIED_EMAIL_FIELD)


def check_customer_email(row, settings) -> tuple[str | None, str | None]:
	"""Mandatory email gate for a Group / Flag member. Returns ``(failure_reason, matched_domain)``.

	1. No enabled approved domain → ``no_approved_domains``.
	2. ``email_id`` missing / not exactly on an approved domain → ``email_domain_not_approved``.
	3. Not OTP-verified, or changed since verification (or the POSNext
	   verification fields are missing, so it cannot be verified) → ``email_not_verified``.
	"""
	approved = get_approved_domains(settings)
	if not approved:
		return REASON_NO_APPROVED_DOMAINS, None
	email = cstr(row.get("email_id")).strip().lower()
	domain = email_domain_of(email)
	if not domain or domain not in approved:
		return REASON_EMAIL_DOMAIN_NOT_APPROVED, None
	verified_email = cstr(row.get(VERIFIED_EMAIL_FIELD)).strip().lower()
	if not cint(row.get(VERIFIED_FLAG_FIELD)) or verified_email != email:
		return REASON_EMAIL_NOT_VERIFIED, None
	return None, domain
