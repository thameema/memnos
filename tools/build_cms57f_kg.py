#!/usr/bin/env python3
"""
tools/build_cms57f_kg.py — CMS-0057-F knowledge graph builder.

Downloads Da Vinci FHIR Implementation Guide packages, extracts
CapabilityStatements and profiles, and seeds engram with structured
requirement and gap-tracking memories — including full coverage of:
  - FHIR conformance requirements (CapabilityStatements, profiles)
  - SLA / timing requirements (from CMS rule text)
  - Security requirements (UDAP, SMART on FHIR)
  - Business rules (opt-out, attribution, quarterly refresh, etc.)

Usage:
    python tools/build_cms57f_kg.py [--dry-run] [--api SLUG] [--reset]

Options:
    --dry-run     Print what would be written without writing to engram
    --api SLUG    Only process a specific API (patient-access, provider-access,
                  prior-auth, p2p, provider-directory)
    --reset       Delete existing cms57f memories before rebuilding
    --ns NS       Engram namespace (default: org:hc:cms57f)
    --list        List available API slugs and exit
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGRAM_API = os.environ.get("ENGRAM_API_URL", "http://127.0.0.1:8766")
ENGRAM_KEY = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")
DEFAULT_NS = "org:hc:cms57f"
FHIR_PKG_BASE = "https://packages.fhir.org"

# Cache downloaded packages to avoid re-downloading on repeated runs
PKG_CACHE_DIR = Path.home() / ".engram" / "fhir-packages"
PKG_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# CMS-0057-F API Registry
# Per 42 CFR Part 438 / 45 CFR Part 156, CMS Interoperability and Prior
# Authorization Final Rule (CMS-0057-F), published January 2024.
# ---------------------------------------------------------------------------

@dataclass
class ApiDef:
    slug: str
    name: str
    short: str
    cfr: str
    deadline: str          # ISO-8601 date
    cms_section: str
    igs: list[str]         # package slugs on packages.fhir.org
    ig_versions: dict[str, str]
    description: str
    key_resources: list[str]     # primary FHIR resource types required
    required_operations: list[str]
    notes: str = ""
    tags: list[str] = field(default_factory=list)


API_REGISTRY: dict[str, ApiDef] = {

    "patient-access": ApiDef(
        slug="patient-access",
        name="Patient Access API",
        short="PA API",
        cfr="45 CFR 156.122(a)",
        deadline="2026-01-01",
        cms_section="§ 156.122(a)",
        igs=[
            "hl7.fhir.us.davinci-pdex",
            "hl7.fhir.us.core",
            "hl7.fhir.uv.smart-app-launch",
            "hl7.fhir.us.udap-security",
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pdex": "2.1.0",
            "hl7.fhir.us.core": "6.1.0",
            "hl7.fhir.uv.smart-app-launch": "2.2.0",
            "hl7.fhir.us.udap-security": "2.0.0",
        },
        description=(
            "Impacted payers MUST provide patients (and their authorized "
            "representatives) with access to their own data via a FHIR R4 API "
            "using SMART on FHIR. Required data includes claims/encounters, "
            "clinical data, prior authorization status and reason for denial. "
            "Implemented using Da Vinci PDex and US Core."
        ),
        key_resources=[
            "Patient", "Coverage", "ExplanationOfBenefit", "Observation",
            "Condition", "Medication", "MedicationRequest", "Procedure",
            "DiagnosticReport", "DocumentReference", "Immunization",
            "AllergyIntolerance", "CarePlan", "Goal",
            "ClaimResponse",  # for prior auth status
        ],
        required_operations=[
            "GET /Patient", "GET /Coverage", "GET /ExplanationOfBenefit",
            "GET /Claim (PA status)", "GET /ClaimResponse",
        ],
        notes="Deadline passed Jan 2026. Must include prior auth status and denial reason.",
        tags=["patient-access", "jan-2026", "pdex", "us-core", "smart-on-fhir"],
    ),

    "provider-access": ApiDef(
        slug="provider-access",
        name="Provider Access API",
        short="PRA API",
        cfr="45 CFR 156.122(b)",
        deadline="2027-01-01",
        cms_section="§ 156.122(b)",
        igs=[
            "hl7.fhir.us.davinci-pdex",
            "hl7.fhir.us.core",
            "hl7.fhir.us.davinci-hrex",
            "hl7.fhir.uv.smart-app-launch",
            "hl7.fhir.us.udap-security",
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pdex": "2.1.0",
            "hl7.fhir.us.core": "6.1.0",
            "hl7.fhir.us.davinci-hrex": "1.2.0",
            "hl7.fhir.uv.smart-app-launch": "2.2.0",
            "hl7.fhir.us.udap-security": "2.0.0",
        },
        description=(
            "Impacted payers MUST allow treating providers (in-network) to "
            "query their patients' data held by the payer. Providers must use "
            "SMART on FHIR with appropriate patient-matching. Data includes "
            "claims, clinical records, and prior authorization status. "
            "Payers must not market or use data for purposes other than care. "
            "Deadline: January 1, 2027."
        ),
        key_resources=[
            "Patient", "Coverage", "ExplanationOfBenefit",
            "Observation", "Condition", "MedicationRequest", "Procedure",
            "DiagnosticReport", "DocumentReference", "Claim", "ClaimResponse",
        ],
        required_operations=[
            "GET /Patient (with matching)", "GET /Coverage",
            "GET /ExplanationOfBenefit", "GET /ClaimResponse",
            "POST /Patient/$match",
        ],
        notes=(
            "Providers query on behalf of patients; payer must match provider "
            "to their in-network panel. Attribution/relationship validation required."
        ),
        tags=["provider-access", "jan-2027", "pdex", "hrex", "patient-match"],
    ),

    "prior-auth": ApiDef(
        slug="prior-auth",
        name="Prior Authorization API",
        short="PA API",
        cfr="45 CFR 156.122(c)",
        deadline="2027-01-01",
        cms_section="§ 156.122(c)",
        igs=[
            "hl7.fhir.us.davinci-pas",
            "hl7.fhir.us.davinci-crd",
            "hl7.fhir.us.davinci-dtr",
            "hl7.fhir.us.davinci-cdex",
            "hl7.fhir.us.davinci-hrex",
            "hl7.fhir.uv.smart-app-launch",
            "hl7.fhir.us.udap-security",
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pas": "2.2.1",
            "hl7.fhir.us.davinci-crd": "2.2.1",
            "hl7.fhir.us.davinci-dtr": "2.2.0",
            "hl7.fhir.us.davinci-cdex": "2.1.0",
            "hl7.fhir.us.davinci-hrex": "1.2.0",
            "hl7.fhir.uv.smart-app-launch": "2.2.0",
            "hl7.fhir.us.udap-security": "2.0.0",
        },
        description=(
            "Impacted payers MUST implement a FHIR-based electronic prior "
            "authorization API. Providers submit PA requests as FHIR Bundles "
            "containing a Claim resource. Payer responds with ClaimResponse. "
            "Full workflow includes CRD (real-time coverage requirements), "
            "DTR (documentation templates), PAS (submit/inquire/update/cancel), "
            "and CDex (clinical data exchange for supporting docs). "
            "Deadline: January 1, 2027."
        ),
        key_resources=[
            "Claim",              # PA request
            "ClaimResponse",      # PA decision
            "Bundle",             # PA request bundle
            "Task",               # PA workflow coordination
            "CommunicationRequest",  # additional info request
            "Communication",      # additional info response
            "Coverage",
            "Patient",
            "Practitioner",
            "Organization",
            "ServiceRequest",
            "MedicationRequest",
        ],
        required_operations=[
            "POST /Claim/$submit",
            "POST /Claim/$inquire",
            "POST /ClaimResponse (pended)",
            "POST /Task (pended update)",
            "GET /ClaimResponse/{id}",
            "POST /Subscription (PA status webhook)",
            "POST /Coverage hook (CRD)",
            "GET /Questionnaire (DTR)",
            "POST /QuestionnaireResponse (DTR)",
        ],
        notes=(
            "Most complex set of IGs. CRD fires before PA submission. "
            "DTR populates documentation. PAS is the actual PA submission layer. "
            "CDex exchanges supporting clinical docs. All 4 must be coordinated."
        ),
        tags=["prior-auth", "jan-2027", "pas", "crd", "dtr", "cdex", "pa-workflow"],
    ),

    "p2p": ApiDef(
        slug="p2p",
        name="Payer-to-Payer Data Exchange",
        short="P2P",
        cfr="45 CFR 156.122(d)",
        deadline="2027-01-01",
        cms_section="§ 156.122(d)",
        igs=[
            "hl7.fhir.us.davinci-pdex",
            "hl7.fhir.us.davinci-hrex",
            "hl7.fhir.us.core",
            "hl7.fhir.us.davinci-atr",
            "hl7.fhir.uv.smart-app-launch",
            "hl7.fhir.us.udap-security",
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pdex": "2.1.0",
            "hl7.fhir.us.davinci-hrex": "1.2.0",
            "hl7.fhir.us.core": "6.1.0",
            "hl7.fhir.us.davinci-atr": "2.1.0",
            "hl7.fhir.uv.smart-app-launch": "2.2.0",
            "hl7.fhir.us.udap-security": "2.0.0",
        },
        description=(
            "When a member enrolls in a new plan, the new payer MUST request "
            "the member's data from all prior payers (up to 5 years). "
            "Prior payer MUST respond with clinical data, claims, "
            "and prior authorization information. Uses FHIR R4 with "
            "bulk data ($export) or individual resource queries. "
            "Member consent required. Deadline: January 1, 2027."
        ),
        key_resources=[
            "Patient", "Coverage", "ExplanationOfBenefit",
            "Observation", "Condition", "MedicationRequest", "Procedure",
            "DiagnosticReport", "DocumentReference",
            "Claim", "ClaimResponse",  # prior auth history
            "Organization",            # payer identity
        ],
        required_operations=[
            "POST /Patient/$match (member matching)",
            "GET /$export (bulk data export)",
            "GET /Patient/{id}/$everything",
            "GET /Coverage",
            "POST /Group (bulk enrollment)",
        ],
        notes=(
            "Flows: (1) new payer identifies prior payer via Coverage, "
            "(2) requests member data via $match then $export, "
            "(3) prior payer responds within 1 business day. "
            "Jan 2027 adds continued exchange requirement for ongoing members."
        ),
        tags=["p2p", "payer-to-payer", "jan-2027", "pdex", "bulk-data", "member-match"],
    ),

    "provider-directory": ApiDef(
        slug="provider-directory",
        name="Provider Directory API",
        short="PD API",
        cfr="45 CFR 156.122(e) / CMS-9115-F",
        deadline="2021-07-01",  # original deadline, updates ongoing
        cms_section="§ 156.122(e)",
        igs=[
            "hl7.fhir.us.davinci-pdex-plan-net",
            "hl7.fhir.us.core",
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pdex-plan-net": "1.2.0",
            "hl7.fhir.us.core": "6.1.0",
        },
        description=(
            "Impacted payers MUST provide a FHIR R4 Provider Directory API "
            "exposing in-network provider, location, and network information. "
            "No authentication required for reads. Data must be updated within "
            "90 business days. Implemented using Da Vinci PDEX Plan-Net IG. "
            "Original deadline was July 2021 (CMS-9115-F); ongoing compliance "
            "maintained under CMS-0057-F attestation requirements."
        ),
        key_resources=[
            "Practitioner",
            "PractitionerRole",
            "Organization",
            "OrganizationAffiliation",
            "Location",
            "HealthcareService",
            "InsurancePlan",
            "Endpoint",
            "Network",
        ],
        required_operations=[
            "GET /Practitioner",
            "GET /PractitionerRole",
            "GET /Organization",
            "GET /OrganizationAffiliation",
            "GET /Location",
            "GET /HealthcareService",
            "GET /InsurancePlan",
            "GET /Endpoint",
        ],
        notes=(
            "Must support no-auth public access. 90-business-day update SLA. "
            "Covers in-network providers, organizations, locations, services. "
            "Plan-Net 1.2.0 is the current STU release aligned with CMS-0057-F attestation."
        ),
        tags=["provider-directory", "plan-net", "no-auth", "cms-9115-f"],
    ),
}


# ---------------------------------------------------------------------------
# SLA / Timing Requirements
# Source: CMS-0057-F final rule text (88 FR 80458, Nov 2023)
# ---------------------------------------------------------------------------

SLA_REQUIREMENTS: dict[str, list[dict]] = {
    "patient-access": [
        {
            "title": "Claim data availability SLA",
            "requirement": "Adjudicated claims and encounter data MUST be available via the API within 1 business day of claim adjudication or encounter close.",
            "cfr": "45 CFR 156.122(a)(1)",
            "severity": "SHALL",
        },
        {
            "title": "Prior authorization status real-time availability",
            "requirement": "Current prior authorization status (approved, denied, pended) and denial reason MUST be available in real-time — it cannot be batched or delayed.",
            "cfr": "45 CFR 156.122(a)(1)(ii)",
            "severity": "SHALL",
        },
        {
            "title": "API availability",
            "requirement": "Patient Access API MUST be available 24/7. Planned maintenance downtime MUST NOT exceed 30 minutes per month. Unplanned downtime MUST be resolved within 4 business hours.",
            "cfr": "45 CFR 156.122(a)",
            "severity": "SHALL",
        },
        {
            "title": "Authorized representative access",
            "requirement": "Patients MUST be able to designate authorized representatives (caregivers, family members) who can access their data on their behalf. OAuth 2.0 delegation scopes required.",
            "cfr": "45 CFR 156.122(a)(3)",
            "severity": "SHALL",
        },
    ],
    "provider-access": [
        {
            "title": "Near-real-time query response",
            "requirement": "Provider queries for individual patient records MUST return results within a reasonable timeframe — synchronous queries SHALL complete within 30 seconds. Async bulk queries SHALL provide status endpoint.",
            "cfr": "45 CFR 156.122(b)",
            "severity": "SHALL",
        },
        {
            "title": "Attribution list update SLA",
            "requirement": "Payer MUST update provider attribution lists (Group resources) within 1 business day of receiving notification of a provider-patient relationship change.",
            "cfr": "45 CFR 156.122(b)(1)",
            "severity": "SHALL",
        },
        {
            "title": "Anti-marketing prohibition",
            "requirement": "Data accessed through the Provider Access API MUST NOT be used for marketing, sales, or any purpose other than treatment, care coordination, and quality improvement. Violation is a CMS enforcement action.",
            "cfr": "45 CFR 156.122(b)(3)",
            "severity": "SHALL",
        },
        {
            "title": "Provider-patient relationship validation",
            "requirement": "Payer MUST validate that the requesting provider has an active treatment relationship with the patient (in-network or treating provider) before returning data. Invalid relationship → 403.",
            "cfr": "45 CFR 156.122(b)(2)",
            "severity": "SHALL",
        },
    ],
    "prior-auth": [
        {
            "title": "Standard PA decision timeframe",
            "requirement": "Standard prior authorization decisions MUST be communicated via API within 7 calendar days of receiving a complete PA request (ClaimResponse or pended Task update). Real-time decisions SHOULD be returned immediately.",
            "cfr": "42 CFR 422.568 / 45 CFR 156.122(c)",
            "severity": "SHALL",
        },
        {
            "title": "Urgent/Expedited PA decision timeframe",
            "requirement": "Urgent/expedited prior authorization requests MUST be decided within 72 hours of receiving a complete request. Request MUST be flagged with priority = urgent in the Claim resource.",
            "cfr": "42 CFR 422.568(b) / 45 CFR 156.122(c)",
            "severity": "SHALL",
        },
        {
            "title": "Pended PA webhook notification",
            "requirement": "When a pended PA request (ClaimResponse.outcome = queued) is decided, payer MUST send a FHIR Subscription notification to the provider's registered endpoint within 1 business day of the decision.",
            "cfr": "45 CFR 156.122(c)(2)",
            "severity": "SHALL",
        },
        {
            "title": "Denial reason specificity",
            "requirement": "Denial (ClaimResponse.outcome = denied) MUST include a specific clinical reason in ClaimResponse.item.adjudication.reason — 'not medically necessary' alone is insufficient. Must reference specific clinical criteria.",
            "cfr": "45 CFR 156.122(c)(1)(iii)",
            "severity": "SHALL",
        },
        {
            "title": "PA tracking in API — no external systems",
            "requirement": "All PA requests submitted via the FHIR API MUST be trackable via the API (GET /ClaimResponse/{id}, Subscription). Payers CANNOT require providers to track PA status in a separate portal or proprietary system.",
            "cfr": "45 CFR 156.122(c)",
            "severity": "SHALL",
        },
    ],
    "p2p": [
        {
            "title": "Member data response SLA — prior payer",
            "requirement": "Prior payer MUST respond to a validated $member-match request and make member data available (via $export or $everything) within 1 business day of receiving the request. Failure triggers CMS enforcement.",
            "cfr": "45 CFR 156.122(d)(1)",
            "severity": "SHALL",
        },
        {
            "title": "New payer initiation window",
            "requirement": "New payer MUST initiate the data exchange request to all prior payers within 30 calendar days of member enrollment. The request MUST cover all prior payers for the preceding 5 years.",
            "cfr": "45 CFR 156.122(d)(2)",
            "severity": "SHALL",
        },
        {
            "title": "Data lookback period",
            "requirement": "Prior payer MUST provide up to 5 years of historical data including: all claims, clinical data, and prior authorization history. No truncation of data is permitted within the 5-year window.",
            "cfr": "45 CFR 156.122(d)(1)(i)",
            "severity": "SHALL",
        },
        {
            "title": "Quarterly ongoing exchange",
            "requirement": "For members who remain continuously enrolled across payers, new payer MUST refresh the data exchange quarterly (every 90 days) for the duration of enrollment. Prior payer MUST respond within 1 business day.",
            "cfr": "45 CFR 156.122(d)(3) — effective Jan 2027",
            "severity": "SHALL",
        },
        {
            "title": "Opt-out processing SLA",
            "requirement": "Member opt-out from P2P data exchange MUST be honored within 1 business day of the member's request. Opt-out MUST be communicated to all queued/scheduled exchange requests before they execute.",
            "cfr": "45 CFR 156.122(d)(4)",
            "severity": "SHALL",
        },
    ],
    "provider-directory": [
        {
            "title": "Data currency SLA",
            "requirement": "Provider directory data MUST be updated within 90 business days of receiving a change notification from a provider or network. Stale data beyond 90 business days is a compliance violation.",
            "cfr": "45 CFR 156.122(e)(1)",
            "severity": "SHALL",
        },
        {
            "title": "No-authentication public access",
            "requirement": "Provider Directory API MUST be publicly accessible without any authentication (no OAuth, no API key). Rate limiting MUST allow at least 1000 requests/hour for third-party directory aggregators.",
            "cfr": "45 CFR 156.122(e)(2)",
            "severity": "SHALL",
        },
        {
            "title": "Minimum dataset",
            "requirement": "Provider directory MUST include at minimum: provider name, specialty, NPI, practice location(s), accepted insurance networks, accepting new patients flag, and telehealth availability.",
            "cfr": "45 CFR 156.122(e)(1)(i-vi)",
            "severity": "SHALL",
        },
    ],
}


# ---------------------------------------------------------------------------
# Security Requirements
# Source: UDAP Security IG 2.0.0, SMART App Launch 2.2.0, CMS-0057-F
# ---------------------------------------------------------------------------

SECURITY_REQUIREMENTS: dict[str, list[dict]] = {
    "patient-access": [
        {
            "title": "SMART on FHIR 2.0 — patient launch",
            "requirement": "SHALL support SMART App Launch 2.2.0 standalone launch pattern. SHALL support patient-level scopes: patient/*.read. SHALL support PKCE (S256). SHALL support token introspection endpoint.",
            "ig": "hl7.fhir.uv.smart-app-launch@2.2.0",
            "severity": "SHALL",
        },
        {
            "title": "UDAP Dynamic Client Registration",
            "requirement": "SHALL support UDAP Dynamic Client Registration (hl7.fhir.us.udap-security 2.0.0) for third-party app registration. Client credentials MUST be validated against UDAP trust community. SHALL support signed software statements (JWT).",
            "ig": "hl7.fhir.us.udap-security@2.0.0",
            "severity": "SHALL",
        },
        {
            "title": "Token lifetimes and refresh",
            "requirement": "Access tokens SHALL have maximum lifetime of 1 hour. Refresh tokens SHALL be supported. Refresh token rotation SHOULD be implemented. Token revocation endpoint MUST be provided.",
            "ig": "hl7.fhir.uv.smart-app-launch@2.2.0",
            "severity": "SHALL",
        },
    ],
    "provider-access": [
        {
            "title": "SMART on FHIR 2.0 — system launch",
            "requirement": "SHALL support SMART Backend Services (system/*.read scopes) for EHR system-to-payer queries. SHALL support asymmetric client credentials (RS384 or ES384 JWT). Bulk queries SHALL use system-level scopes.",
            "ig": "hl7.fhir.uv.smart-app-launch@2.2.0",
            "severity": "SHALL",
        },
        {
            "title": "UDAP B2B Authorization Extension",
            "requirement": "Provider-to-payer queries SHALL use UDAP B2B Authorization Extension (hl7.fhir.us.udap-security Section 4). The B2B extension MUST include: hl7_b2b claim with organization_name, subject_id (provider NPI), and purpose_of_use.",
            "ig": "hl7.fhir.us.udap-security@2.0.0",
            "severity": "SHALL",
        },
        {
            "title": "Scope restrictions — anti-marketing enforcement",
            "requirement": "OAuth scopes issued for Provider Access MUST be restricted to treatment and care coordination purposes. Payer MUST reject requests with purpose_of_use outside [TREAT, HPAYMT, HOPERAT]. Marketing purpose_of_use codes MUST be rejected with 403.",
            "ig": "hl7.fhir.us.udap-security@2.0.0",
            "severity": "SHALL",
        },
    ],
    "prior-auth": [
        {
            "title": "SMART on FHIR 2.0 — EHR launch",
            "requirement": "SHALL support SMART EHR Launch pattern for CRD/DTR. SHALL support context parameters: patient, encounter, fhirContext. SHALL support user-level scopes for DTR form population. Launch URL MUST be registered in FHIR Endpoint.",
            "ig": "hl7.fhir.uv.smart-app-launch@2.2.0",
            "severity": "SHALL",
        },
        {
            "title": "UDAP Dynamic Client Registration for PA intermediaries",
            "requirement": "PAS intermediaries and clearinghouses SHALL support UDAP Dynamic Client Registration. The software statement MUST identify the clearinghouse organization. Payer MUST validate the trust chain before accepting PA submissions.",
            "ig": "hl7.fhir.us.udap-security@2.0.0",
            "severity": "SHALL",
        },
        {
            "title": "Subscription authentication",
            "requirement": "FHIR Subscription notifications (pended PA webhook) MUST use SMART Backend Services token. Subscription endpoint MUST be pre-registered and validated. Notification payload SHALL be signed.",
            "ig": "hl7.fhir.uv.smart-app-launch@2.2.0",
            "severity": "SHALL",
        },
    ],
    "p2p": [
        {
            "title": "UDAP Tiered OAuth for member matching",
            "requirement": "P2P $member-match SHALL use UDAP Tiered OAuth (hl7.fhir.us.udap-security Section 6). New payer authenticates to prior payer using UDAP B2B JWT. The JWT MUST include: hl7_b2b claim with payer organization NPI, purpose_of_use = HPAYMT.",
            "ig": "hl7.fhir.us.udap-security@2.0.0",
            "severity": "SHALL",
        },
        {
            "title": "mTLS for server-to-server bulk export",
            "requirement": "Bulk data export (system-level $export) SHOULD use mutual TLS (mTLS) in addition to OAuth bearer tokens. The TLS certificate MUST chain to a UDAP trust community anchor. TLS 1.2 minimum; TLS 1.3 SHOULD.",
            "ig": "hl7.fhir.us.udap-security@2.0.0",
            "severity": "SHOULD",
        },
        {
            "title": "Payer identity validation",
            "requirement": "Prior payer MUST validate that the requesting entity is an impacted payer (MA plan, Medicaid MCO, CHIP, QHP) using UDAP trust community membership. Requests from unvalidated entities MUST be rejected with 401.",
            "ig": "hl7.fhir.us.udap-security@2.0.0",
            "severity": "SHALL",
        },
        {
            "title": "SMART system scopes for bulk export",
            "requirement": "Bulk $export requests SHALL use SMART Backend Services (system/*.read) scopes. Per-patient $everything requests SHALL use patient-level scopes. Scope validation MUST occur at the resource server (not just the auth server).",
            "ig": "hl7.fhir.uv.smart-app-launch@2.2.0",
            "severity": "SHALL",
        },
    ],
    "provider-directory": [
        {
            "title": "No authentication required",
            "requirement": "Provider Directory API MUST NOT require authentication for read operations. The server MUST NOT issue 401 or 403 for GET requests on directory resources (Practitioner, Organization, Location, etc.). OAuth MAY be supported but MUST NOT be required.",
            "ig": "hl7.fhir.us.davinci-pdex-plan-net@1.2.0",
            "severity": "SHALL",
        },
    ],
}


# ---------------------------------------------------------------------------
# Business Rules
# Non-FHIR operational requirements from CMS-0057-F rule text
# ---------------------------------------------------------------------------

BUSINESS_RULES: dict[str, list[dict]] = {
    "patient-access": [
        {
            "title": "Authorized representative support",
            "requirement": "Payer MUST support OAuth 2.0 delegation allowing a patient's authorized representative to access data on the patient's behalf. The representative's identity MUST be logged. Delegation MUST be revocable by the patient.",
            "category": "access-control",
        },
        {
            "title": "Data portability — third-party app support",
            "requirement": "Payer MUST NOT block or discriminate against third-party applications that have been properly registered via UDAP. Payer CANNOT require patients to use the payer's own app. All SMART/UDAP registered apps MUST receive equal access.",
            "category": "anti-discrimination",
        },
        {
            "title": "Prior auth data included in patient record",
            "requirement": "Patient Access API MUST include current and historical prior authorization data: PA request details, decision (approved/denied/pended), denial reason with specific clinical criteria, and appeal rights information.",
            "category": "data-completeness",
        },
    ],
    "provider-access": [
        {
            "title": "Provider attribution validation",
            "requirement": "Before returning patient data, payer MUST verify: (1) provider has active NPI, (2) provider is in-network with the payer OR has a documented treating relationship, (3) the patient is currently enrolled. All 3 checks required; failure → 403 with OperationOutcome.",
            "category": "access-control",
        },
        {
            "title": "Secondary use prohibition enforcement",
            "requirement": "Payer MUST implement technical controls (purpose_of_use scope validation, audit logging) to detect and prevent data returned via Provider Access API from being used for marketing, underwriting, or non-treatment purposes. Audit trail must be retained for 7 years.",
            "category": "data-use-restriction",
        },
        {
            "title": "Provider group/attribution list management",
            "requirement": "Payers MUST maintain and expose FHIR Group resources representing provider attribution lists. Group MUST include all patients attributed to a provider. Group MUST be queryable by the provider via Group/{id}/$davinci-data-export.",
            "category": "attribution",
        },
    ],
    "prior-auth": [
        {
            "title": "PA list publication",
            "requirement": "Payers MUST publish a current list of services and items requiring prior authorization on their public website AND via FHIR API. The list MUST be updated within 7 calendar days of any change. CMS can cite payers for requiring PA on services not on the list.",
            "category": "transparency",
        },
        {
            "title": "PA decision tracking — no portal redirect",
            "requirement": "Payer MUST NOT redirect providers to a proprietary portal to check PA status. All PA status MUST be accessible via the FHIR API (GET /ClaimResponse/{id} or Subscription notification). Portal access MAY be provided in addition but CANNOT be the only mechanism.",
            "category": "api-only-access",
        },
        {
            "title": "Specific denial reason required",
            "requirement": "ClaimResponse for denied PA (outcome = denied) MUST include: (1) specific clinical criterion that was not met, (2) reference to the clinical guideline used, (3) information on how to appeal. Generic 'not medically necessary' is non-compliant.",
            "category": "denial-transparency",
        },
        {
            "title": "Retrospective PA prohibition",
            "requirement": "Payer MUST NOT require prior authorization for services that were provided in an emergency or urgent situation where obtaining PA in advance was not feasible. Retrospective PA requests MUST be handled via a separate non-FHIR appeals process.",
            "category": "pa-scope",
        },
    ],
    "p2p": [
        {
            "title": "Member opt-out right",
            "requirement": "Members MUST be given the right to opt out of P2P data exchange. Payer MUST provide a mechanism (portal, phone, written request) for opt-out. Opt-out MUST be processed within 1 business day and persisted across re-enrollment events.",
            "category": "consent-and-privacy",
        },
        {
            "title": "Consent resource in $member-match",
            "requirement": "New payer's $member-match request MUST include a FHIR R4 Consent resource in the Parameters bundle (parameter name: consent). Consent MUST have status=active, performer = requesting payer organization, and scope = patient data exchange. Missing or invalid Consent → 422.",
            "category": "consent-and-privacy",
        },
        {
            "title": "5-year lookback — all prior payers",
            "requirement": "New payer MUST request data from ALL prior payers where the member was enrolled within the preceding 5 years. Payer CANNOT selectively query only the most recent prior payer. Member's Coverage history MUST be used to identify all prior payers.",
            "category": "data-completeness",
        },
        {
            "title": "Prior authorization history included",
            "requirement": "P2P exchange MUST include prior authorization history (approved, denied, pended) from the prior payer. ClaimResponse resources with PA information MUST be included in the $export response. This is a specific CMS-0057-F addition over previous PDex requirements.",
            "category": "data-completeness",
        },
        {
            "title": "Attribution list lifecycle — Group resource",
            "requirement": "New payer MUST maintain a FHIR Group resource for each prior payer being queried, representing the cohort of attributed members. Group MUST use Da Vinci ATR (hl7.fhir.us.davinci-atr 2.1.0) profiles. Group identifier MUST be stable across quarterly refreshes.",
            "category": "attribution",
        },
        {
            "title": "Concurrent member enrollment handling",
            "requirement": "If a member is simultaneously enrolled in multiple plans (e.g., Medicare + Medicaid), each payer MUST independently comply with P2P exchange requirements. Data from dual enrollment MUST NOT be merged or de-duplicated in a way that loses provenance.",
            "category": "edge-cases",
        },
    ],
    "provider-directory": [
        {
            "title": "Minimum required data elements",
            "requirement": "Each Practitioner MUST include: NPI (identifier), name, specialty (PractitionerRole.specialty), practice address (Location), accepting new patients (PractitionerRole.availableTime or extension), network affiliations (PractitionerRole.network).",
            "category": "data-completeness",
        },
        {
            "title": "InsurancePlan network linkage",
            "requirement": "Each InsurancePlan MUST be linked to its network via InsurancePlan.network reference. Network MUST link to all in-network providers via PractitionerRole.network. Broken links are a compliance violation detectable by CMS auditors.",
            "category": "data-integrity",
        },
        {
            "title": "90-business-day update SLA enforcement",
            "requirement": "Payer MUST have a documented process for receiving and processing provider change notifications. Change timestamp MUST be recorded. If update is not applied within 90 business days, payer MUST document the reason. CMS audits directory accuracy quarterly.",
            "category": "data-currency",
        },
    ],
}


# ---------------------------------------------------------------------------
# Reporting Requirements
# Source: CMS-0057-F final rule 45 CFR 156.122, 88 FR 80458 (Nov 2023)
# ---------------------------------------------------------------------------

REPORTING_REQUIREMENTS: dict[str, list[dict]] = {
    "patient-access": [
        {
            "title": "Annual API Operational Attestation",
            "requirement": (
                "Impacted payers MUST annually attest to CMS that the Patient Access API is "
                "operational, publicly accessible, and compliant with 45 CFR 156.122(a). "
                "MA plans attest via HPMS. QHP issuers attest via Healthcare.gov annual "
                "certification. Medicaid MCOs attest via their state Medicaid agency. "
                "False attestation is a compliance violation subject to civil monetary penalties."
            ),
            "cfr": "45 CFR 156.122(a) / CMS HPMS annual certification",
            "frequency": "Annual",
            "submitted_to": "CMS via HPMS (MA) / Healthcare.gov (QHP) / State Medicaid agency",
        },
        {
            "title": "API Usage Metrics — public posting",
            "requirement": (
                "Payers MUST publicly post API usage statistics on their website annually: "
                "(1) number of unique patients who accessed their data, "
                "(2) number of unique third-party applications registered, "
                "(3) API uptime percentage (must be ≥ 99.5%), "
                "(4) number of failed authentication attempts. "
                "Data must be posted within 90 days after the end of the calendar year."
            ),
            "cfr": "45 CFR 156.122(a) / CMS FAQs on interoperability reporting",
            "frequency": "Annual (posted within 90 days of year-end)",
            "submitted_to": "Public website + CMS on request",
        },
        {
            "title": "Member complaints about API access — tracking and reporting",
            "requirement": (
                "Payer MUST track and log all member complaints related to inability to access "
                "data via the Patient Access API. Complaints MUST be resolvable within 30 days. "
                "Summary of API-related complaints must be included in the annual grievance and "
                "appeals report submitted to CMS. Each complaint must record: date, member ID, "
                "app involved (if any), and resolution outcome."
            ),
            "cfr": "45 CFR 156.122(a) / 42 CFR 422.564 (MA grievances)",
            "frequency": "Ongoing tracking; annual summary reporting",
            "submitted_to": "CMS as part of annual grievance/appeals report",
        },
    ],
    "provider-access": [
        {
            "title": "Annual API Operational Attestation",
            "requirement": (
                "Impacted payers MUST annually attest to CMS that the Provider Access API is "
                "operational and compliant with 45 CFR 156.122(b). Attestation must confirm: "
                "(1) API is live and accepting provider queries, "
                "(2) provider-patient attribution validation is implemented, "
                "(3) anti-marketing technical controls are in place, "
                "(4) audit logging of all provider queries is active."
            ),
            "cfr": "45 CFR 156.122(b) / CMS HPMS annual certification",
            "frequency": "Annual",
            "submitted_to": "CMS via HPMS (MA) / state Medicaid agency",
        },
        {
            "title": "Secondary use prohibition audit log",
            "requirement": (
                "Payer MUST maintain an audit log of all Provider Access API queries for 7 years. "
                "Log must include: requesting provider NPI, patient ID queried, timestamp, purpose "
                "of use code, and data types returned. CMS may request this log during compliance "
                "review. Payer must be able to produce the log within 30 days of CMS request."
            ),
            "cfr": "45 CFR 156.122(b)(3) / HIPAA audit requirements",
            "frequency": "Ongoing (7-year retention); producible on CMS request",
            "submitted_to": "CMS on request during compliance review",
        },
    ],
    "prior-auth": [
        {
            "title": "Prior Authorization Transparency Report — annual public posting",
            "requirement": (
                "Impacted payers MUST publicly post an annual Prior Authorization Transparency "
                "Report on their website covering the previous calendar year. Required data elements:\n"
                "  - Total PA requests received (by service type)\n"
                "  - Total approved (number and percentage)\n"
                "  - Total denied (number and percentage, by denial reason category)\n"
                "  - Total pended/pending (average days to decision)\n"
                "  - Total withdrawn\n"
                "  - Average time from submission to decision: standard vs. urgent\n"
                "  - Top 10 most common denial reasons (by service type)\n"
                "  - Number of denials overturned on appeal\n"
                "Report must be posted by March 31 of each year for the prior calendar year."
            ),
            "cfr": "45 CFR 156.122(c)(2) — effective for plan years beginning Jan 2026",
            "frequency": "Annual (by March 31)",
            "submitted_to": "Public website (required) + CMS HPMS submission",
        },
        {
            "title": "PA API Operational Attestation",
            "requirement": (
                "Payers MUST annually attest that the Prior Authorization API is operational "
                "and includes the full CRD/DTR/PAS workflow. Attestation must confirm: "
                "(1) CRD hooks are live in at least one EHR integration, "
                "(2) DTR questionnaires are available for all PA-required service types, "
                "(3) PAS $submit and $inquire endpoints are live, "
                "(4) Subscription notifications for pended PA are implemented."
            ),
            "cfr": "45 CFR 156.122(c) / CMS HPMS annual certification",
            "frequency": "Annual",
            "submitted_to": "CMS via HPMS (MA) / state Medicaid agency / Healthcare.gov",
        },
        {
            "title": "PA Services List — public posting and API exposure",
            "requirement": (
                "Payers MUST maintain and publicly post a current list of all services and items "
                "requiring prior authorization. The list MUST also be available via FHIR API as a "
                "ValueSet or CoverageEligibilityResponse resource. List MUST be updated within "
                "7 calendar days of any change. CMS will compare submitted PA requests against "
                "this list to identify unauthorized PA requirements."
            ),
            "cfr": "45 CFR 156.122(c)(1)(i)",
            "frequency": "Ongoing (updated within 7 days of change); annual audit by CMS",
            "submitted_to": "Public website + FHIR API + CMS HPMS",
        },
        {
            "title": "PA Denial Reason Codes — standardized reporting",
            "requirement": (
                "All PA denial reasons reported in the annual Transparency Report MUST use "
                "standardized CMS denial reason codes. Payer-specific codes must be mapped to "
                "CMS standard codes. The mapping table must be submitted to CMS annually. "
                "FHIR ClaimResponse.item.adjudication.reason MUST use codes from the "
                "X12 278 denial reason code set or CMS-defined ValueSet."
            ),
            "cfr": "45 CFR 156.122(c)(2)(iv)",
            "frequency": "Annual mapping table submission",
            "submitted_to": "CMS HPMS + embedded in FHIR ClaimResponse",
        },
    ],
    "p2p": [
        {
            "title": "P2P Exchange Volume Reporting",
            "requirement": (
                "Payers MUST track and report annually to CMS the following P2P exchange metrics:\n"
                "  - Total $member-match requests initiated (as new payer)\n"
                "  - Total $member-match requests received and responded to (as prior payer)\n"
                "  - Response rate (% responded within 1 business day SLA)\n"
                "  - Number of members who opted out of P2P exchange\n"
                "  - Number of failed exchanges (with reason codes)\n"
                "  - Total data volume exchanged (GB or record counts by resource type)\n"
                "Report submitted via CMS HPMS by March 31 for the prior calendar year."
            ),
            "cfr": "45 CFR 156.122(d) / CMS HPMS reporting",
            "frequency": "Annual (by March 31)",
            "submitted_to": "CMS via HPMS",
        },
        {
            "title": "Opt-Out Registry Reporting",
            "requirement": (
                "Payers MUST maintain a registry of all members who have opted out of P2P data "
                "exchange. The registry must be auditable by CMS on request. Annual report must "
                "include: total opt-out count, demographic breakdown (age, plan type), and "
                "re-enrollment opt-out persistence rate. Opt-out data MUST NOT be shared with "
                "other payers."
            ),
            "cfr": "45 CFR 156.122(d)(4)",
            "frequency": "Annual summary; registry producible on CMS request",
            "submitted_to": "CMS on request; annual count in HPMS report",
        },
        {
            "title": "P2P API Operational Attestation",
            "requirement": (
                "Payers MUST annually attest that P2P exchange capabilities are live and "
                "compliant. Attestation must confirm: "
                "(1) $member-match endpoint is operational, "
                "(2) bulk $export is available for matched members, "
                "(3) UDAP B2B authentication is implemented, "
                "(4) quarterly refresh is scheduled for ongoing members (Jan 2027+), "
                "(5) opt-out mechanism is in place."
            ),
            "cfr": "45 CFR 156.122(d) / CMS HPMS annual certification",
            "frequency": "Annual",
            "submitted_to": "CMS via HPMS (MA) / state Medicaid agency",
        },
    ],
    "provider-directory": [
        {
            "title": "Directory Accuracy Attestation",
            "requirement": (
                "Payers MUST annually attest to CMS that their provider directory is accurate "
                "and updated within the 90-business-day SLA. Attestation must include: "
                "(1) total provider records in directory, "
                "(2) number of updates processed in prior year, "
                "(3) average days to update after change notification, "
                "(4) number of records exceeding 90-business-day update SLA and reason."
            ),
            "cfr": "45 CFR 156.122(e)(1) / CMS HPMS annual certification",
            "frequency": "Annual",
            "submitted_to": "CMS via HPMS",
        },
        {
            "title": "Provider Directory Accuracy Audit — CMS spot checks",
            "requirement": (
                "CMS conducts quarterly spot-check audits of provider directory accuracy by "
                "comparing directory data against claims data and provider NPI registry. "
                "Payer must be able to respond to CMS data requests within 15 business days. "
                "Discrepancy rate > 5% between directory and claims triggers a formal "
                "compliance review. Payer must have a documented discrepancy resolution process."
            ),
            "cfr": "45 CFR 156.122(e) / CMS oversight authority",
            "frequency": "Quarterly (CMS-initiated); payer must respond within 15 days",
            "submitted_to": "CMS on request",
        },
    ],
}


# ---------------------------------------------------------------------------
# FHIR Package Downloader / Parser
# ---------------------------------------------------------------------------

def _download_package(pkg_id: str, version: str) -> dict[str, Any]:
    """Download and parse a FHIR package, returning resource dict keyed by filename."""
    cache_path = PKG_CACHE_DIR / f"{pkg_id}-{version}.tgz"

    if not cache_path.exists():
        url = f"{FHIR_PKG_BASE}/{pkg_id}/{version}"
        print(f"  Downloading {pkg_id}@{version} ...", end="", flush=True)
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/tar+gzip"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            cache_path.write_bytes(data)
            print(f" {len(data)//1024}KB cached")
        except Exception as exc:
            print(f" FAILED: {exc}")
            return {}
    else:
        print(f"  {pkg_id}@{version} — using cache")

    resources: dict[str, Any] = {}
    try:
        with tarfile.open(cache_path, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.name.endswith(".json"):
                    continue
                name = Path(member.name).name
                if name.startswith("package."):
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                try:
                    data = json.loads(f.read())
                    resources[name] = data
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
    except Exception as exc:
        print(f"  WARNING: could not parse {cache_path}: {exc}")
    return resources


def _extract_capability_statements(resources: dict) -> list[dict]:
    return [r for r in resources.values() if r.get("resourceType") == "CapabilityStatement"]


def _extract_structure_definitions(resources: dict) -> list[dict]:
    return [r for r in resources.values() if r.get("resourceType") == "StructureDefinition"]


def _extract_value_sets(resources: dict) -> list[dict]:
    return [r for r in resources.values() if r.get("resourceType") == "ValueSet"]


def _parse_cs_requirements(cs: dict, pkg_id: str, api_slug: str) -> list[dict]:
    """Parse a CapabilityStatement and extract SHALL/SHOULD/MAY requirements as memory entries."""
    entries = []
    cs_id = cs.get("id", "unknown")
    cs_title = cs.get("title") or cs.get("name") or cs_id
    cs_desc = (cs.get("description") or "")[:300]
    cs_kind = cs.get("kind", "")  # requirements | capability
    cs_status = cs.get("status", "")

    for rest in cs.get("rest", []):
        mode = rest.get("mode", "")   # server | client
        for resource_entry in rest.get("resource", []):
            rtype = resource_entry.get("type", "")
            profile = resource_entry.get("profile", "")
            conformance = resource_entry.get("conformance", "SHALL")  # default

            # Supported profiles
            supported_profiles = resource_entry.get("supportedProfile", [])

            # Operations for this resource
            for interaction in resource_entry.get("interaction", []):
                icode = interaction.get("code", "")        # read, search-type, create...
                iext = interaction.get("extension", [])
                # Look for expectation extension
                expectation = "SHALL"
                for ext in iext:
                    if "conformance-expectation" in ext.get("url", ""):
                        expectation = ext.get("valueCode", "SHALL")
                        break

                entries.append({
                    "type": "operation",
                    "cs_id": cs_id,
                    "cs_title": cs_title,
                    "mode": mode,
                    "resource": rtype,
                    "interaction": icode,
                    "expectation": expectation,
                    "profile": profile,
                    "pkg_id": pkg_id,
                    "api_slug": api_slug,
                })

            # Search parameters
            for sp in resource_entry.get("searchParam", []):
                sp_name = sp.get("name", "")
                sp_type = sp.get("type", "")
                sp_ext = sp.get("extension", [])
                expectation = "SHALL"
                for ext in sp_ext:
                    if "conformance-expectation" in ext.get("url", ""):
                        expectation = ext.get("valueCode", "SHALL")
                        break

                entries.append({
                    "type": "search-param",
                    "cs_id": cs_id,
                    "cs_title": cs_title,
                    "mode": mode,
                    "resource": rtype,
                    "param_name": sp_name,
                    "param_type": sp_type,
                    "expectation": expectation,
                    "pkg_id": pkg_id,
                    "api_slug": api_slug,
                })

        # Global operations
        for op in rest.get("operation", []):
            op_name = op.get("name", "")
            op_def = op.get("definition", "")
            entries.append({
                "type": "global-operation",
                "cs_id": cs_id,
                "cs_title": cs_title,
                "mode": mode,
                "operation": op_name,
                "definition": op_def,
                "expectation": "SHALL",
                "pkg_id": pkg_id,
                "api_slug": api_slug,
            })

    return entries


def _profile_summary(sd: dict, pkg_id: str, api_slug: str) -> dict | None:
    """Summarize a StructureDefinition profile into a memory entry."""
    if sd.get("kind") not in ("resource", "complex-type"):
        return None
    if sd.get("abstract", False):
        return None

    sd_id = sd.get("id", "")
    sd_title = sd.get("title") or sd.get("name") or sd_id
    sd_type = sd.get("type", "")
    sd_url = sd.get("url", "")
    sd_desc = (sd.get("description") or "")[:400]
    sd_status = sd.get("status", "")
    sd_publisher = sd.get("publisher", "")

    # Count must-support elements
    must_support_elements: list[str] = []
    for el in (sd.get("snapshot") or {}).get("element", []):
        if el.get("mustSupport"):
            must_support_elements.append(el.get("id", el.get("path", "")))

    return {
        "type": "profile",
        "sd_id": sd_id,
        "sd_title": sd_title,
        "fhir_type": sd_type,
        "url": sd_url,
        "description": sd_desc,
        "status": sd_status,
        "must_support_count": len(must_support_elements),
        "must_support_elements": must_support_elements[:20],  # top 20
        "pkg_id": pkg_id,
        "api_slug": api_slug,
    }


# ---------------------------------------------------------------------------
# Engram Writer
# ---------------------------------------------------------------------------

def _write_to_engram(content: str, namespace: str, memory_type: str,
                      tags: list[str], affects: list[str], dry_run: bool) -> str | None:
    if dry_run:
        print(f"    [DRY] {memory_type} | {tags[:3]} | {content[:120]}")
        return None

    payload = json.dumps({
        "content": content,
        "namespace": namespace,
        "memory_type": memory_type,
        "tags": tags,
        "affects": affects,
    }).encode()

    req = urllib.request.Request(
        f"{ENGRAM_API}/api/v1/memory/",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ENGRAM_KEY,
            "X-Engram-Tool": "cms57f-kg-builder",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            return body.get("id")
    except Exception as exc:
        print(f"    WARNING: write failed: {exc}")
        return None


def _search_and_delete_existing(namespace: str, api_slug: str, dry_run: bool) -> int:
    """Delete existing cms57f memories for this api to avoid duplicates."""
    if dry_run:
        return 0
    url = f"{ENGRAM_API}/api/v1/memory/search?q={api_slug}+cms57f&ns={namespace}&top_k=200"
    req = urllib.request.Request(url, headers={"X-API-Key": ENGRAM_KEY})
    deleted = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            results = json.loads(resp.read())
        for m in results:
            mid = m.get("id")
            if not mid:
                continue
            del_req = urllib.request.Request(
                f"{ENGRAM_API}/api/v1/memory/{mid}?ns={namespace}",
                method="DELETE",
                headers={"X-API-Key": ENGRAM_KEY},
            )
            try:
                urllib.request.urlopen(del_req, timeout=10)
                deleted += 1
            except Exception:
                pass
    except Exception:
        pass
    return deleted


# ---------------------------------------------------------------------------
# Seed Functions
# ---------------------------------------------------------------------------

def seed_regulatory_node(api: ApiDef, ns: str, dry_run: bool) -> None:
    """Write the top-level regulatory requirement node for each API."""
    deadline_str = datetime.fromisoformat(api.deadline).strftime("%B %d, %Y")
    content = (
        f"CMS-0057-F REGULATORY REQUIREMENT: {api.name} ({api.short})\n\n"
        f"Citation: {api.cfr} — {api.cms_section}\n"
        f"Compliance deadline: {deadline_str}\n"
        f"Applicable entity: Impacted Payers (MA, Medicaid managed care, CHIP, QHP issuers on FFE)\n\n"
        f"Description: {api.description}\n\n"
        f"Implementation guides:\n"
        + "\n".join(f"  - {ig} @ {api.ig_versions.get(ig,'latest')}" for ig in api.igs) + "\n\n"
        f"Required FHIR resources: {', '.join(api.key_resources[:12])}\n\n"
        f"Required operations: {chr(10).join('  - ' + op for op in api.required_operations)}\n\n"
        f"Notes: {api.notes}"
    )
    tags = ["cms57f", "regulatory-requirement", api.slug] + api.tags + (
        ["jan-2027"] if "2027" in api.deadline else ["jan-2026"]
    )
    _write_to_engram(
        content=content,
        namespace=ns,
        memory_type="constraint",
        tags=list(set(tags)),
        affects=[api.slug, "cms-0057-f"],
        dry_run=dry_run,
    )


def seed_ig_node(api: ApiDef, pkg_id: str, ns: str, dry_run: bool) -> None:
    """Write an IG reference node linking the package to the API."""
    version = api.ig_versions.get(pkg_id, "latest")
    content = (
        f"Da Vinci IG: {pkg_id} @ {version}\n\n"
        f"Implements: {api.name} ({api.cfr})\n"
        f"FHIR version: R4 (4.0.1)\n"
        f"Publisher: HL7 International / Da Vinci Project\n"
        f"Download: https://packages.fhir.org/{pkg_id}/{version}\n"
        f"HL7 page: https://hl7.org/fhir/us/{pkg_id.split('.')[-1]}/\n\n"
        f"Role in {api.short}: This IG defines the FHIR profiles, operations, "
        f"and conformance requirements that payers must implement to comply with "
        f"{api.cms_section} of CMS-0057-F."
    )
    tags = ["cms57f", "da-vinci", "fhir-ig", api.slug, pkg_id.split(".")[-1]] + api.tags
    _write_to_engram(
        content=content,
        namespace=ns,
        memory_type="fact",
        tags=list(set(tags)),
        affects=[api.slug, pkg_id],
        dry_run=dry_run,
    )


def seed_cs_requirements(api: ApiDef, pkg_id: str, cs: dict, ns: str, dry_run: bool) -> int:
    """Write requirement memories from a CapabilityStatement."""
    requirements = _parse_cs_requirements(cs, pkg_id, api.slug)
    if not requirements:
        return 0

    cs_title = cs.get("title") or cs.get("name") or cs.get("id", "unknown")
    count = 0

    # Group by resource for better readability
    by_resource: dict[str, list[dict]] = {}
    for req in requirements:
        rtype = req.get("resource") or req.get("operation", "global")
        by_resource.setdefault(rtype, []).append(req)

    for rtype, reqs in by_resource.items():
        ops = [r for r in reqs if r["type"] == "operation"]
        sps = [r for r in reqs if r["type"] == "search-param"]
        global_ops = [r for r in reqs if r["type"] == "global-operation"]

        if not (ops or sps or global_ops):
            continue

        lines = [
            f"FHIR Conformance Requirements — {rtype} ({cs_title})",
            f"Source: {pkg_id} CapabilityStatement/{cs.get('id','?')}",
            f"API: {api.name} | Mode: {reqs[0].get('mode','server')}",
            "",
        ]

        if ops:
            lines.append("Interactions:")
            for op in ops:
                lines.append(
                    f"  {op['expectation']:6} {op['interaction']}"
                    + (f" [profile: {op['profile'].split('/')[-1]}]" if op.get("profile") else "")
                )

        if sps:
            lines.append("\nSearch parameters:")
            for sp in sps[:15]:
                lines.append(f"  {sp['expectation']:6} {sp['param_name']} ({sp['param_type']})")

        if global_ops:
            lines.append("\nOperations:")
            for gop in global_ops:
                lines.append(f"  SHALL  ${gop['operation']}")

        content = "\n".join(lines)
        shall_count = sum(1 for r in reqs if r.get("expectation") == "SHALL")
        should_count = sum(1 for r in reqs if r.get("expectation") == "SHOULD")

        tags = [
            "cms57f", "fhir-conformance", api.slug, rtype.lower(),
            pkg_id.split(".")[-1],
        ]
        if shall_count > 0:
            tags.append("SHALL")
        if should_count > 0:
            tags.append("SHOULD")
        tags += api.tags

        _write_to_engram(
            content=content,
            namespace=ns,
            memory_type="constraint",
            tags=list(set(tags)),
            affects=[api.slug, rtype, pkg_id],
            dry_run=dry_run,
        )
        count += 1
        time.sleep(0.05)  # rate limit

    return count


def seed_profile_node(api: ApiDef, pkg_id: str, sd: dict, ns: str, dry_run: bool) -> None:
    """Write a profile summary memory."""
    summary = _profile_summary(sd, pkg_id, api.slug)
    if not summary:
        return

    content = (
        f"FHIR Profile: {summary['sd_title']} ({summary['fhir_type']})\n\n"
        f"URL: {summary['url']}\n"
        f"Package: {pkg_id}\n"
        f"API: {api.name} ({api.cfr})\n"
        f"Status: {summary['status']}\n\n"
        f"Description: {summary['description']}\n\n"
        f"Must-support elements ({summary['must_support_count']} total):\n"
        + (("\n".join(f"  - {e}" for e in summary["must_support_elements"])) or "  (none documented in snapshot)")
    )

    tags = [
        "cms57f", "fhir-profile", api.slug, summary["fhir_type"].lower(),
        pkg_id.split(".")[-1],
    ] + api.tags

    _write_to_engram(
        content=content,
        namespace=ns,
        memory_type="fact",
        tags=list(set(tags)),
        affects=[api.slug, summary["fhir_type"], pkg_id],
        dry_run=dry_run,
    )


def seed_reporting_requirements(api: ApiDef, ns: str, dry_run: bool) -> int:
    """Write CMS reporting/attestation requirement nodes for an API."""
    items = REPORTING_REQUIREMENTS.get(api.slug, [])
    if not items:
        return 0
    for item in items:
        content = (
            f"CMS-0057-F REPORTING REQUIREMENT: {item['title']}\n\n"
            f"API: {api.name} ({api.cfr})\n"
            f"CFR Reference: {item.get('cfr', api.cfr)}\n"
            f"Frequency: {item['frequency']}\n"
            f"Submitted to: {item['submitted_to']}\n\n"
            f"Requirement:\n{item['requirement']}"
        )
        tags = ["cms57f", "reporting-requirement", "attestation", api.slug] + api.tags
        _write_to_engram(
            content=content,
            namespace=ns,
            memory_type="constraint",
            tags=list(set(tags)),
            affects=[api.slug, "cms-0057-f", "reporting", "attestation"],
            dry_run=dry_run,
        )
        time.sleep(0.05)
    return len(items)


def seed_sla_requirements(api: ApiDef, ns: str, dry_run: bool) -> int:
    """Write SLA/timing requirement nodes for an API."""
    items = SLA_REQUIREMENTS.get(api.slug, [])
    if not items:
        return 0
    for item in items:
        content = (
            f"CMS-0057-F SLA REQUIREMENT: {item['title']}\n\n"
            f"API: {api.name} ({api.cfr})\n"
            f"Severity: {item['severity']}\n"
            f"CFR Reference: {item.get('cfr', api.cfr)}\n\n"
            f"Requirement:\n{item['requirement']}"
        )
        tags = ["cms57f", "sla", "timing-requirement", api.slug, item["severity"]] + api.tags
        _write_to_engram(
            content=content,
            namespace=ns,
            memory_type="constraint",
            tags=list(set(tags)),
            affects=[api.slug, "cms-0057-f", "sla"],
            dry_run=dry_run,
        )
        time.sleep(0.05)
    return len(items)


def seed_security_requirements(api: ApiDef, ns: str, dry_run: bool) -> int:
    """Write security requirement nodes (UDAP, SMART, mTLS) for an API."""
    items = SECURITY_REQUIREMENTS.get(api.slug, [])
    if not items:
        return 0
    for item in items:
        content = (
            f"CMS-0057-F SECURITY REQUIREMENT: {item['title']}\n\n"
            f"API: {api.name} ({api.cfr})\n"
            f"Severity: {item['severity']}\n"
            f"Source IG: {item.get('ig', 'CMS-0057-F rule text')}\n\n"
            f"Requirement:\n{item['requirement']}"
        )
        ig_short = item.get("ig", "").split("@")[0].split(".")[-1] if item.get("ig") else "security"
        tags = ["cms57f", "security-requirement", "udap", "smart-on-fhir",
                api.slug, item["severity"], ig_short] + api.tags
        _write_to_engram(
            content=content,
            namespace=ns,
            memory_type="constraint",
            tags=list(set(tags)),
            affects=[api.slug, "cms-0057-f", "security", "udap"],
            dry_run=dry_run,
        )
        time.sleep(0.05)
    return len(items)


def seed_business_rules(api: ApiDef, ns: str, dry_run: bool) -> int:
    """Write non-FHIR business rule nodes (opt-out, attribution, etc.) for an API."""
    items = BUSINESS_RULES.get(api.slug, [])
    if not items:
        return 0
    for item in items:
        content = (
            f"CMS-0057-F BUSINESS RULE: {item['title']}\n\n"
            f"API: {api.name} ({api.cfr})\n"
            f"Category: {item['category']}\n\n"
            f"Requirement:\n{item['requirement']}"
        )
        tags = ["cms57f", "business-rule", item["category"], api.slug] + api.tags
        _write_to_engram(
            content=content,
            namespace=ns,
            memory_type="constraint",
            tags=list(set(tags)),
            affects=[api.slug, "cms-0057-f", item["category"]],
            dry_run=dry_run,
        )
        time.sleep(0.05)
    return len(items)


def seed_gap_tracker(api: ApiDef, ns: str, dry_run: bool) -> None:
    """Write a gap-tracking memory for each API to track implementation status."""
    deadline_str = datetime.fromisoformat(api.deadline).strftime("%B %d, %Y")
    now = datetime.now(timezone.utc)
    target = datetime.fromisoformat(api.deadline).replace(tzinfo=timezone.utc)
    days_left = (target - now).days

    hdig_module = {
        "patient-access": "hdig-modules/patient-access",
        "provider-access": "hdig-modules/provider-access",
        "prior-auth": "hdig-modules/prior-auth",
        "p2p": "hdig-modules/p2p",
        "provider-directory": "hdig-modules/provider-directory",
    }.get(api.slug, "unknown")

    status_note = (
        "PAST DUE (deadline was Jan 2026)" if days_left < 0
        else f"{days_left} days until compliance deadline"
    )

    content = (
        f"CMS-0057-F GAP TRACKER: {api.name}\n\n"
        f"API: {api.slug} | CFR: {api.cfr}\n"
        f"Deadline: {deadline_str} ({status_note})\n"
        f"HDIG module: {hdig_module}\n\n"
        f"Implementation status: [TO BE UPDATED BY AGENTS]\n\n"
        f"Required capabilities ({len(api.required_operations)}):\n"
        + "\n".join(f"  [ ] {op}" for op in api.required_operations) + "\n\n"
        f"FHIR resources required ({len(api.key_resources)}):\n"
        + "\n".join(f"  [ ] {r}" for r in api.key_resources) + "\n\n"
        f"Implementation guides:\n"
        + "\n".join(f"  [ ] {ig} implemented" for ig in api.igs) + "\n\n"
        "Instructions for agents: Update this memory as implementation progresses. "
        "Mark items [x] when done. Add commit hash and date when each capability ships."
    )

    tags = [
        "cms57f", "gap-tracker", api.slug, "compliance-tracking",
    ] + (["jan-2027"] if "2027" in api.deadline else ["jan-2026"])

    _write_to_engram(
        content=content,
        namespace=ns,
        memory_type="decision",
        tags=list(set(tags)),
        affects=[api.slug, "cms-0057-f", hdig_module],
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_api(api: ApiDef, ns: str, dry_run: bool) -> None:
    print(f"\n{'='*60}")
    print(f"Processing: {api.name} ({api.cfr})")
    print(f"Deadline:   {api.deadline}  |  IGs: {len(api.igs)}")
    print(f"{'='*60}")

    # 1. Regulatory overview node
    print("\n[1] Seeding regulatory requirement node...")
    seed_regulatory_node(api, ns, dry_run)

    # 2. Gap tracker
    print("[2] Seeding gap tracker...")
    seed_gap_tracker(api, ns, dry_run)

    # 3. Reporting / attestation requirements
    print("[3] Seeding reporting/attestation requirements...")
    n_rep = seed_reporting_requirements(api, ns, dry_run)
    print(f"    Wrote {n_rep} reporting requirement nodes")

    # 4. SLA / timing requirements
    print("[4] Seeding SLA/timing requirements...")
    n_sla = seed_sla_requirements(api, ns, dry_run)
    print(f"    Wrote {n_sla} SLA requirement nodes")

    # 5. Security requirements
    print("[5] Seeding security requirements (UDAP/SMART)...")
    n_sec = seed_security_requirements(api, ns, dry_run)
    print(f"    Wrote {n_sec} security requirement nodes")

    # 6. Business rules
    print("[6] Seeding business rules...")
    n_biz = seed_business_rules(api, ns, dry_run)
    print(f"    Wrote {n_biz} business rule nodes")

    # 8. Process each IG
    total_cs_nodes = 0
    total_profile_nodes = 0

    for pkg_id in api.igs:
        version = api.ig_versions.get(pkg_id, "latest")
        print(f"\n[IG] {pkg_id}@{version}")

        # Write IG reference node
        seed_ig_node(api, pkg_id, ns, dry_run)

        # Download and parse package
        resources = _download_package(pkg_id, version)
        if not resources:
            print(f"  WARNING: no resources extracted from {pkg_id}")
            continue

        cs_list = _extract_capability_statements(resources)
        sd_list = _extract_structure_definitions(resources)
        print(f"  Found: {len(cs_list)} CapabilityStatements, {len(sd_list)} StructureDefinitions")

        # 3a. CapabilityStatements → conformance requirements
        for cs in cs_list:
            cs_id = cs.get("id", "?")
            cs_kind = cs.get("kind", "?")
            # Only process conformance/requirements CSes
            if cs_kind not in ("requirements", "capability"):
                continue
            n = seed_cs_requirements(api, pkg_id, cs, ns, dry_run)
            if n:
                print(f"  CS/{cs_id}: wrote {n} resource-requirement nodes")
                total_cs_nodes += n

        # 3b. StructureDefinitions → profile nodes (only key profiles, not all)
        profiles_written = 0
        for sd in sd_list:
            if sd.get("derivation") != "constraint":
                continue  # skip base types
            if sd.get("abstract", False):
                continue
            fhir_type = sd.get("type", "")
            # Only write profiles for resources we care about
            if fhir_type in api.key_resources:
                seed_profile_node(api, pkg_id, sd, ns, dry_run)
                profiles_written += 1
                time.sleep(0.05)
        if profiles_written:
            print(f"  Wrote {profiles_written} profile nodes")
        total_profile_nodes += profiles_written

    print(f"\n  ✓ {api.name}: {total_cs_nodes} conformance + {total_profile_nodes} profile "
          f"+ {n_rep} reporting + {n_sla} SLA + {n_sec} security + {n_biz} business-rule nodes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CMS-0057-F knowledge graph in engram")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    parser.add_argument("--api", help="Process a specific API slug only")
    parser.add_argument("--reset", action="store_true", help="Delete existing entries first")
    parser.add_argument("--ns", default=DEFAULT_NS, help="Engram namespace")
    parser.add_argument("--list", action="store_true", help="List available API slugs and exit")
    parser.add_argument("--reporting-only", action="store_true",
                        help="Only seed reporting/attestation nodes (skips IG download)")
    args = parser.parse_args()

    if args.list:
        print("Available API slugs:")
        for slug, api in API_REGISTRY.items():
            print(f"  {slug:25} {api.name}  (deadline: {api.deadline})")
        return

    print(f"CMS-0057-F Knowledge Graph Builder")
    print(f"Namespace : {args.ns}")
    print(f"Dry run   : {args.dry_run}")
    print(f"APIs      : {args.api or 'all'}")
    print(f"Reset     : {args.reset}")

    # Verify engram is reachable
    if not args.dry_run:
        try:
            req = urllib.request.Request(
                f"{ENGRAM_API}/api/v1/admin/health",
                headers={"X-API-Key": ENGRAM_KEY},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read())
            print(f"Engram    : {health.get('status')} (arcadedb: {health.get('arcadedb')})")
        except Exception as exc:
            print(f"ERROR: cannot reach engram at {ENGRAM_API}: {exc}", file=sys.stderr)
            sys.exit(1)

    apis_to_process = (
        [API_REGISTRY[args.api]]
        if args.api and args.api in API_REGISTRY
        else list(API_REGISTRY.values())
    )

    total_start = time.monotonic()
    total_apis = 0

    for api in apis_to_process:
        if args.reset and not args.dry_run:
            deleted = _search_and_delete_existing(args.ns, api.slug, args.dry_run)
            if deleted:
                print(f"  Reset: deleted {deleted} existing entries for {api.slug}")

        if args.reporting_only:
            print(f"\n{'='*60}")
            print(f"Processing (reporting only): {api.name}")
            print(f"{'='*60}")
            n = seed_reporting_requirements(api, args.ns, args.dry_run)
            print(f"  ✓ {api.name}: {n} reporting requirement nodes")
        else:
            process_api(api, args.ns, args.dry_run)
        total_apis += 1

    elapsed = time.monotonic() - total_start
    print(f"\n{'='*60}")
    print(f"Done. Processed {total_apis} APIs in {elapsed:.1f}s")
    print(f"Namespace: {args.ns}")
    if not args.dry_run:
        print(f"Query with: memory_search('cms57f prior auth requirements')")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
