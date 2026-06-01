"""
Shared fixtures for the enterprise-feature regression suite.

These tests hit the running **dev** memnos on localhost:19766. Production
memnos on 8766 is NEVER touched — every test writes into a unique
timestamp+uuid namespace.

Double-pass safety
------------------
Re-running the entire suite back-to-back must remain green. We guarantee
that by:

  * Every namespace is unique per process invocation (timestamp + uuid).
    Two consecutive `pytest` runs never collide because the suffixes differ.
  * No fixture mutates a shared/global namespace.
  * Every assertion is computed against state the test itself just wrote —
    we never assume "DB starts empty" or "there are exactly N memories".

Env vars (optional overrides):
  MEMNOS_DEV_URL       default http://localhost:19766
  MEMNOS_DEV_API_KEY   default read from /tmp/memnos-dev-data/.env
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import httpx
import pytest


def _load_dev_api_key() -> str:
    override = os.environ.get("MEMNOS_DEV_API_KEY")
    if override:
        return override
    env_file = Path("/tmp/memnos-dev-data/.env")
    if not env_file.exists():
        pytest.skip(f"dev .env not found at {env_file}; set MEMNOS_DEV_API_KEY to run")
    for line in env_file.read_text().splitlines():
        if line.startswith("MEMNOS_API_KEY="):
            return line.split("=", 1)[1].strip()
    pytest.skip("MEMNOS_API_KEY not set in dev .env")


DEV_BASE_URL = os.environ.get("MEMNOS_DEV_URL", "http://localhost:19766")


def _new_ns(label: str) -> str:
    return f"regr-{label}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def base_url() -> str:
    return DEV_BASE_URL


@pytest.fixture(scope="session")
def api_key() -> str:
    return _load_dev_api_key()


@pytest.fixture(scope="session")
def http(base_url: str, api_key: str):
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(30.0, connect=5.0),
    ) as client:
        r = client.get("/api/v1/admin/health")
        if r.status_code != 200:
            pytest.skip(
                f"dev memnos not healthy at {base_url} "
                f"(status={r.status_code}). Start dev stack with "
                f"/tmp/memnos-dev-data/dev.sh up -d"
            )
        yield client


# Per-test disposable namespaces — independent across tests AND across runs.
@pytest.fixture
def ns_embed() -> str:
    return _new_ns("embed")


@pytest.fixture
def ns_episode() -> str:
    return _new_ns("episode")


@pytest.fixture
def ns_hybrid() -> str:
    return _new_ns("hybrid")
