# memnos — the friendly quickstart

*For sharing with someone who isn't deep in the codebase: what memnos is, why it
helps, and exactly what to hand your IT person. Want a link you can just send
someone? Share **[memnos.net/getting-started.html](https://memnos.net/getting-started.html)**
instead — same content, designed, and public. The page below is the same
content in plain markdown, for reading in the repo.*

## What is memnos, in one sentence

memnos gives an AI assistant (Claude, ChatGPT-style tools, Cursor, etc.) a real
memory — so it remembers who you are, what you're working on, and what you told
it last week, without you re-explaining every time.

## Why it's useful

- **No more repeating yourself.** Tell it once — a decision, a preference, a
  project detail — and it's there next time, even in a brand-new conversation.
- **Organized by project.** Keep "work," "personal," and each client/project
  separate (see Namespaces below) so memories never bleed into each other.
- **Runs on your own machine.** Self-hosted, private by default — nothing leaves
  the laptop unless you choose to add a cloud AI key.
- **Free.** No subscription. The optional cloud key (for slightly sharper recall)
  is the only thing that can ever cost anything, and it's entirely optional.

## The one concept worth understanding: namespaces

A **namespace** is just a labeled folder for memories — e.g. `work:acme-project`,
`personal`, `family`. Everything you tell memnos goes into a namespace you choose,
and it only recalls from the namespaces it's been given access to. Think of it
like separate notebooks: a work notebook and a personal notebook don't get mixed up.

Most people start with just **one** namespace and only add a second once they
notice they actually want one.

## Setup — for your IT person (Windows)

This installs memnos locally — no cloud account, no Docker, nothing else to set
up first.

```powershell
# 1. Install uv (a Python package runner)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Open a NEW PowerShell window, then install memnos
uv tool install memnos
memnos --help                 # confirms it installed correctly

# 3. Set it up — downloads its own private database, nothing else needed
memnos setup

# 4. Start it
memnos start
memnos status
```

That's the whole install. Step 3 prints a one-time admin token — save it; it's
needed once to open the management console at `http://127.0.0.1:8900/admin`.

Full step-by-step, with troubleshooting (antivirus scan delays, port conflicts),
is in [`windows.md`](windows.md).

### Connect it to Claude Code (or another AI tool)

```powershell
memnos agent-setup claude-code
```

One command, wires it in automatically. Restart the AI tool afterward — recall
and save then happen automatically in the background.

## How to use it, day to day

Nothing to learn — just talk to your AI assistant normally. In the background:

- **It saves** what you tell it (facts, preferences, decisions) as you go.
- **It recalls** relevant memories automatically before each response, so the
  assistant already knows the context.

Curious what it remembers? Just ask it directly — e.g. *"what do you remember
about the Q3 roadmap?"*

## Is my data private? Does this cost anything?

- **Private**: everything is stored in a database on your own machine. Nothing
  is sent anywhere unless you explicitly add a cloud AI key.
- **Free**: no subscription, no usage fees. An optional OpenAI key sharpens
  recall slightly — entirely your choice, and skippable.

## Learn more

- [Full Quickstart](../../QUICKSTART.md) — the complete version, for anyone
  comfortable with a terminal.
- [Windows guide](windows.md) — detailed install + troubleshooting.
- [memnos.net](https://memnos.net) — overview and comparisons.
