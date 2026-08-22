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

    def test_note_inside_alt_gets_the_same_conditional_prefix_as_arrows(self):
        # Regression: a Note-derived prohibition inside a supported `alt`
        # block used to be emitted as an absolute, unconditional statement
        # — silently dropping the enclosing alt's condition entirely, unlike
        # arrow-derived SHALL statements (test_alt_else_become_when_
        # condition_prefixes above), which already got nearest_active_alt()'s
        # "When <condition>, ..." prefix. On unfixed code this diagram
        # produces the bare, unconditional "Server must not accept writes."
        # with zero warning — exactly the "silently over-broadened into an
        # absolute rule" failure this module's docstring says v1 must never
        # do.
        diagram = """
sequenceDiagram
    participant S as Server
    alt only in maintenance mode
        Note over S: Server must not accept writes
    end
"""
        text, warnings = _mermaid_to_cfc(diagram, "maint-flow")
        assert 'When only in maintenance mode, Server must not accept writes.' in text
        # The old, unconditional form must be gone — not just the new form
        # additionally present.
        assert text.count("Server must not accept writes.") == 1
        assert "\nServer must not accept writes." not in text
        assert warnings == []
        # The alt-prefixed prohibition must still be genuinely extractable
        # by core/store.py's real RFC-2119 keyword regex — a fix that made
        # the statement conditional but broke extractability would trade
        # one silent failure for another.
        lines = _constraint_lines(text)
        assert any(_REAL_CONSTRAINT_RE.search(line.upper()) for line in lines)

    def test_note_outside_alt_stays_unconditional(self):
        # Sibling case to the alt-prefix regression above: a prohibition
        # Note that is NOT inside any alt block must keep reading as an
        # absolute statement — nearest_active_alt() must return None here,
        # not a stale condition from some earlier block.
        diagram = """
sequenceDiagram
    participant C as Client
    participant S as Server
    alt some condition
        C->>S: Submit request
    end
    Note over S: Server must not accept writes
"""
        text, warnings = _mermaid_to_cfc(diagram, "unconditional-flow")
        assert "Server must not accept writes." in text
        assert "When some condition, Server must not accept writes." not in text
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
    def test_calls_corpus_ingest_with_real_endpoint_shape(self, isolated_cfg, monkeypatch, tmp_path):
        captured = {}

        def fake_ingest(memnos_url, token, namespace, name, text, *, kind="doc", git_sha=None,
                         timeout=30.0, transport=None):
            captured.update(memnos_url=memnos_url, token=token, namespace=namespace,
                             name=name, text=text, kind=kind)
            return {"ok": True, "constraints": 2, "ids": [1, 2]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)

        # No tommy.yaml in `workspace` — exercises the CORRECTED resolution
        # path's fallback (effective.value("namespace"), same as
        # tommy_dispatch's corpus gate / tommy_verdict / tommy_drift_sweep),
        # which lands on cfg.default_ns here for the same reason the old
        # _effective_namespace(cfg) helper did: nothing overrides it. See
        # test_tommy_yaml_namespace_override_is_picked_up_by_default below
        # for the case that actually distinguishes the two paths.
        result = mcp_server_mod.tommy_sketch(
            flow_name="checkout-flow", mermaid_text=FLAT_DIAGRAM, workspace=str(tmp_path))

        assert captured["memnos_url"] == "http://fake-memnos.invalid"
        assert captured["token"] == "test-token"
        assert captured["namespace"] == isolated_cfg.default_ns
        assert captured["name"] == "checkout-flow"
        assert captured["kind"] == "cfc"
        assert "SHALL" in captured["text"]
        assert result["ok"] is True
        assert result["constraints"] == 2
        assert result["warnings"] == []

    def test_namespace_argument_overrides_default(self, isolated_cfg, monkeypatch, tmp_path):
        captured = {}

        def fake_ingest(memnos_url, token, namespace, name, text, **kw):
            captured["namespace"] = namespace
            return {"ok": True, "constraints": 1, "ids": [1]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)
        mcp_server_mod.tommy_sketch(
            flow_name="f", mermaid_text=FLAT_DIAGRAM, namespace="org:custom", workspace=str(tmp_path))
        assert captured["namespace"] == "org:custom"

    def test_tommy_yaml_namespace_override_is_picked_up_by_default(
        self, isolated_cfg, monkeypatch, tmp_path,
    ):
        """Regression for issue #124: tommy_sketch used to resolve its
        default namespace via `_effective_namespace(cfg)` (the
        active-project helper, which always falls through to
        `cfg.default_ns` since `ProjectEntry` carries no `namespace` field)
        — diverging from tommy_dispatch's corpus gate, tommy_verdict, and
        tommy_drift_sweep (fixed for the same divergence in #128), all of
        which resolve via effective_config.py's tommy.yaml-aware
        `effective.value("namespace")`. A project whose tommy.yaml set a
        `memnos.namespace` override had its dispatch-gate/verdict/drift
        checks land in one namespace while `/sketch`-ingested constraints
        silently landed in a completely different one — invisible to the
        very enforcement path they exist to feed. This pins the corrected
        behavior: `/sketch`'s default namespace now honors the same
        tommy.yaml override the gate/verdict/drift-sweep already do."""
        (tmp_path / "tommy.yaml").write_text(
            'tommy:\n  version: 1\nmemnos:\n  namespace: "org:custom:from-yaml"\n'
        )
        captured = {}

        def fake_ingest(memnos_url, token, namespace, name, text, **kw):
            captured["namespace"] = namespace
            return {"ok": True, "constraints": 1, "ids": [1]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)

        assert isolated_cfg.default_ns != "org:custom:from-yaml", (
            "fixture bug: this test only proves anything if the tommy.yaml "
            "override differs from what cfg.default_ns would have given"
        )

        result = mcp_server_mod.tommy_sketch(
            flow_name="checkout-flow", mermaid_text=FLAT_DIAGRAM, workspace=str(tmp_path))

        assert captured["namespace"] == "org:custom:from-yaml"
        assert captured["namespace"] != isolated_cfg.default_ns
        assert result["ok"] is True

    def test_explicit_namespace_still_overrides_a_tommy_yaml_default(
        self, isolated_cfg, monkeypatch, tmp_path,
    ):
        (tmp_path / "tommy.yaml").write_text(
            'tommy:\n  version: 1\nmemnos:\n  namespace: "org:custom:from-yaml"\n'
        )
        captured = {}

        def fake_ingest(memnos_url, token, namespace, name, text, **kw):
            captured["namespace"] = namespace
            return {"ok": True, "constraints": 1, "ids": [1]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)
        mcp_server_mod.tommy_sketch(
            flow_name="f", mermaid_text=FLAT_DIAGRAM, namespace="org:explicit", workspace=str(tmp_path))
        assert captured["namespace"] == "org:explicit"

    def test_broken_tommy_yaml_aborts_the_sketch_visibly(self, isolated_cfg, monkeypatch, tmp_path):
        """A tommy.yaml that fails to parse must abort the ingest with a
        visible error — the same "never silently swallow" posture
        tommy_verdict/tommy_drift_sweep already apply to their own
        tommy.yaml read, and never a silent fall-back to cfg.default_ns for
        a namespace tommy.yaml explicitly tried, and failed, to set."""
        # Same "broken tommy.yaml" fixture content as test_drift_sweep.py's
        # test_broken_tommy_yaml_aborts_the_sweep_visibly / test_verdict.py's
        # TestBrokenTommyYaml: valid YAML syntax, but `platform` is not a
        # recognized top-level tommy.yaml field, which load_tommy_yaml()
        # rejects with TommyYamlError (schema validation, not a YAML parse
        # error).
        (tmp_path / "tommy.yaml").write_text("tommy:\n  version: 1\nplatform:\n  foo: bar\n")
        called = {"n": 0}

        def fake_ingest(*a, **kw):
            called["n"] += 1
            return {"ok": True, "constraints": 1, "ids": [1]}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)
        result = mcp_server_mod.tommy_sketch(
            flow_name="checkout-flow", mermaid_text=FLAT_DIAGRAM, workspace=str(tmp_path))

        assert result["ok"] is False
        assert "tommy.yaml" in result["error"]
        assert called["n"] == 0

    def test_read_only_token_403_surfaces_as_not_ok_never_raised(self, isolated_cfg, monkeypatch, tmp_path):
        def fake_ingest(*a, **kw):
            return {"ok": False, "error": "corpus ingest failed (403): forbidden"}

        monkeypatch.setattr(mcp_server_mod, "_http_corpus_ingest", fake_ingest)
        result = mcp_server_mod.tommy_sketch(
            flow_name="checkout-flow", mermaid_text=FLAT_DIAGRAM, workspace=str(tmp_path))
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
        result = mcp_server_mod.tommy_sketch(flow_name="f", mermaid_file=str(f), workspace=str(tmp_path))
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
