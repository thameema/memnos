#!/usr/bin/env bash
# Re-record docs/assets/cc-team-memory.{gif,mp4} from scratch — OPUS + OpenAI edition.
#
# This stands up the ENTIRE off-camera scratch setup (isolated from your live memnos
# + Claude Code), drives a real bidirectional relay between two separate Claude Code
# agents RUNNING ON OPUS (claude -p --model opus, i.e. claude-opus-4-8) against a
# scratch memnos server configured for OPENAI 1536-d embeddings + LLM fact
# extraction, captures their verbatim output, then records the tmux split-screen
# with vhs. It NEVER touches:
#   - your live memnos server on :8900 or its DBs (memnos, memnos_gate)
#   - your real ~/.memnos/config.json / encrypted secret vault (read-only key access)
#   - your real ~/.claude/settings.json / ~/.claude.json
#   - the globally installed `memnos` (~/.local/bin/memnos)
#
# WHAT CHANGED vs the earlier local-384 build:
#  1. EMBEDDINGS = OpenAI 1536-d (text-embedding-3-small) + LLM extraction. The
#     scratch server is started with OPENAI_API_KEY set, which makes memnos bake the
#     scratch DB schema at vector dim 1536 and enable extraction. The key is sourced
#     READ-ONLY from the owner's encrypted vault (secret name `openai`) — decrypted
#     with the live MEMNOS_SECRET_KEY via a SELECT-only DB read, written to a 0600
#     scratch file, and NEVER printed on camera or written into any deliverable.
#  2. STOCK HOOK, NO WRAPPER (issue #18). memnos is run from the WORKING TREE via an
#     isolated `pip install -e` venv, so cmd_hook's #18 fix is active: in headless
#     `claude -p` the Stop hook captures the assistant side from the payload's
#     `last_assistant_message` when the transcript hasn't flushed the final block.
#     Both sides (user + assistant) are auto-saved with NO external wrapper.
#  3. OPUS for all relay turns (quality / exact attribution).
#  4. ~50-58s dwell, and a text-only replay source saved under docs/assets/.cc-replay/
#     (UNTRACKED, secret-free) so future re-times need NO paid re-run.
#
# COST (owner approved): ~3 opus relay turns + 1 auth ping (Claude tokens) and the
# OpenAI embedding + extraction calls for ~6 stored turns (a few cents). The relay is
# kept minimal so spend stays small.
#
# The relay proved (verified in the scratch DB raw_turns + audit_log, attribution
# server-stamped from each agent's bearer token):
#   turn 1  dev-alice  decides: server-side sessions for PLAT-214 (opaque session ID
#                      in Redis, HttpOnly/Secure/SameSite cookie), reason = instant
#                      revocation + XSS/CSRF defense. Stop hook AUTO-CAPTURES both
#                      sides (by dev-alice).
#   turn 2  dev-bob    (fresh context, never saw Alice's session) recalls Alice's
#                      decision (auto-injected by the pre-prompt hook), cites
#                      dev-alice, adds a constraint: regenerate the session ID on
#                      login / privilege change to defeat session fixation.
#   turn 3  dev-alice  recalls Bob's constraint (auto-injected), uses it, and
#                      CORRECTLY credits dev-bob — no attribution slip.
#
# Because headless `claude -p` prints-then-exits, the split-screen REPLAYS the
# captured verbatim opus output (see cc-team-memory.tape header). The text shown is
# exactly what the live opus agents produced; the recall block is the real pre-prompt
# hook output (soft-wrapped). Nothing is invented. The verbatim source + the
# driver/player/seq scripts live in docs/assets/.cc-replay/ for free re-timing.
set -euo pipefail
echo "See this file's header. Reproduction steps:"
cat <<'STEPS'
0. Baseline isolation — checksum protected files + confirm live server answers
   (must be byte-identical at the end):
     for f in ~/.claude/settings.json ~/.memnos/config.json ~/.claude.json; do shasum -a256 "$f"; done
     shasum -a256 ~/.local/bin/memnos          # installed memnos must NOT change
     curl -s http://127.0.0.1:8900/readyz       # -> {"ready": true}
   Live `memnos` DB is on the native Postgres at localhost:5432; the scratch DB goes
   in the pgvector/pgvector:pg16 Docker container on host :5439 (container
   memnos-gate-pg, which also holds memnos_gate — do NOT touch it). Snapshot a
   row-count anchor of memnos_gate so you can prove it is unchanged afterward.

1. Source the OpenAI key READ-ONLY from the vault (never printed):
     # MEMNOS_SECRET_KEY = ~/.memnos/config.json secret_key; dsn = live memnos DB
     python3 - <<'PY'
     import json,os; from core.vault import Vault; import psycopg
     from psycopg.rows import dict_row
     cfg=json.load(open(os.path.expanduser('~/.memnos/config.json')))
     os.environ['MEMNOS_SECRET_KEY']=cfg['secret_key']
     with psycopg.connect(cfg['dsn'],row_factory=dict_row) as c:   # SELECT only
         k=Vault.get(c,'openai')
     fd=os.open('/tmp/memnos-cc-demo/openai.key',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
     os.write(fd,k.encode()); os.close(fd)
     PY

2. Isolated scratch venv on the WORKING TREE (so the #18 hook fix is active):
     python3 -m venv /tmp/memnos-cc-demo/scratch-venv
     /tmp/memnos-cc-demo/scratch-venv/bin/pip install -e .   # editable -> uses repo memnos_cli.py
   Use $SCRATCH/scratch-venv/bin/memnos for EVERY scratch command below.

3. Scratch DB + server in OpenAI 1536-d mode:
     docker exec memnos-gate-pg psql -U memnos -d postgres -c "CREATE DATABASE cc_team_demo;"
     HOME=$S/server-home OPENAI_API_KEY="$(cat $S/openai.key)" \
       $VENV/memnos setup --dsn postgresql://memnos:memnos@localhost:5439/cc_team_demo
       # OPENAI_API_KEY present -> schema baked at vector dim 1536
     HOME=$S/server-home OPENAI_API_KEY="$(cat $S/openai.key)" $VENV/memnos serve --port 8902 &
     # wait for http://127.0.0.1:8902/readyz -> {"ready": true}
     # server.log must show: "[memnos] OpenAI 1536-d embeddings + extraction ENABLED"

4. Principals + namespace + grants + dev tokens (HOME=server-home, MEMNOS_TOKEN=admin):
     memnos namespace add team:eng
     memnos principal create dev-alice ; memnos principal create dev-bob
     ALICE=$(memnos token mint dev-alice --label demo | grep -o 'mnk_[A-Za-z0-9_-]*')
     BOB=$(memnos token mint dev-bob   --label demo | grep -o 'mnk_[A-Za-z0-9_-]*')
     memnos grant add dev-alice team:eng ; memnos grant add dev-bob team:eng

5. Wire EACH agent under its OWN scratch HOME (writes hooks into the scratch
   ~/.claude, never the real one). Then in each scratch settings.json: swap the
   auto-minted token for the dev token, and rewrite the hook command to the ABSOLUTE
   scratch-venv memnos so the #18 working-tree code runs (not installed v0.1.10):
     HOME=$S/alice MEMNOS_URL=http://127.0.0.1:8902 $VENV/memnos agent-setup claude-code --namespace team:eng --force
     # ...same for bob... then sed mnk_* -> $ALICE/$BOB and `memnos hook` -> `$VENV/memnos hook`
   Give each scratch HOME a logged-in Claude:
     security find-generic-password -s "Claude Code-credentials" -w > $S/<who>/.claude/.credentials.json
   and carry oauthAccount/userID/anonymousId/hasCompletedOnboarding from your real
   ~/.claude.json into each scratch ~/.claude.json (keep scratch mcpServers; DROP
   "projects"). Headless `claude -p` reads the file-based credential when HOME is
   non-default. SHRED that credential copy + the OpenAI key + dev tokens at cleanup.

6. Run the relay on OPUS (real cost), tenant tables truncated so the ns starts EMPTY,
   verifying between turns via the scratch DB + audit_log:
     claude -p --model opus --permission-mode bypassPermissions "<turn prompt>"
     turn1 (HOME=alice): "PLAT-214 ... pick the session auth approach and say why"
     turn2 (HOME=bob):   pre-prompt hook recall injected, then "what did the team
                         decide about auth for PLAT-214, and why? ... add ONE
                         production constraint"
     turn3 (HOME=alice): pre-prompt hook recall injected, then "any team constraint
                         my PLAT-214 plan must satisfy?"
   Capture each verbatim reply to out/a{1,2,3}.txt; capture the recall-hook injection
   each agent receives to out/*_inject.disp. Verify in DB:
     SELECT id,speaker,author_principal,left(text,55) FROM tenant_memnos.raw_turns ORDER BY id;
     SELECT p.name,a.action,a.namespace,a.result_count FROM memnos_control.audit_log a
       JOIN memnos_control.principals p ON p.id=a.principal_id ORDER BY a.id;

7. Record (replays the verbatim captured opus output in a tmux split via per-pane
   player.sh fed by an interleaved seq.txt; renders rendered output only):
     vhs docs/assets/cc-team-memory.tape   (drives driver.sh -> tmux split)
   The replay source (out/*.txt, *.show, seq.txt, driver.sh, player.sh, gen_timeline.py)
   is copied SECRET-FREE into docs/assets/.cc-replay/ for free re-timing later.

8. Cleanup: kill scratch server + tmux; DROP DATABASE cc_team_demo; remove the
   scratch venv; SHRED $S (it holds the OAuth credential copy + dev tokens + OpenAI
   key copy); confirm the live server on :8900 still answers {"ready":true}, the
   memnos_gate row-count anchor is unchanged, and the real
   ~/.claude/settings.json + ~/.claude.json + ~/.memnos/config.json + installed
   memnos checksums are byte-identical to step 0.
STEPS
