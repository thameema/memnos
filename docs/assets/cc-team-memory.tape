# memnos — two Claude Code agents (OPUS), one shared memory (TRUE split-screen via tmux)
# Re-record:  bash docs/assets/cc-team-memory.render.sh
#   (the render script stands up the OFF-camera scratch setup, runs the real opus
#    agents, captures their verbatim output, then runs vhs on this tape)
#
# WHAT THIS SHOWS (all real, captured from live OPUS runs — claude -p --model opus,
# i.e. claude-opus-4-8):
#   Two SEPARATE Claude Code agents — Dev Alice and Dev Bob — each its own OS
#   process, its own scratch $HOME, its own memnos bearer token, all wired to ONE
#   shared namespace `team:eng` on a scratch memnos server running OPENAI 1536-d
#   embeddings + LLM fact extraction (text-embedding-3-small). A bidirectional relay:
#     1. Alice's agent decides an auth approach (PLAT-214). memnos's Stop hook
#        AUTO-CAPTURES both sides — no visible save command (saved by dev-alice).
#     2. Bob's agent (fresh context, never saw Alice's session) is asked what the
#        team decided. memnos's pre-prompt hook AUTO-INJECTS Alice's memory; Bob
#        answers citing dev-alice's decision + reasoning, then adds a production
#        constraint (regenerate the session ID on login/privilege change to defeat
#        session fixation).
#     3. Alice's NEXT turn picks up Bob's constraint — and correctly credits it to
#        dev-bob — proving the channel is bidirectional with server-stamped
#        attribution (by dev-alice / by dev-bob).
#
# WHY OPUS: opus keeps attribution exact — Alice's turn 3 reads "...dev-bob raised a
# production constraint that my implementation must satisfy..." with no slip.
#
# OFF-CAMERA SCRATCH SETUP (never touches the owner's live setup):
#   - Scratch memnos server on port 8902, OPENAI 1536-d mode (OPENAI_API_KEY sourced
#     READ-ONLY from the owner's encrypted secret vault, never shown), scratch
#     HOME=/tmp/memnos-cc-demo/server-home, scratch DB `cc_team_demo` created fresh
#     at vector dim 1536 in the pgvector/pgvector:pg16 Docker container (host :5439),
#     DROPped afterward. The live server (:8900) and live DBs are untouched.
#   - memnos run from the WORKING TREE (isolated `pip install -e` venv) so the
#     issue-#18 fix is active: headless `claude -p` Stop hooks capture the assistant
#     side via last_assistant_message — NO external wrapper.
#   - Principals dev-alice / dev-bob, each granted read+write on team:eng, each with
#     its own bearer token (referenced only off-camera; NEVER on screen).
#   - Each agent runs under its OWN scratch $HOME so `memnos agent-setup claude-code`
#     writes hooks/MCP into the SCRATCH ~/.claude, never the real one.
#
# HONESTY: headless `claude -p` prints-then-exits, so the split-screen REPLAYS the
# captured VERBATIM real opus-agent output for clean two-pane timing — every line of
# agent text is exactly what the live agents produced; the injected memory block is
# the actual recall-hook output (soft-wrapped to pane width). Nothing is invented.
# The verbatim source + driver/player/seq live in docs/assets/.cc-replay/ (untracked,
# secret-free) so the GIF can be re-timed/re-rendered later with no paid re-run.

Output docs/assets/cc-team-memory.gif
Output docs/assets/cc-team-memory.mp4

Set Shell bash
Set Theme "Catppuccin Mocha"
Set FontSize 13
Set Width 1760
Set Height 820
Set Padding 16
Set Framerate 30

Hide
Type "bash /tmp/memnos-cc-demo/driver.sh >/tmp/memnos-cc-demo/driver.log 2>&1 & sleep 1; clear; tmux attach -t memnos_cc"
Enter
# stay hidden until the split + both pane banners are drawn, so the FIRST visible
# frame is the clean two-pane layout (not the bootstrap command line).
Sleep 2600ms
Show

Sleep 51500ms
