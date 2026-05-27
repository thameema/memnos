#!/usr/bin/env python3
"""
tools/build_cms57f_kg.py — CMS-0057-F knowledge graph builder.

Downloads Da Vinci FHIR Implementation Guide packages, extracts
CapabilityStatements and profiles, and seeds engram with structured
requirement and gap-tracking memories.

Usage:
    python tools/build_cms57f_kg.py [--dry-run] [--api SLUG] [--reset]

Options:
    --dry-run     Print what would be written without writing to engram
    --api SLUG    Only process a specific API (patient-access, provider-access,
                  prior-auth, p2p, provider-directory)
    --reset       Delete existing cms57f memories before rebuilding
    --ns NS       Engram namespace (default: org:hc:cms57f)
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
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pdex": "2.1.0",
            "hl7.fhir.us.core": "6.1.0",
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
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pdex": "2.1.0",
            "hl7.fhir.us.core": "6.1.0",
            "hl7.fhir.us.davinci-hrex": "1.2.0",
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
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pas": "2.2.1",
            "hl7.fhir.us.davinci-crd": "2.2.1",
            "hl7.fhir.us.davinci-dtr": "2.2.0",
            "hl7.fhir.us.davinci-cdex": "2.1.0",
            "hl7.fhir.us.davinci-hrex": "1.2.0",
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
        ],
        ig_versions={
            "hl7.fhir.us.davinci-pdex": "2.1.0",
            "hl7.fhir.us.davinci-hrex": "1.2.0",
            "hl7.fhir.us.core": "6.1.0",
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

    # 3. Process each IG
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

    print(f"\n  ✓ {api.name}: {total_cs_nodes} conformance nodes + {total_profile_nodes} profile nodes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CMS-0057-F knowledge graph in engram")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    parser.add_argument("--api", help="Process a specific API slug only")
    parser.add_argument("--reset", action="store_true", help="Delete existing entries first")
    parser.add_argument("--ns", default=DEFAULT_NS, help="Engram namespace")
    parser.add_argument("--list", action="store_true", help="List available API slugs and exit")
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
