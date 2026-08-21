"""
Tests for issue #111's `/sketch`: tommy/sketch.py's `_mermaid_to_cfc()` and
the `tommy_sketch` MCP tool (tommy/mcp_server.py).

Layer 1 (`TestMermaidToCfcHappyPath`, `TestKnownLimitations`,
`TestSyntaxVariance`, `TestEmptyAndEdgeCases`): pure unit tests against
`_mermaid_to_cfc()` — no MCP, no network, no DB. Covers the issue's four
"reasonable v1" transformations (participants -> actors, arrows -> SHALL
statements, alt/else -> conditionals, "must not" notes -> prohibitions) on
flat, representative diagrams, plus at least one case per documented v1
limitation (nested alt/opt/loop, multi-line/wrapped labels, comments and
diagram directives, arrow-token syntax variance) proving each degrades
predictably (skipped + warned) rather than being silently mis-parsed.

`TestExtractedTextIsRealConstraintText` independently re-applies
core/store.py's real RFC-2119 keyword regex (core/store.py:1760-1761) to
whatever `_mermaid_to_cfc()` produces — same technique
test_auto_ingest.py's fixture check uses — so a pass here is also proof the
generated text is genuinely SHALL/MUST-extractable by the real
`ingest_constraints()` extractor, not just plausible-looking prose. Not
imported from core.store: that package isn't a dependency of
tommy-orchestrator and pulls in psycopg et al. — this suite stays on the
fast, DB-free `tommy-tests` CI job (`agents/tommy/tests -q`).

Layer 2 (`TestTommySketchIngestCallShape`): the `tommy_sketch` MCP tool
end-to-end, with `mcp_server._http_corpus_ingest` (the tommy.corpus import)
monkeypatched to a capturing stub rather than a live server — proves
Tommy's own responsibility (namespace/name/text/kind sent, ok/error/
warnings surfaced) exactly the way test_corpus_gate.py and
test_auto_ingest.py already do for the sibling corpus_check/corpus_ingest
call sites, not the server's extraction correctness (tests/test_corpus_api.py,
root suite, already covers that against a real Postgres).
"""
from __future__ import annotations

import re

import pytest

import tommy.config as config_mod
import tommy.mcp_server as mcp_server_mod
from tommy.config import TommyConfig
from tommy.sketch import _mermaid_to_cfc

# Mirrors core/store.py:1760-1761 exactly — used ONLY here as an
# independent self-check that generated CFC text is really extractable,
# never imported from core.store (see module docstring for why).
_REAL_CONSTRAINT_RE = re.compile(
    r"\b(SHALL NOT|MUST NOT|SHOULD NOT|MAY NOT|SHALL|MUST|REQUIRED|SHOULD|PROHIBITED|FORBIDDEN)\b")


FLAT_DIAGRAM = """
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Submit request
    S-->>C: Return response
"""


def _constraint_lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if l and not l.startswith("#")]


# ---------------------------------------------------------------------------
# Layer 1: the four "reasonable v1" transformations, on flat diagrams
# ---------------------------------------------------------------------------


class TestMermaidToCfcHappyPath:
    def test_participants_become_actor_labels_in_statements(self):
        text, warnings = _mermaid_to_cfc(FLAT_DIAGRAM, "checkout-flow")
        assert 'Client SHALL send "Submit request" to Server.' in text
        assert 'Server SHALL send "Return response" to Client.' in text
        assert warnings == []

    def test_undeclared_participant_falls_back_to_raw_id(self):
        # Perfectly legal mermaid: an id used only in an arrow, never
        # declared via `participant`/`actor` — must not error or drop it.
        diagram = "sequenceDiagram\nA->>B: ping\n"
        text, warnings = _mermaid_to_cfc(diagram, "ping-flow")
        assert 'A SHALL send "ping" to B.' in text
        assert warnings == []

    def test_alt_else_become_when_condition_prefixes(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    alt request valid
        S-->>C: 200 OK
    else request invalid
        S-->>C: 400 Bad Request
    end
"""
        text, warnings = _mermaid_to_cfc(diagram, "validate-flow")
        assert 'When request valid, Server SHALL send "200 OK" to Client.' in text
        assert 'When request invalid, Server SHALL send "400 Bad Request" to Client.' in text
        assert warnings == []

    def test_arrows_outside_alt_are_unconditional(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Submit request
    alt ok
        S-->>C: 200 OK
    end
    C->>S: Log completion
"""
        text, warnings = _mermaid_to_cfc(diagram, "mixed-flow")
        assert 'Client SHALL send "Submit request" to Server.' in text
        assert 'Client SHALL send "Log completion" to Server.' in text
        assert 'When ok, Server SHALL send "200 OK" to Client.' in text
        assert warnings == []

    def test_must_not_note_becomes_prohibition(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Submit request
    Note over S: Server must not log the raw request body
"""
        text, warnings = _mermaid_to_cfc(diagram, "log-flow")
        assert "Server must not log the raw request body." in text
        assert warnings == []

    def test_note_without_negation_produces_no_constraint(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Submit request
    Note over S: Just an informational aside
"""
        text, warnings = _mermaid_to_cfc(diagram, "aside-flow")
        assert "informational aside" not in text
        assert len(_constraint_lines(text)) == 1


class TestExtractedTextIsRealConstraintText:
    """core/store.py's real ingest_constraints() extractor, re-applied
    independently, must actually find these as constraints — not just
    "looks like it has the word SHALL in it somewhere"."""

    def test_every_generated_line_matches_the_real_extractor_regex(self):
        text, _ = _mermaid_to_cfc(FLAT_DIAGRAM, "checkout-flow")
        lines = _constraint_lines(text)
        assert lines, "expected at least one constraint line"
        for line in lines:
            assert _REAL_CONSTRAINT_RE.search(line.upper()), f"not extractable: {line!r}"

    def test_prohibition_note_matches_the_real_extractor_regex(self):
        diagram = "sequenceDiagram\nA->>B: go\nNote over A,B: A must not retry more than once\n"
        text, _ = _mermaid_to_cfc(diagram, "retry-flow")
        lines = _constraint_lines(text)
        assert any(_REAL_CONSTRAINT_RE.search(l.upper()) for l in lines)


# ---------------------------------------------------------------------------
# Layer 1: known v1 limitations — predictable degrade, never silent mis-parse
# ---------------------------------------------------------------------------


class TestKnownLimitations:
    def test_nested_alt_is_skipped_with_warning_not_misparsed(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    alt outer condition
        alt inner condition
            S-->>C: nested response
        end
        S-->>C: outer response
    end
"""
        text, warnings = _mermaid_to_cfc(diagram, "nested-flow")
        assert any("nested" in w.lower() and "alt" in w.lower() for w in warnings)
        assert "nested response" not in text
        assert 'When outer condition, Server SHALL send "outer response" to Client.' in text

    def test_second_top_level_alt_after_first_closes_is_not_nested(self):
        # Two SEQUENTIAL (sibling) alt blocks are flat, single-level — must
        # not be mistaken for nesting just because a second one appears.
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    alt first condition
        S-->>C: first response
    end
    alt second condition
        S-->>C: second response
    end
"""
        text, warnings = _mermaid_to_cfc(diagram, "sibling-flow")
        assert not any("nested" in w.lower() for w in warnings)
        assert 'When first condition, Server SHALL send "first response" to Client.' in text
        assert 'When second condition, Server SHALL send "second response" to Client.' in text

    def test_opt_block_contents_are_skipped_with_warning_not_emitted_as_shall(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    opt optional retry
        C->>S: Retry request
    end
"""
        text, warnings = _mermaid_to_cfc(diagram, "retry-flow")
        assert any("opt" in w.lower() for w in warnings)
        assert "Retry request" not in text
        assert text == ""  # nothing else in this diagram was extractable

    def test_loop_block_contents_are_skipped_with_warning_not_emitted_as_shall(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    loop until acked
        C->>S: Ping
    end
"""
        text, warnings = _mermaid_to_cfc(diagram, "ping-loop-flow")
        assert any("loop" in w.lower() for w in warnings)
        assert "Ping" not in text

    def test_wrapped_continuation_line_is_skipped_with_warning_not_folded_in(self):
        # A naive line-based scan cannot merge a wrapped label's second
        # physical line into the arrow statement above it.
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Submit a request
    continues onto a second physical line that was never really an arrow
"""
        text, warnings = _mermaid_to_cfc(diagram, "wrap-flow")
        assert any("unrecognized" in w.lower() for w in warnings)
        assert "continues onto a second physical line" not in text
        assert 'Client SHALL send "Submit a request" to Server.' in text

    def test_comments_and_directives_are_dropped_cleanly(self):
        diagram = """
sequenceDiagram
    %% this is a comment, not a message
    autonumber
    title Checkout flow
    participant C as Client
    participant S as Server
    C->>S: Submit request
"""
        text, warnings = _mermaid_to_cfc(diagram, "checkout-flow")
        assert "comment" not in text.lower()
        assert "autonumber" not in text.lower()
        assert "Checkout flow" not in text
        assert len(_constraint_lines(text)) == 1
        assert not any("unrecognized" in w.lower() for w in warnings)

    def test_unclosed_block_is_flagged(self):
        diagram = "sequenceDiagram\nalt missing end\nA->>B: hi\n"
        _, warnings = _mermaid_to_cfc(diagram, "truncated-flow")
        assert any("never closed" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Layer 1: syntax variance — handled, not just documented as a gap
# ---------------------------------------------------------------------------


class TestSyntaxVariance:
    @pytest.mark.parametrize("arrow", ["->", "-->", "->>", "-->>", "-x", "--x", "-)", "--)"])
    def test_arrow_token_variants_are_all_recognized(self, arrow):
        diagram = f"sequenceDiagram\nparticipant C as Client\nparticipant S as Server\nC{arrow}S: ping\n"
        text, warnings = _mermaid_to_cfc(diagram, "variant-flow")
        assert '"ping"' in text
        assert not any("unrecognized" in w.lower() for w in warnings)

    def test_activation_shorthand_on_target_is_tolerated(self):
        diagram = "sequenceDiagram\nA->>+B: request\nB-->>-A: response\n"
        text, warnings = _mermaid_to_cfc(diagram, "activation-flow")
        assert '"request"' in text and '"response"' in text
        assert not any("unrecognized" in w.lower() for w in warnings)

    def test_activate_deactivate_are_skipped_cleanly(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Submit request
    activate S
    S-->>C: OK
    deactivate S
"""
        text, warnings = _mermaid_to_cfc(diagram, "flow")
        assert warnings == []
        assert "activate" not in text.lower()

    def test_note_over_multiple_participants_is_parsed(self):
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Submit request
    Note over C,S: Neither party must not skip validation
"""
        text, warnings = _mermaid_to_cfc(diagram, "note-flow")
        assert "must not skip validation" in text

    def test_note_left_of_and_right_of_are_parsed(self):
        diagram = (
            "sequenceDiagram\nparticipant C as Client\nparticipant S as Server\n"
            "C->>S: go\n"
            "Note left of C: C should not retry without backoff\n"
            "Note right of S: S may not cache the response\n"
        )
        text, warnings = _mermaid_to_cfc(diagram, "note-positions-flow")
        assert "should not retry without backoff" in text
        assert "may not cache the response" in text


# ---------------------------------------------------------------------------
# Layer 1: empty / edge cases
# ---------------------------------------------------------------------------


class TestEmptyAndEdgeCases:
    def test_diagram_with_nothing_extractable_returns_empty_text(self):
        diagram = "sequenceDiagram\n%% just a comment\n"
        text, warnings = _mermaid_to_cfc(diagram, "empty-flow")
        assert text == ""

    def test_missing_sequence_diagram_header_is_flagged(self):
        diagram = "participant C as Client\nparticipant S as Server\nC->>S: hi\n"
        text, warnings = _mermaid_to_cfc(diagram, "no-header-flow")
        assert any("sequencediagram" in w.lower() for w in warnings)
        # still extracts what it can — the missing header is advisory, not a hard stop
        assert '"hi"' in text

    def test_blank_input_returns_empty_text_and_header_warning(self):
        text, warnings = _mermaid_to_cfc("", "blank-flow")
        assert text == ""
        assert any("sequencediagram" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Layer 2: tommy_sketch MCP tool — /corpus/ingest call shape (mocked)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_user_conf(monkeypatch, tmp_path):
    # Same isolation test_corpus_gate.py / test_effective_config.py use: a
    # real ~/.memnos/agents/tommy/tommy.conf on the dev machine must not
    # leak into these deterministic assertions.
    monkeypatch.setattr(config_mod, "_USER_CONF", tmp_path / "nonexistent.conf", raising=False)


@pytest.fixture
def isolated_cfg(monkeypatch):
    cfg = TommyConfig(memnos_url="http://fake-memnos.invalid", memnos_token="test-token")
    monkeypatch.setattr(mcp_server_mod, "_cfg", cfg, raising=False)
    monkeypatch.setattr(mcp_server_mod, "_active_project", None, raising=False)
    return cfg


class TestTommySketchIngestCallShape:
    def test_calls_corpus_ingest_with_real_endpoint_shape(self, isolated_cfg, monkeypatch):
        captured = {}

        def fake_ingest(memnos_url, token, namespace, name, text, *, kind="doc", git_sha=None,
                         timeout=30.0, transport=None):
            captured.update(memnos_url=memnos_url, token=token, namespace=namespace,
                             name=name, text=text, kind=kind)
            return {"ok": True, "constraints": 2, "ids": [1, 2]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)

        result = mcp_server_mod.tommy_sketch(flow_name="checkout-flow", mermaid_text=FLAT_DIAGRAM)

        assert captured["memnos_url"] == "http://fake-memnos.invalid"
        assert captured["token"] == "test-token"
        assert captured["namespace"] == isolated_cfg.default_ns
        assert captured["name"] == "checkout-flow"
        assert captured["kind"] == "cfc"
        assert "SHALL" in captured["text"]
        assert result["ok"] is True
        assert result["constraints"] == 2
        assert result["warnings"] == []

    def test_namespace_argument_overrides_default(self, isolated_cfg, monkeypatch):
        captured = {}

        def fake_ingest(memnos_url, token, namespace, name, text, **kw):
            captured["namespace"] = namespace
            return {"ok": True, "constraints": 1, "ids": [1]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)
        mcp_server_mod.tommy_sketch(flow_name="f", mermaid_text=FLAT_DIAGRAM, namespace="org:custom")
        assert captured["namespace"] == "org:custom"

    def test_read_only_token_403_surfaces_as_not_ok_never_raised(self, isolated_cfg, monkeypatch):
        def fake_ingest(*a, **kw):
            return {"ok": False, "error": "corpus ingest failed (403): forbidden"}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)
        result = mcp_server_mod.tommy_sketch(flow_name="checkout-flow", mermaid_text=FLAT_DIAGRAM)
        assert result["ok"] is False
        assert "403" in result["error"]

    def test_mermaid_file_is_read_when_text_not_given(self, isolated_cfg, monkeypatch, tmp_path):
        f = tmp_path / "diagram.mmd"
        f.write_text(FLAT_DIAGRAM)
        captured = {}

        def fake_ingest(memnos_url, token, namespace, name, text, **kw):
            captured["text"] = text
            return {"ok": True, "constraints": 1, "ids": [1]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)
        result = mcp_server_mod.tommy_sketch(flow_name="f", mermaid_file=str(f))
        assert "SHALL" in captured["text"]
        assert result["ok"] is True

    def test_missing_mermaid_input_is_an_error_not_a_crash(self, isolated_cfg):
        result = mcp_server_mod.tommy_sketch(flow_name="f")
        assert "error" in result

    def test_missing_flow_name_is_an_error(self, isolated_cfg):
        result = mcp_server_mod.tommy_sketch(flow_name="   ", mermaid_text=FLAT_DIAGRAM)
        assert "error" in result

    def test_unreadable_mermaid_file_is_an_error_not_a_crash(self, isolated_cfg, tmp_path):
        missing = tmp_path / "does-not-exist.mmd"
        result = mcp_server_mod.tommy_sketch(flow_name="f", mermaid_file=str(missing))
        assert "error" in result

    def test_diagram_with_nothing_extractable_is_an_error_with_warnings_and_never_calls_ingest(
        self, isolated_cfg, monkeypatch,
    ):
        called = {"n": 0}

        def fake_ingest(*a, **kw):
            called["n"] += 1
            return {"ok": True, "constraints": 0, "ids": []}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)
        result = mcp_server_mod.tommy_sketch(flow_name="f", mermaid_text="sequenceDiagram\n%% nothing\n")
        assert "error" in result
        assert "warnings" in result
        assert called["n"] == 0
