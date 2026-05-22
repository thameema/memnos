# Contributing to engram

Thank you for your interest in contributing. engram is MIT-licensed and community contributions are essential to making it a better tool.

## Before you start

1. **Check existing issues** — someone may already be working on it
2. **Open an issue first** for anything non-trivial (new features, architectural changes) so we can discuss before you write code
3. For bug fixes and small improvements, a PR is fine without a prior issue

## Development setup

```bash
git clone https://github.com/thameema/engram.git
cd engram
make setup          # install all packages in editable mode + copy example configs
# Edit .env with your API keys
make dev            # start Neo4j + Qdrant via Docker Compose
python -m engram_api.main   # start the server
```

## Project layout

```
packages/
  core/           engram Python client (memory, graph, vector, skills)
  mcp-server/     MCP server + SSE transport for Claude Code
  orchestrator/   Multi-agent task runner
  api/            FastAPI REST API
  gateway/        Telegram + WhatsApp bots
  learning/       Reflection, heuristics, skill extraction
agents/           Built-in agent YAML definitions
docs/             Guides and reference docs
```

## What we need most

| Area | What |
|------|------|
| Integrations | New MCP tools, Discord/Slack/SMS gateways |
| Vector backends | Pinecone, Weaviate, pgvector |
| Graph backends | Lightweight alternative to Neo4j (SQLite-based?) |
| Learning | Better reflection prompts, smarter heuristic decay |
| Packaging | Homebrew formula, Docker Hub image, proper PyPI release |
| Docs | Tutorials, recipes, video walkthroughs |
| Tests | Unit and integration coverage is thin |

## Code style

- Python: `black` + `ruff` (run `make lint` before committing)
- Async/await throughout — no blocking I/O in async functions
- No new runtime dependencies without opening an issue first
- No comments that explain what the code does — only comments that explain why

## Pull requests

1. Fork the repo and create a branch from `main`
2. Run `make test` before submitting (even if coverage is sparse)
3. Keep PRs focused — one feature or fix per PR
4. Write a clear description: what changed and why
5. We aim to review within 48 hours

## Commit messages

Use present tense, imperative mood: "add Pinecone backend" not "added Pinecone backend". No AI attribution trailers needed.

## Reporting bugs

Open a GitHub issue with:
- engram version (`engram status`)
- OS and Python version
- Steps to reproduce
- What you expected vs what happened
- Relevant logs (`engram logs`)

## Questions

Open a GitHub Discussion or an issue tagged `question`. We are happy to help.
