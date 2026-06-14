# Your AI remembers your repo. memnos remembers your team.

## The problem: memory is local and per-developer

Every AI coding tool now has memory. Claude Code has CLAUDE.md. Cursor has rules.
There's `memory.md`, and the models themselves are starting to remember your chats. All
of it is genuinely useful — and all of it is **local and per-developer**. It lives on one
person's machine, scoped to one repo or one person's chat history.

CLAUDE.md describes *the repo*: build commands, conventions, the layout. That's the right
scope for it. But it sits in your checkout, on your laptop, and it captures what *you* told
*your* agent.

## The insight: the lost knowledge is between developers

The knowledge that actually gets lost isn't in the repo. It's **between developers**:

- the decision Alice made three sprints ago, and *why*;
- the incident Bob debugged at 2am, and what the root cause turned out to be;
- the constraint the team agreed on that isn't written down anywhere an agent can read.

That knowledge never reaches the next engineer's agent. When a new teammate's Claude Code
opens the repo, it sees CLAUDE.md — it does not see the reasoning behind the retry budget
Alice set, attributed to Alice, with the ticket IDs. Today that lives in someone's head, a
closed Slack thread, or a PR comment nobody will find again.

## What memnos does: shared, governed, attributed memory on your Postgres

memnos is one self-hosted memory server, backed by one Postgres (pgvector), that the whole
team's agents read and write. It is:

- **Shared.** One host, namespaced. A fact Alice's agent learns is recallable by Bob's
  agent later, even though Bob never saw her session.
- **Governed.** Bearer-token auth, namespace-level ACLs (a token reads only the namespaces
  it's granted), and an append-only audit log. A larger org can actually run it.
- **Attributed.** Authorship is **server-stamped** from the authenticated principal — a
  body-supplied author is ignored. A recalled fact is correctly tied to the teammate who
  learned it, and you can't spoof who learned something.

Under the hood it's deliberately simple: **one Postgres, no LLM call at query time.**
Bi-temporal supersession is decided on the *write* path — when a newer fact contradicts an
older one, the old fact is closed out with an end-date automatically, recall ranks the
current truth first, and history is retained. So recall is fast and deterministic, and the
team's memory stays true to *now* without an LLM in the hot path.

There's also a fully-local **$0 option**: 384-d local embeddings plus optional local
extraction against any OpenAI-compatible endpoint (e.g. Ollama `llama3.2:3b` via
`MEMNOS_EXTRACT_BASE_URL`), so an air-gapped team can run the whole thing on its own
hardware with no external API spend.

## Who it's for

1. **AI engineering teams** on Claude Code / Codex / Cursor who want shared, governed
   memory across the team rather than per-developer silos.
2. **LangChain / LangGraph builders** who need a real memory backend — not a toy vector
   store — with attribution and access control.
3. **Self-hosted agent runners** (Hermes / OpenClaw-style) who want local, $0, private
   memory on their own box.

If you're a solo developer, CLAUDE.md is fine — you don't need this. The moment more than
one person's agent should know what the others learned, you need a shared layer. That's
memnos.

## Get started

- **Stand it up for a team:** [`docs/guides/team.md`](guides/team.md).
- **Benchmarks (honestly reported):** LongMemEval **78.4%** (500 questions; gpt-4o answer +
  judge; MemoryBench harness); LoCoMo **64–65%** (judge ladder, strict→lenient). Not SOTA,
  reported straight.
- License: **Apache-2.0**. Repo: <https://github.com/thameema/memnos>.
