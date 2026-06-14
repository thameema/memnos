# memnos — two developers, one shared memory (TRUE split-screen via tmux)
# Re-record:  HOME=/tmp/memnos-team-demo/home OPENAI_API_KEY="" vhs docs/assets/team-memory-demo.tape
#
# Prereqs (all OFF camera — set up before recording):
#   1. A FRESH scratch memnos server in local-384 mode (OPENAI_API_KEY="") on a
#      scratch port (>=8902) with a scratch HOME=/tmp/memnos-team-demo/home, backed
#      by a scratch DB (team_demo in the pgvector/pgvector:pg16 Docker container on
#      host port 5439). Created via:  memnos setup --dsn postgresql://memnos:memnos@localhost:5439/team_demo
#      then `memnos serve --port 8902`. NEVER point at the live server (8900) or live config.
#   2. Two principals dev-alice / dev-bob, each granted read+write on namespace team:eng,
#      each with a bearer token.
#   3. /tmp/memnos-team-demo/demo.env exporting MEMNOS_URL (the scratch server),
#      $ALICE and $BOB (the two tokens) and a neutral PS1 — NEVER put tokens in this
#      file's git copy or on camera; they are referenced only as $ALICE / $BOB.
#   4. /tmp/memnos-team-demo/driver.sh — the off-camera tmux orchestrator that splits
#      the screen into LEFT=Dev Alice / RIGHT=Dev Bob and drives the keystrokes.
#   5. namespace team:eng starts EMPTY (truncate the scratch DB tables first).
#
# Layout on screen: a single terminal split into two vertical panes. Left pane is
# "Dev Alice — laptop" (pink prompt), right pane is "Dev Bob — laptop" (blue prompt).
# Alice remembers a decision; Bob (a separate session) recalls it and gets it back
# attributed `by dev-alice`.

Output docs/assets/team-memory-demo.gif
Output docs/assets/team-memory-demo.mp4

Set Shell bash
Set Theme "Catppuccin Mocha"
Set FontSize 15
Set Width 1700
Set Height 760
Set Padding 20
Set TypingSpeed 32ms
Set Framerate 30

Hide
# Start the off-camera orchestrator (it builds the tmux split + drives both panes),
# then attach so vhs records the live split-screen. When the driver kills the session,
# `tmux attach` exits and the recording ends.
Type "bash /tmp/memnos-team-demo/driver.sh >/tmp/memnos-team-demo/driver.log 2>&1 & sleep 2; clear; tmux attach -t memnos_team"
Enter
Sleep 1500ms
Show

# The driver controls all timing/keystrokes from here; it holds on the punchline at
# the end (it does NOT kill the session), so the recording closes on a clean frame.
Sleep 37s
