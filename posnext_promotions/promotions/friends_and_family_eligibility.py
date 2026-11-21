# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Friends & Family customer eligibility.

A customer is entitled to the program when it is live (see
:func:`posnext_promotions.promotions.friends_and_family.get_active_program`) and
**both** of the following are true:

1. ``Customer.email_id`` domain is exactly one of the enabled **Approved Domains**
   on Friends and Family Settings (case-insensitive; sub-domains must be listed
   as their own rows).
2. That email is OTP-verified through POSNext and unchanged since
   (``custom_email_verified`` = 1 and ``custom_verified_email`` = ``email_id``) —
   always, independent of ``POS Settings.enable_customer_email_verification``.

With no enabled Approved Domain nobody gets F&F (``no_approved_domains``).

Customer Group and the legacy ``Friends & Family Member`` checkbox are **not**
part of eligibility. ``ff_member`` in the payload means the email domain is
approved (so POSNext can offer OTP even when general verification is off).

Disabled customers are never eligible. The server is the authority: the discount
engine must call :func:`get_program_for_customer` with the invoice's customer and
posting date rather than trust a client-side flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate, nowdate
from frappe.utils.caching import request_cache

from posnext_promotions.promotions.friends_and_family import (
	FriendsAndFamilyProgram,
	forget_request_cache,
	get_active_program,
	get_settings,
	profile_allows_ff,
)

# Legacy constants kept so older callers / logs do not break on import.
BASIS_GROUP_OR_FLAG = "Customer Group or Flag"
BASIS_GROUP = "Customer Group"
BASIS_FLAG = "Customer Flag"
BASES = (BASIS_GROUP_OR_FLAG, BASIS_GROUP, BASIS_FLAG)

# Custom Field on Customer (hidden; no longer gates eligibility).
FLAG_FIELD = "custom_is_friends_and_family"
FLAG_MANAGER_ROLES = ("System Manager", "Sales Manager")

CUSTOMER_FLAG_FIELD_SPEC = {
	"fieldname": FLAG_FIELD,
	"module": "POSNext Promotions",
	"label": "Friends & Family Member",
	"fieldtype": "Check",
	"default": "1",
	"insert_after": "customer_group",
	"hidden": 1,
	"in_standard_filter": 0,
	"description": "Legacy field. Friends & Family eligibility is based only on an "
	"OTP-verified email on an approved company domain.",
}

REASON_NO_CUSTOMER = "no_customer"
REASON_PROGRAM_INACTIVE = "program_inactive"
REASON_PROFILE_NOT_ALLOWED = "profile_not_allowed"
REASON_CUSTOMER_NOT_FOUND = "customer_not_found"
REASON_CUSTOMER_DISABLED = "customer_disabled"
# Legacy reason codes (no longer produced by get_customer_eligibility).
REASON_CUSTOMER_GROUP = "customer_group"
REASON_CUSTOMER_FLAG = "customer_flag"
REASON_NOT_ELIGIBLE = "not_eligible"
REASON_EMAIL_DOMAIN_NOT_APPROVED = "email_domain_not_approved"
REASON_EMAIL_NOT_VERIFIED = "email_not_verified"
REASON_NO_APPROVED_DOMAINS = "no_approved_domains"
# Success reason when domain + verified email pass.
REASON_EMAIL_VERIFIED = "email_verified"
EMAIL_REASONS = (REASON_EMAIL_DOMAIN_NOT_APPROVED, REASON_EMAIL_NOT_VERIFIED, REASON_NO_APPROVED_DOMAINS)

VERIFIED_FLAG_FIELD = "custom_email_verified"
VERIFIED_EMAIL_FIELD = "custom_verified_email"


@dataclass(frozen=True)
class CustomerEligibility:
	customer: str | None
	eligible: bool
	reason: str
	customer_group: str | None = None
	matched_customer_group: str | None = None
	program: FriendsAndFamilyProgram | None = None
	email_domain: str | None = None
	# True when the customer's email domain is on an approved domain (OTP may be offered).
	ff_member: bool = False

	@property
	def message(self) -> str:
		if self.eligible:
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
	"""Legacy helper — Group/Flag basis is no longer used for eligibility."""
	basis = cstr(settings.get("eligibility_basis"))
	return basis if basis in BASES else BASIS_GROUP_OR_FLAG


def get_configured_customer_groups(settings) -> list[str]:
	"""Legacy helper — eligible customer groups are ignored."""
	return [cstr(row.customer_group) for row in settings.get("eligible_customer_groups") or [] if row.customer_group]


def has_flag_field() -> bool:
	return frappe.get_meta("Customer").has_field(FLAG_FIELD)


def match_customer_group(customer_group: str | None, eligible_groups) -> str | None:
	"""Legacy helper retained for audit code; always returns None for new eligibility."""
	eligible_groups = [g for g in eligible_groups or [] if g]
	if not customer_group or not eligible_groups:
		return None
	if customer_group in eligible_groups:
		return customer_group

	from frappe.query_builder import DocType, Order

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
	"""Whether ``customer`` gets Friends & Family on ``on_date`` (today when omitted).

	Eligibility is approved email domain + OTP-verified email only. Computed once
	per request for the same customer, date and profile (:func:`clear_cache` after
	a write it reads).
	"""
	return _cached_eligibility(
		cstr(customer).strip() or None, getdate(on_date or nowdate()), cstr(pos_profile).strip() or None
	)


@request_cache
def _cached_eligibility(customer: str | None, on_date, pos_profile: str | None) -> CustomerEligibility:
	return _compute_eligibility(customer, on_date, pos_profile)


def clear_cache(doc=None, method=None) -> None:
	"""Forget this request's eligibility and Allow-F&F answers.

	Customer / POS Profile ``on_update``; Settings saves clear every request cache.
	"""
	forget_request_cache(_cached_eligibility, profile_allows_ff)


def _compute_eligibility(customer: str | None, on_date, pos_profile: str | None) -> CustomerEligibility:
	if not profile_allows_ff(pos_profile):
		return CustomerEligibility(customer=customer, eligible=False, reason=REASON_PROFILE_NOT_ALLOWED)
	if not customer:
		return CustomerEligibility(customer=None, eligible=False, reason=REASON_NO_CUSTOMER)

	program = get_active_program(on_date)
	if program is None:
		return CustomerEligibility(customer=customer, eligible=False, reason=REASON_PROGRAM_INACTIVE)

	fields = ["name", "customer_group", "disabled", "email_id"]
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
	email_failure, email_domain, domain_ok = check_customer_email(row, settings)
	ff_member = bool(domain_ok)
	if email_failure:
		return CustomerEligibility(
			eligible=False, reason=email_failure, email_domain=email_domain, ff_member=ff_member, **result
		)
	return CustomerEligibility(
		eligible=True,
		reason=REASON_EMAIL_VERIFIED,
		email_domain=email_domain,
		ff_member=True,
		**result,
	)


def is_customer_ff_member(customer: str | None, pos_profile: str | None = None) -> bool:
	"""True when the customer's email domain is approved (OTP may be offered for F&F)."""
	return bool(get_customer_eligibility(customer, pos_profile=pos_profile).ff_member)


def _log_verification_change(customer: str, verified: bool, email_domain: str | None, via: str | None) -> None:
	"""F&F Log row for an email-verification change. Domain and flags only, never the address.

	Inside a save of this Customer (POS Next resets the verification from its
	``on_update`` when the email changes) the Customer ``on_update`` logger writes the
	row, with the diff and the previous qualifying reason: one change, one row.
	"""
	if ("Customer", customer) in (frappe.flags.currently_saving or []):
		return
	from posnext_promotions.promotions.friends_and_family_log import (
		LOG_ELIGIBILITY_APPLIED,
		LOG_ELIGIBILITY_REMOVED,
		write_log,
	)

	write_log(
		LOG_ELIGIBILITY_APPLIED if verified else LOG_ELIGIBILITY_REMOVED,
		customer=customer,
		reference_doctype="Customer",
		reference_name=customer,
		reason=REASON_EMAIL_VERIFIED if verified else REASON_EMAIL_NOT_VERIFIED,
		message=_("Customer {0} email verification changed ({1})").format(customer, via or "-"),
		details={"email_domain": email_domain, "verified": bool(verified), "via": via},
	)


def benefit_provider() -> dict:
	"""``pos_next_customer_benefit_providers`` hook: lets POS Next ask who is an F&F member,
	and what F&F rate the server gives each row of an invoice (``fnf_line_rates``)."""
	return {
		"is_member": is_customer_ff_member,
		"on_verification_change": _log_verification_change,
		"fnf_line_rates": _fnf_line_rates,
	}


def _fnf_line_rates(doc) -> dict:
	from posnext_promotions.promotions.friends_and_family_apply import fnf_line_rates

	return fnf_line_rates(doc)


def is_customer_eligible(customer: str | None, on_date=None, pos_profile: str | None = None) -> bool:
	return get_customer_eligibility(customer, on_date, pos_profile).eligible


def get_program_for_customer(
	customer: str | None, on_date=None, pos_profile: str | None = None
) -> FriendsAndFamilyProgram | None:
	"""The program to apply for this customer, or ``None``. The engine's single entry point."""
	eligibility = get_customer_eligibility(customer, on_date, pos_profile)
	return eligibility.program if eligibility.eligible else None


def get_eligibility_rules_payload(settings=None) -> dict:
	"""Eligibility criteria for an offline POS to pre-check a cached customer."""
	settings = settings or get_settings()
	return {
		"basis": "approved_email_domain",
		"customer_groups": [],
		"flag_field": None,
		"approved_domains": get_approved_domains(settings),
		"requires_verified_email": True,
	}


# ---- Customer hooks ---------------------------------------------------------


def _flag_default(doc) -> int:
	field = doc.meta.get_field(FLAG_FIELD)
	return cint(field.default) if field and field.default not in (None, "") else 0


def guard_flag_changes(doc, method=None):
	"""Customer.validate — flag is legacy/hidden; still restrict edits to managers."""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
		return
	if not doc.meta.has_field(FLAG_FIELD):
		return

	before = doc.get_doc_before_save()
	old_value = cint(before.get(FLAG_FIELD)) if before else _flag_default(doc)
	if cint(doc.get(FLAG_FIELD)) == old_value:
		return
	if set(frappe.get_roles()) & set(FLAG_MANAGER_ROLES):
		return

	frappe.throw(
		_("Only a Sales Manager or System Manager can change the Friends & Family Member flag"),
		frappe.PermissionError,
	)


def ensure_customer_flag_field():
	"""Ensure the legacy Friends & Family Member checkbox exists and stays hidden."""
	if not frappe.db.exists("DocType", "Customer"):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields({"Customer": [CUSTOMER_FLAG_FIELD_SPEC]}, update=True)


# ---- approved email domains -------------------------------------------------

_HOSTNAME_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_email_domain(value) -> str:
	"""``" @Miraaya.COM "`` / ``"https://www.miraaya.com/"`` → ``"miraaya.com"``."""
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


def check_customer_email(row, settings) -> tuple[str | None, str | None, bool]:
	"""Email gate. Returns ``(failure_reason, matched_domain, domain_approved)``.

	1. No enabled approved domain → ``no_approved_domains`` (domain_approved=False).
	2. ``email_id`` missing / not on an approved domain → ``email_domain_not_approved``.
	3. Not OTP-verified (or verification fields missing) → ``email_not_verified``
	   with domain_approved=True so POS can offer OTP.
	"""
	approved = get_approved_domains(settings)
	if not approved:
		return REASON_NO_APPROVED_DOMAINS, None, False
	email = cstr(row.get("email_id")).strip().lower()
	domain = email_domain_of(email)
	if not domain or domain not in approved:
		return REASON_EMAIL_DOMAIN_NOT_APPROVED, None, False
	verified_email = cstr(row.get(VERIFIED_EMAIL_FIELD)).strip().lower()
	if not cint(row.get(VERIFIED_FLAG_FIELD)) or verified_email != email:
		return REASON_EMAIL_NOT_VERIFIED, domain, True
	return None, domain, True
