"""Tommy structured handoffs (issue #193): tommy_handoff / tommy_handoff_transition /
tommy_handoff_status.

Real memnos (via the `live_memnos` fixture in conftest.py), no mocking of the DB layer
or the memnos client — the whole point of this feature is that a handoff record is
durable, auditable memnos state, not an in-memory data structure, so the test proves
exactly that: a handoff written via one MemnosClient instance is readable, with its full
history, via a completely separate call.

Covers:
  - the full state machine: offered -> accepted -> in_progress -> completed, each hop
    incrementing, each transition's full history visible via tommy_handoff_status
  - escalated as a valid exit from any non-terminal state
  - invalid transitions rejected (skipping a state, acting on a terminal handoff)
  - the hop cap (TOMMY_CFG_HANDOFF_HOP_LIMIT): enforced across re-offers for the SAME
    task_id (not just within one handoff_id's own chain), and does NOT bleed across
    different task_ids
  - extract=False means these writes are fast/deterministic/no-LLM — proven indirectly
    by this suite needing no LLM/extraction backend configured at all to pass
  - type='handoff' writes are not force-pinned into unrelated recalls (unlike
    type='constraint') — proven directly against the live server

Run: MEMNOS_DSN=... MEMNOS_URL=... python -m pytest agents/tommy/tests/test_handoff.py -q
(uses the live_memnos fixture from conftest.py — see that file's module docstring for
how to stand up a throwaway server to run this against)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig


@pytest.fixture
def handoff_namespace(live_memnos):
    """A real, isolated namespace + token, granted write access — same shape as the
    secret_shield fixtures' own namespace setup, kept local to this file since nothing
    else needs it."""
    import core.control as control_mod

    conn = live_memnos["conn"]
    run = f"{live_memnos['run']}_{int(time.time() * 1000)}"
    ns = f"test:tommy_handoff_{run}"
    pid = live_memnos["make_principal"]("agent")
    control_mod.Control.create_namespace(conn, ns, created_by=pid)
    control_mod.Control.grant(conn, pid, ns)
    tok = control_mod.Control.mint_token(conn, pid, "test-handoff")
    yield {"namespace": ns, "token": tok, "url": live_memnos["url"]}
    live_memnos["cleanup_namespace"](ns)
    control_mod.Control.delete_namespace(conn, ns, purge_data=True)


@pytest.fixture
def cfg(live_memnos, handoff_namespace):
    return TommyConfig(
        memnos_url=live_memnos["url"],
        memnos_token=handoff_namespace["token"],
        default_ns=handoff_namespace["namespace"],
    )


@pytest.fixture(autouse=True)
def _inject_cfg(cfg, monkeypatch):
    monkeypatch.setattr(mcp_server_mod, "_cfg", cfg, raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)


def test_full_lifecycle_offered_to_completed():
    task_id = "task-full-lifecycle"
    r1 = mcp_server_mod.tommy_handoff(
        task_id=task_id, to_harness="codex",
        context_summary="refactor the auth module to use PKCE",
        expected_deliverable="PR with PKCE flow + tests")
    assert r1["ok"] is True
    assert r1["state"] == "offered"
    assert r1["hop"] == 1
    assert "context_bundle" in r1    # present even if empty — never silently dropped
    handoff_id = r1["handoff_id"]

    status = mcp_server_mod.tommy_handoff_status(handoff_id)
    assert status["state"] == "offered"
    assert status["hop"] == 1
    assert status["is_terminal"] is False
    assert len(status["history"]) == 1

    r2 = mcp_server_mod.tommy_handoff_transition(handoff_id, "accepted")
    assert r2["ok"] is True
    assert r2["state"] == "accepted"
    assert r2["hop"] == 2

    r3 = mcp_server_mod.tommy_handoff_transition(handoff_id, "in_progress", note="working on it")
    assert r3["ok"] is True
    assert r3["hop"] == 3

    r4 = mcp_server_mod.tommy_handoff_transition(handoff_id, "completed")
    assert r4["ok"] is True
    assert r4["state"] == "completed"
    assert r4["hop"] == 4

    final = mcp_server_mod.tommy_handoff_status(handoff_id)
    assert final["state"] == "completed"
    assert final["is_terminal"] is True
    assert len(final["history"]) == 4
    assert [h["state"] for h in final["history"]] == ["offered", "accepted", "in_progress", "completed"]
    assert final["history"][2]["note"] == "working on it"


def test_escalated_is_a_valid_exit_from_any_non_terminal_state():
    r1 = mcp_server_mod.tommy_handoff(task_id="task-escalate", to_harness="goose",
                                      context_summary="investigate the flaky test")
    handoff_id = r1["handoff_id"]
    r2 = mcp_server_mod.tommy_handoff_transition(handoff_id, "escalated", note="needs a human")
    assert r2["ok"] is True
    assert r2["state"] == "escalated"

    status = mcp_server_mod.tommy_handoff_status(handoff_id)
    assert status["is_terminal"] is True


def test_rejects_skipping_a_state():
    r1 = mcp_server_mod.tommy_handoff(task_id="task-skip", to_harness="cursor-agent",
                                      context_summary="x")
    handoff_id = r1["handoff_id"]
    r2 = mcp_server_mod.tommy_handoff_transition(handoff_id, "completed")  # offered -> completed, invalid
    assert r2["ok"] is False
    assert "invalid transition" in r2["error"]

    # the rejected attempt must not have moved the state forward
    status = mcp_server_mod.tommy_handoff_status(handoff_id)
    assert status["state"] == "offered"


def test_rejects_transition_on_a_terminal_handoff():
    r1 = mcp_server_mod.tommy_handoff(task_id="task-terminal", to_harness="aider",
                                      context_summary="x")
    handoff_id = r1["handoff_id"]
    mcp_server_mod.tommy_handoff_transition(handoff_id, "accepted")
    mcp_server_mod.tommy_handoff_transition(handoff_id, "in_progress")
    mcp_server_mod.tommy_handoff_transition(handoff_id, "completed")

    r = mcp_server_mod.tommy_handoff_transition(handoff_id, "accepted")
    assert r["ok"] is False
    assert "terminal" in r["error"]


def test_rejects_an_unknown_new_state_value():
    r1 = mcp_server_mod.tommy_handoff(task_id="task-badstate", to_harness="claude",
                                      context_summary="x")
    r = mcp_server_mod.tommy_handoff_transition(r1["handoff_id"], "not_a_real_state")
    assert r["ok"] is False
    assert "invalid new_state" in r["error"]


def test_unknown_handoff_id_is_a_clear_error():
    r = mcp_server_mod.tommy_handoff_status("nonexistent12")
    assert "error" in r
    r2 = mcp_server_mod.tommy_handoff_transition("nonexistent12", "accepted")
    assert r2["ok"] is False


def test_hop_cap_enforced_across_reoffers_for_the_same_task_id(monkeypatch, tmp_path):
    """The cap must see across DIFFERENT handoff_ids for the same task_id (a re-offer
    after an escalation mints a new handoff_id) — not just within one handoff_id's own
    chain. No tommy.yaml in tmp_path, so this exercises the env-var override layer of
    effective_config, same convention test_effective_config.py already uses."""
    monkeypatch.setenv("TOMMY_CFG_HANDOFF_HOP_LIMIT", "2")
    monkeypatch.chdir(tmp_path)
    task_id = "task-hopcapped"

    r1 = mcp_server_mod.tommy_handoff(task_id=task_id, to_harness="codex", context_summary="x")
    assert r1["ok"] is True
    assert r1["hop"] == 1
    assert r1["hop_limit"] == 2

    r2 = mcp_server_mod.tommy_handoff_transition(r1["handoff_id"], "escalated")
    assert r2["ok"] is True
    assert r2["hop"] == 2    # hits the cap exactly

    # a third hop for the SAME task_id — a re-offer via a brand new handoff_id — must
    # be refused: hop 2 already consumed the whole cap of 2.
    r3 = mcp_server_mod.tommy_handoff(task_id=task_id, to_harness="goose", context_summary="retry")
    assert r3["ok"] is False
    assert r3["reason"] == "hop_limit_exceeded"
    assert r3["hop_count"] == 2
    assert r3["hop_limit"] == 2


def test_hop_cap_does_not_bleed_across_different_task_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("TOMMY_CFG_HANDOFF_HOP_LIMIT", "1")
    monkeypatch.chdir(tmp_path)

    r1 = mcp_server_mod.tommy_handoff(task_id="task-a-capped", to_harness="codex", context_summary="x")
    assert r1["ok"] is True
    r2 = mcp_server_mod.tommy_handoff(task_id="task-a-capped", to_harness="codex", context_summary="x")
    assert r2["ok"] is False   # task-a-capped already used its 1 hop

    # a DIFFERENT task_id must start with a fresh counter, not inherit task-a-capped's
    r3 = mcp_server_mod.tommy_handoff(task_id="task-b-fresh", to_harness="codex", context_summary="x")
    assert r3["ok"] is True
    assert r3["hop"] == 1


def test_handoff_type_is_not_force_pinned_into_unrelated_recall(handoff_namespace):
    """type='handoff' must behave like 'fact', not like 'constraint' — it should never
    get force-pinned into a recall that has nothing to do with it. Checked directly
    against the live server's /recall, not just through Tommy's own query helper."""
    from memnos_sdk import MemnosClient

    mcp_server_mod.tommy_handoff(task_id="task-pin-check", to_harness="codex",
                                 context_summary="x")
    client = MemnosClient(base_url=handoff_namespace["url"],
                          token=handoff_namespace["token"], namespace=handoff_namespace["namespace"])
    result = client.recall("something completely unrelated to any handoff")
    mems = result.get("memories", [])
    assert not any(m.get("pinned") and m.get("type") == "handoff" for m in mems)
