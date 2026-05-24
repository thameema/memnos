"""
Catalog of Claude Code capabilities for the Skill Coach.

Each entry describes one technique or feature. The 'when_to_use' field is
optimized for semantic similarity matching against developer task descriptions.
The 'content' field is the full memory written to the skill namespace.
"""
from __future__ import annotations

CLAUDE_CODE_CAPABILITIES: list[dict] = [
    {
        "id": "cc-loop",
        "title": "/loop — Repeat a task on an interval",
        "category": "slash-commands",
        "when_to_use": "polling deploy status watch test results check service health wait for CI to pass monitor background job repeatedly run same check",
        "example": "/loop check if the deployment is healthy every 30 seconds",
        "content": (
            "SKILL: /loop slash command\n"
            "Use /loop to run a prompt or slash command on a repeating interval. "
            "Perfect for: watching test results, polling deploy status, waiting for a CI job to finish, "
            "checking if a service came up after restart.\n"
            "Example: /loop every 30s run the test suite and stop when all pass\n"
            "Stop the loop with Ctrl+C."
        ),
        "tags": ["loop", "polling", "automation", "deploy", "ci", "monitoring", "watch", "repeat"],
    },
    {
        "id": "cc-compact",
        "title": "/compact — Compress context with custom focus",
        "category": "context-management",
        "when_to_use": "running out of tokens long session hitting context limit preserve thread free token budget summarize conversation",
        "example": "/compact focus on the authentication changes we made",
        "content": (
            "SKILL: /compact with instructions\n"
            "Use /compact before hitting the token limit to compress history while preserving the thread. "
            "Pass focus instructions: /compact keep the API design decisions and ignore the debugging tangents. "
            "Call it proactively — don't wait until Claude truncates automatically."
        ),
        "tags": ["compact", "context", "tokens", "compression", "session", "memory", "limit"],
    },
    {
        "id": "cc-pipe-review",
        "title": "Pipe patterns — feed output directly to Claude",
        "category": "patterns",
        "when_to_use": "review a git diff analyze logs diagnose docker output check test failures pipe output stdin",
        "example": "git diff main | claude -p 'review these changes for security issues'",
        "content": (
            "SKILL: Pipe-based workflows\n"
            "Stream any terminal output directly to Claude without copy-pasting:\n"
            "  git diff main | claude -p 'review for breaking changes'\n"
            "  docker logs myservice | claude -p 'diagnose the error'\n"
            "  npm test 2>&1 | claude -p 'explain failures and suggest fixes'\n"
            "  cat error.log | claude -p 'find the root cause'\n"
            "The -p flag runs Claude in single-response print mode — no interactive session opened."
        ),
        "tags": ["pipe", "stdin", "git-diff", "logs", "review", "one-shot", "automation", "ci"],
    },
    {
        "id": "cc-opusplan",
        "title": "/model opusplan — Deep reasoning for architecture",
        "category": "slash-commands",
        "when_to_use": "complex architecture planning large refactor audit payments module race condition deep reasoning multi-file change",
        "example": "/model opusplan then: Audit the payments module for race conditions",
        "content": (
            "SKILL: opusplan mode\n"
            "Use /model opusplan (or --model opusplan flag) when you need Opus-level reasoning "
            "for planning, then automatic switch to Sonnet speed for execution. "
            "Best for: large refactors, security audits, architecture decisions, multi-file changes.\n"
            "CLI: claude --model opusplan 'Audit the entire payments module for race conditions, then fix them'"
        ),
        "tags": ["opus", "opusplan", "architecture", "planning", "reasoning", "refactor", "audit"],
    },
    {
        "id": "cc-custom-commands",
        "title": "Custom slash commands — reusable prompt templates",
        "category": "configuration",
        "when_to_use": "repetitive prompts team shared workflow standard review template always run same prompt automate recurring task",
        "example": "Create .claude/commands/review.md with your standard review checklist",
        "content": (
            "SKILL: Custom slash commands\n"
            "Create reusable prompt templates as Markdown files:\n"
            "  .claude/commands/review.md   → /review  (shared with team via git)\n"
            "  ~/.claude/commands/standup.md → /standup (personal, all projects)\n"
            "The file content becomes the prompt. Reference $ARGUMENTS for dynamic input.\n"
            "Example .claude/commands/pr-review.md:\n"
            "  Review the git diff for: security issues, missing tests, breaking changes, "
            "  performance problems. Output a checklist."
        ),
        "tags": ["custom-commands", "slash", "template", "reusable", "team", "workflow", "automation"],
    },
    {
        "id": "cc-hooks-pretool",
        "title": "PreToolUse hook — intercept tool calls before execution",
        "category": "hooks",
        "when_to_use": "prevent dangerous commands log every tool call enforce policy block specific bash commands audit what claude is doing",
        "example": "Block rm -rf in PreToolUse hook",
        "content": (
            "SKILL: PreToolUse hook\n"
            "Hooks in .claude/settings.json fire on Claude Code events and can block actions.\n"
            "PreToolUse runs before every tool call — use it to:\n"
            "  - Log what Claude is about to do\n"
            "  - Block dangerous commands (rm -rf, git push --force)\n"
            "  - Enforce org policies\n"
            "Example settings.json:\n"
            '  {"hooks": {"PreToolUse": [{"matcher": "Bash", "command": "echo Tool: $TOOL_NAME"}]}}\n'
            "Other hook events: PostToolUse, Stop, SubagentStop, SessionStart, SessionEnd, "
            "UserPromptSubmit (can modify prompt before Claude sees it), PreCompact, Notification."
        ),
        "tags": ["hooks", "pretooluse", "block", "audit", "policy", "automation", "settings"],
    },
    {
        "id": "cc-userpromptsubmit-hook",
        "title": "UserPromptSubmit hook — modify prompt before Claude sees it",
        "category": "hooks",
        "when_to_use": "automatically inject context into every prompt prepend project info add team conventions auto-add namespace",
        "example": "Prepend the current git branch and JIRA ticket to every prompt",
        "content": (
            "SKILL: UserPromptSubmit hook\n"
            "The UserPromptSubmit hook fires when you press Enter and can modify the prompt "
            "before Claude receives it. Use it to:\n"
            "  - Auto-inject current git context (branch, last commit)\n"
            "  - Prepend project conventions or CLAUDE.md snippets\n"
            "  - Add JIRA ticket context from the branch name\n"
            "The hook script receives the prompt on stdin and should print the modified prompt to stdout."
        ),
        "tags": ["hooks", "userpromptsubmit", "inject", "context", "auto", "prompt", "modify"],
    },
    {
        "id": "cc-session-resume",
        "title": "Session resumption — continue interrupted work",
        "category": "cli-flags",
        "when_to_use": "resume last session continue work reconnect interrupted session restore context pick up where left off",
        "example": "claude -c  (resume most recent)  or  claude -r SESSION_ID",
        "content": (
            "SKILL: Session resumption\n"
            "  claude -c           → resume the most recent session with full context\n"
            "  claude -r SESSION_ID → restore a specific session by ID\n"
            "  /status             → show current session ID (copy it to resume later)\n"
            "Session IDs persist in ~/.claude/projects/. Use -c to continue after closing the terminal."
        ),
        "tags": ["session", "resume", "continue", "-c", "-r", "context", "interrupted"],
    },
    {
        "id": "cc-permission-modes",
        "title": "Permission modes — control how Claude asks for approval",
        "category": "permissions",
        "when_to_use": "automate ci cd pipeline no prompts auto approve edits bypass permissions trusted environment",
        "example": "claude --permission-mode bypassPermissions (CI/CD only)",
        "content": (
            "SKILL: Permission modes\n"
            "  default         — asks before file edits and bash commands\n"
            "  plan            — requires plan approval before any modification\n"
            "  acceptEdits     — auto-approves file changes, asks for bash\n"
            "  dontAsk         — auto-approves most actions, blocks risky ones\n"
            "  bypassPermissions — skips all prompts (CI/CD pipelines only)\n\n"
            "CLI: claude --permission-mode acceptEdits\n"
            "Or in settings.json: {\"defaultMode\": \"acceptEdits\"}"
        ),
        "tags": ["permissions", "ci", "automation", "bypass", "approve", "mode", "pipeline"],
    },
    {
        "id": "cc-allowed-tools",
        "title": "Tool allowlists — surgical permission control",
        "category": "permissions",
        "when_to_use": "restrict claude to specific commands only allow git commands limit bash allow only read no write locked down",
        "example": "--allowedTools 'Bash(git:*)' 'Read'",
        "content": (
            "SKILL: Surgical tool permissions\n"
            "Restrict Claude to specific tools or command patterns:\n"
            "  --allowedTools 'Bash(git:*)' 'Read'     → only git commands + file reads\n"
            "  --allowedTools 'Bash(npm:*)' 'Edit'     → npm commands + file edits\n"
            "  --disallowedTools 'Bash'                → no shell access at all\n"
            "This is much safer than blanket tool access for automated scripts."
        ),
        "tags": ["allowedtools", "permissions", "restrict", "git", "bash", "security", "ci"],
    },
    {
        "id": "cc-subagent-explore",
        "title": "Explore subagent — fast codebase search without editing",
        "category": "subagents",
        "when_to_use": "search codebase find files locate symbol grep pattern discovery read-only investigation",
        "example": "Agent(subagent_type='Explore', prompt='Find all API endpoints in src/')",
        "content": (
            "SKILL: Explore subagent\n"
            "The Explore subagent is optimized for fast read-only codebase search. It can:\n"
            "  - Find files matching a glob pattern\n"
            "  - Grep for symbols, function names, or strings\n"
            "  - Answer 'where is X defined?' questions quickly\n"
            "Use it instead of reading files one by one — it's much faster for discovery.\n"
            "Limitation: reads excerpts, not full files. Don't use for deep analysis."
        ),
        "tags": ["explore", "subagent", "search", "grep", "codebase", "find", "locate"],
    },
    {
        "id": "cc-plan-mode",
        "title": "Plan subagent — get a strategy before executing",
        "category": "subagents",
        "when_to_use": "plan implementation design approach before coding get architecture review strategy complex task",
        "example": "Agent(subagent_type='Plan', prompt='Design the approach for this refactor')",
        "content": (
            "SKILL: Plan subagent\n"
            "Use the Plan subagent when you want a strategy before any code is written. "
            "It identifies critical files, considers trade-offs, and returns a step-by-step plan.\n"
            "Best for: large feature implementations, refactors, new service designs.\n"
            "The plan comes back as the result — you decide whether to execute it."
        ),
        "tags": ["plan", "subagent", "strategy", "architecture", "design", "before-coding"],
    },
    {
        "id": "cc-mcp-config",
        "title": "MCP server configuration — extend Claude Code with external tools",
        "category": "mcp",
        "when_to_use": "add database access connect external api add tools extend claude code capabilities custom tools memory server",
        "example": "Configure .mcp.json with database, memory, or API tool servers",
        "content": (
            "SKILL: MCP server integration\n"
            "Extend Claude Code with external tools via .mcp.json in your project root:\n"
            '  {"mcpServers": {"engram": {"type": "sse", "url": "http://localhost:8765/sse"}}}\n'
            "MCP servers can provide: database query tools, memory stores, external APIs, "
            "specialized code analysis, CI/CD integration, ticket systems.\n"
            "After adding a server, use /mcp to list available tools."
        ),
        "tags": ["mcp", "extend", "tools", "database", "api", "config", "server", "integration"],
    },
    {
        "id": "cc-claude-md-hierarchy",
        "title": "CLAUDE.md hierarchy — scoped instructions per directory",
        "category": "configuration",
        "when_to_use": "different rules per subdirectory frontend backend specific conventions override global settings component specific instructions",
        "example": "Create src/payments/CLAUDE.md with PCI-specific rules",
        "content": (
            "SKILL: CLAUDE.md hierarchy\n"
            "Instructions load in order — later overrides earlier:\n"
            "  ~/.claude/CLAUDE.md           → global defaults (all projects)\n"
            "  ./CLAUDE.md                   → repository-level rules\n"
            "  ./src/payments/CLAUDE.md      → subdirectory-specific rules\n"
            "  ./.claude/CLAUDE.local.md     → personal gitignored preferences\n\n"
            "Put PCI constraints in src/payments/CLAUDE.md so they only apply there. "
            "Put team conventions in the repo-level CLAUDE.md so everyone gets them."
        ),
        "tags": ["claude-md", "hierarchy", "scoped", "subdirectory", "config", "conventions"],
    },
    {
        "id": "cc-output-format-json",
        "title": "JSON output mode — machine-readable Claude output for scripts",
        "category": "cli-flags",
        "when_to_use": "script automation parse claude output ci cd pipeline structured output json format programmatic",
        "example": "claude -p 'analyze this code' --output-format json | jq .result",
        "content": (
            "SKILL: JSON output format\n"
            "  --output-format json        → single JSON object with result field\n"
            "  --output-format stream-json → newline-delimited JSON events (streaming)\n\n"
            "Use in CI/CD scripts:\n"
            "  claude -p 'check for security issues' --output-format json | jq '.result'\n"
            "  claude -p 'generate tests' --output-format stream-json  # streaming\n\n"
            "The -p flag (print mode) is required for scripting — no interactive session."
        ),
        "tags": ["json", "output", "script", "ci", "automation", "parse", "stream", "pipeline"],
    },
    {
        "id": "cc-max-turns",
        "title": "--max-turns — cap agentic iterations",
        "category": "cli-flags",
        "when_to_use": "limit how many steps claude takes prevent runaway agent cap iterations budget constrain autonomous tasks",
        "example": "claude --max-turns 5 'fix the failing tests'",
        "content": (
            "SKILL: --max-turns flag\n"
            "Limit the number of agentic turns (tool call cycles) Claude makes:\n"
            "  claude --max-turns 3 'apply this migration'\n"
            "Use it to: budget token usage, prevent runaway autonomous tasks, "
            "enforce human checkpoints in CI pipelines."
        ),
        "tags": ["max-turns", "limit", "budget", "agentic", "control", "iterations", "cap"],
    },
    {
        "id": "cc-vim-mode",
        "title": "/vim — Vim keybindings in Claude Code",
        "category": "slash-commands",
        "when_to_use": "vim keybindings vi navigation keyboard shortcuts vim user modal editing",
        "example": "/vim",
        "content": (
            "SKILL: /vim mode\n"
            "Enable vim-style keybindings in the Claude Code prompt with /vim. "
            "Supports normal mode (hjkl navigation, dd to delete line), insert mode, "
            "and visual mode. Toggle off with /vim again.\n"
            "Also set permanently in settings.json: {\"inputMode\": \"vim\"}"
        ),
        "tags": ["vim", "keybindings", "vi", "keyboard", "modal", "navigation"],
    },
    {
        "id": "cc-cost-tracking",
        "title": "/cost — track token usage and session spend",
        "category": "slash-commands",
        "when_to_use": "how much have i spent token usage cost monitoring budget awareness session cost",
        "example": "/cost",
        "content": (
            "SKILL: /cost command\n"
            "Run /cost at any point to see:\n"
            "  - Total tokens used in this session (input + output)\n"
            "  - Estimated cost in USD\n"
            "  - Session duration\n"
            "Also available as /cos. Use it after long tasks to understand your usage pattern."
        ),
        "tags": ["cost", "tokens", "budget", "usage", "monitoring", "spend"],
    },
    {
        "id": "cc-doctor",
        "title": "/doctor — diagnose Claude Code installation issues",
        "category": "slash-commands",
        "when_to_use": "claude code not working installation problem mcp not connecting debugging setup issue check health",
        "example": "/doctor",
        "content": (
            "SKILL: /doctor command\n"
            "Run /doctor to check your Claude Code installation health:\n"
            "  - API key validity\n"
            "  - MCP server connections\n"
            "  - Node version compatibility\n"
            "  - Config file locations\n"
            "Run this first when something seems wrong before debugging further."
        ),
        "tags": ["doctor", "diagnose", "health", "debug", "installation", "mcp", "troubleshoot"],
    },
    {
        "id": "cc-init",
        "title": "/init — auto-generate CLAUDE.md from codebase",
        "category": "slash-commands",
        "when_to_use": "new project setup generate claude md starting fresh onboard claude to existing codebase",
        "example": "/init",
        "content": (
            "SKILL: /init command\n"
            "Run /init in any project directory and Claude Code will:\n"
            "  1. Analyze the codebase structure\n"
            "  2. Detect the tech stack, build commands, and conventions\n"
            "  3. Generate a CLAUDE.md file tailored to the project\n\n"
            "Run this when adding Claude Code to an existing project. "
            "Then customize the generated CLAUDE.md with team-specific rules."
        ),
        "tags": ["init", "setup", "claude-md", "onboard", "new-project", "generate", "setup"],
    },
    {
        "id": "cc-parallel-agents",
        "title": "Parallel subagents — run multiple tasks simultaneously",
        "category": "subagents",
        "when_to_use": "run multiple independent tasks simultaneously parallel work speed up research write tests and implementation at same time",
        "example": "Send multiple Agent tool calls in one message to run them in parallel",
        "content": (
            "SKILL: Parallel subagent execution\n"
            "Claude Code can run multiple Agent tool calls simultaneously when sent in the same message. "
            "This is dramatically faster for independent tasks:\n"
            "  - Research two topics at once\n"
            "  - Write tests and implementation in parallel\n"
            "  - Search for multiple symbols across the codebase\n"
            "Key: all parallel Agent calls must be in a SINGLE response. "
            "Sequential messages run sequentially."
        ),
        "tags": ["parallel", "subagents", "simultaneous", "speed", "independent", "concurrent"],
    },
    {
        "id": "cc-worktree",
        "title": "Git worktrees for isolated agent work",
        "category": "subagents",
        "when_to_use": "agent work on separate branch without affecting current work isolated changes experimental safe sandbox",
        "example": "Agent(isolation='worktree', ...) — auto-creates git worktree",
        "content": (
            "SKILL: Agent worktree isolation\n"
            "Use isolation='worktree' when spawning agents to give them a separate git worktree. "
            "The agent works on an isolated copy of the repo — changes don't affect your working tree.\n"
            "Worktrees are auto-cleaned if the agent makes no changes. "
            "If changes are made, the worktree path and branch are returned for review.\n"
            "Best for: experimental refactors, risky changes, parallel agent work on same files."
        ),
        "tags": ["worktree", "isolation", "git", "branch", "safe", "sandbox", "agent"],
    },
]

# ---------------------------------------------------------------------------
# GitHub CLI capabilities
# ---------------------------------------------------------------------------

GH_CAPABILITIES: list[dict] = [
    {
        "id": "gh-pr-create",
        "title": "gh pr create — Open a pull request from the CLI",
        "category": "pull-requests",
        "when_to_use": "create pull request open PR submit code for review push branch propose changes merge request",
        "example": "gh pr create --title 'Fix auth bug' --body 'Resolves #42' --assignee @me",
        "content": (
            "SKILL: gh pr create\n"
            "Create a pull request without leaving the terminal:\n"
            "  gh pr create                            # interactive prompts\n"
            "  gh pr create --title '...' --body '...' # non-interactive\n"
            "  gh pr create --draft                    # draft PR\n"
            "  gh pr create --reviewer alice,bob       # assign reviewers\n"
            "  gh pr create --label bug --assignee @me\n"
            "Tip: run from the feature branch — gh auto-detects base."
        ),
        "tags": ["gh", "pr", "pull-request", "review", "github", "submit"],
    },
    {
        "id": "gh-pr-checkout",
        "title": "gh pr checkout — Check out a PR branch locally",
        "category": "pull-requests",
        "when_to_use": "checkout PR branch review someone else code test pull request locally switch to PR",
        "example": "gh pr checkout 123",
        "content": (
            "SKILL: gh pr checkout\n"
            "Switch to any open PR's branch in one command:\n"
            "  gh pr checkout 123          # by PR number\n"
            "  gh pr checkout my-feature   # by branch name\n"
            "Creates a local tracking branch automatically. "
            "Combine with: gh pr diff, gh pr review, gh pr merge."
        ),
        "tags": ["gh", "pr", "checkout", "review", "branch", "github"],
    },
    {
        "id": "gh-issue-create",
        "title": "gh issue create — File a GitHub issue from the CLI",
        "category": "issues",
        "when_to_use": "create issue file bug report open ticket report problem track work GitHub issue",
        "example": "gh issue create --title 'Login fails on Safari' --label bug",
        "content": (
            "SKILL: gh issue create\n"
            "File GitHub issues without a browser:\n"
            "  gh issue create                          # interactive\n"
            "  gh issue create --title '...' --body '...' --label bug\n"
            "  gh issue create --assignee @me --project 'Sprint 4'\n"
            "Use --web to finish in the browser if you need the full editor."
        ),
        "tags": ["gh", "issue", "bug", "ticket", "github", "create"],
    },
    {
        "id": "gh-run-watch",
        "title": "gh run watch — Stream CI output to your terminal",
        "category": "ci-cd",
        "when_to_use": "watch CI run stream workflow output monitor GitHub Actions check pipeline status wait for tests",
        "example": "gh run watch && gh run view --log",
        "content": (
            "SKILL: gh run watch\n"
            "Stream GitHub Actions output without opening a browser:\n"
            "  gh run watch                  # latest run on current branch\n"
            "  gh run watch 12345678         # specific run ID\n"
            "  gh run list                   # list recent runs\n"
            "  gh run view --log             # dump full logs after completion\n"
            "  gh run rerun --failed         # retry only failed jobs\n"
            "Combine with /loop: /loop every 60s gh run watch"
        ),
        "tags": ["gh", "ci", "actions", "workflow", "run", "watch", "github", "pipeline"],
    },
    {
        "id": "gh-search-code",
        "title": "gh search code — Search GitHub code from the CLI",
        "category": "search",
        "when_to_use": "search GitHub code find usage across repos look for examples pattern search codebase",
        "example": "gh search code 'MemberMatch' --repo myorg/myrepo --language java",
        "content": (
            "SKILL: gh search code\n"
            "Search code across GitHub without leaving the terminal:\n"
            "  gh search code 'pattern' --repo owner/repo\n"
            "  gh search code 'interface' --language typescript --limit 20\n"
            "  gh search repos 'fhir member-match' --language java\n"
            "  gh search issues 'auth timeout' --repo owner/repo --state open\n"
            "Returns file paths + snippets. Pipe to jq or claude for analysis."
        ),
        "tags": ["gh", "search", "code", "github", "find", "repos", "issues"],
    },
]

# ---------------------------------------------------------------------------
# Docker capabilities
# ---------------------------------------------------------------------------

DOCKER_CAPABILITIES: list[dict] = [
    {
        "id": "docker-compose-up",
        "title": "docker compose up — Start services with logs",
        "category": "compose",
        "when_to_use": "start services run docker compose bring up containers spin up local environment start all dependencies",
        "example": "docker compose up --build --watch",
        "content": (
            "SKILL: docker compose up\n"
            "Start your local stack:\n"
            "  docker compose up               # attach (logs in terminal)\n"
            "  docker compose up -d            # detached (background)\n"
            "  docker compose up --build       # rebuild images first\n"
            "  docker compose up --watch       # auto-rebuild on file change (Compose 2.22+)\n"
            "  docker compose up db redis      # start only specific services\n"
            "  docker compose logs -f api      # follow logs for one service"
        ),
        "tags": ["docker", "compose", "up", "services", "local", "dev", "containers"],
    },
    {
        "id": "docker-exec",
        "title": "docker exec — Run commands inside a running container",
        "category": "debugging",
        "when_to_use": "debug container shell into container inspect running container execute command inside container",
        "example": "docker exec -it my-api bash",
        "content": (
            "SKILL: docker exec\n"
            "Get a shell or run a one-off command in a running container:\n"
            "  docker exec -it <name> bash           # interactive shell\n"
            "  docker exec -it <name> sh             # when bash unavailable\n"
            "  docker exec <name> env                # dump environment\n"
            "  docker exec <name> cat /etc/hosts     # inspect files\n"
            "  docker exec <name> psql -U user db    # run DB commands\n"
            "Use 'docker ps' to find container names."
        ),
        "tags": ["docker", "exec", "shell", "debug", "container", "bash", "inspect"],
    },
    {
        "id": "docker-build-cache",
        "title": "docker build --cache-from — Speed up CI image builds",
        "category": "build",
        "when_to_use": "speed up docker build CI image build cache layers slow build registry cache",
        "example": "docker build --cache-from myrepo/api:latest -t myrepo/api:new .",
        "content": (
            "SKILL: docker build cache strategies\n"
            "Avoid rebuilding unchanged layers:\n"
            "  docker build --cache-from myrepo/api:latest -t myrepo/api:new .\n"
            "  DOCKER_BUILDKIT=1 docker build --build-arg BUILDKIT_INLINE_CACHE=1 .\n"
            "  docker build --target test-stage .\n"
            "Order Dockerfile instructions from least to most frequently changed."
        ),
        "tags": ["docker", "build", "cache", "ci", "layers", "speed", "buildkit"],
    },
    {
        "id": "docker-prune",
        "title": "docker system prune — Reclaim disk space",
        "category": "maintenance",
        "when_to_use": "free disk space clean up docker images volumes dangling layers out of space",
        "example": "docker system prune -af --volumes",
        "content": (
            "SKILL: docker system prune\n"
            "Reclaim disk space from stopped containers, dangling images, unused volumes:\n"
            "  docker system prune          # safe: stopped containers + dangling images\n"
            "  docker system prune -a       # also removes all unused images\n"
            "  docker system prune --volumes # also removes unused volumes (DATA LOSS)\n"
            "  docker image prune -a        # images only\n"
            "  docker volume prune          # volumes only\n"
            "  docker system df             # see how much space is used"
        ),
        "tags": ["docker", "prune", "cleanup", "disk", "space", "images", "volumes"],
    },
    {
        "id": "docker-inspect",
        "title": "docker inspect — Extract container or image metadata",
        "category": "debugging",
        "when_to_use": "inspect container metadata network IP port mapping environment variables image layers",
        "example": "docker inspect my-api | jq '.[0].NetworkSettings.IPAddress'",
        "content": (
            "SKILL: docker inspect + jq\n"
            "Pull structured metadata from containers or images:\n"
            "  docker inspect <container>                         # full JSON\n"
            "  docker inspect <container> | jq '.[0].State'\n"
            "  docker inspect <container> | jq '.[0].NetworkSettings.Ports'\n"
            "  docker inspect <image> | jq '.[0].Config.Env'\n"
            "  docker inspect --format '{{.State.Status}}' <c>   # Go template\n"
            "Combine with: docker stats, docker logs --tail 50"
        ),
        "tags": ["docker", "inspect", "metadata", "jq", "network", "ports", "debug"],
    },
]

# ---------------------------------------------------------------------------
# kubectl capabilities
# ---------------------------------------------------------------------------

KUBECTL_CAPABILITIES: list[dict] = [
    {
        "id": "kubectl-logs",
        "title": "kubectl logs — Stream pod logs with filtering",
        "category": "debugging",
        "when_to_use": "view pod logs stream kubernetes logs debug crash check errors follow logs container output",
        "example": "kubectl logs -f deploy/api --all-containers --since=5m | grep ERROR",
        "content": (
            "SKILL: kubectl logs\n"
            "Stream or dump logs from pods and deployments:\n"
            "  kubectl logs <pod>                      # dump logs\n"
            "  kubectl logs -f <pod>                   # follow (stream)\n"
            "  kubectl logs -f deploy/api              # by deployment\n"
            "  kubectl logs -f deploy/api --all-containers  # all containers in pod\n"
            "  kubectl logs -f <pod> --since=10m       # last 10 minutes\n"
            "  kubectl logs -f <pod> --previous        # crashed previous container\n"
            "Pipe to grep, jq, or claude for analysis."
        ),
        "tags": ["kubectl", "logs", "pod", "debug", "stream", "kubernetes", "k8s"],
    },
    {
        "id": "kubectl-exec",
        "title": "kubectl exec — Shell into a running pod",
        "category": "debugging",
        "when_to_use": "shell into pod debug kubernetes exec command inside container inspect k8s pod",
        "example": "kubectl exec -it deploy/api -- bash",
        "content": (
            "SKILL: kubectl exec\n"
            "Run commands inside a running Kubernetes pod:\n"
            "  kubectl exec -it <pod> -- bash\n"
            "  kubectl exec -it deploy/api -- bash           # by deployment\n"
            "  kubectl exec <pod> -- env | grep DB           # check env vars\n"
            "  kubectl exec <pod> -- curl -s localhost:8080/health\n"
            "  kubectl exec -it <pod> -c sidecar -- sh       # specific container\n"
            "Use kubectl get pods to find pod names."
        ),
        "tags": ["kubectl", "exec", "shell", "pod", "debug", "kubernetes", "k8s"],
    },
    {
        "id": "kubectl-rollout",
        "title": "kubectl rollout — Manage and roll back deployments",
        "category": "deployments",
        "when_to_use": "rollback deployment undo bad deploy restart pods rolling update deployment history kubernetes",
        "example": "kubectl rollout undo deploy/api",
        "content": (
            "SKILL: kubectl rollout\n"
            "Manage deployment rollouts and rollbacks:\n"
            "  kubectl rollout status deploy/api       # watch rollout progress\n"
            "  kubectl rollout history deploy/api      # show revision history\n"
            "  kubectl rollout undo deploy/api         # rollback to previous\n"
            "  kubectl rollout undo deploy/api --to-revision=3\n"
            "  kubectl rollout restart deploy/api      # rolling restart\n"
            "  kubectl set image deploy/api api=myrepo/api:v2  # deploy new image"
        ),
        "tags": ["kubectl", "rollout", "rollback", "deploy", "kubernetes", "k8s", "restart"],
    },
    {
        "id": "kubectl-port-forward",
        "title": "kubectl port-forward — Access cluster services locally",
        "category": "networking",
        "when_to_use": "access kubernetes service locally port forward debug service without ingress connect to database redis cluster",
        "example": "kubectl port-forward svc/postgres 5432:5432",
        "content": (
            "SKILL: kubectl port-forward\n"
            "Tunnel cluster services to your local machine:\n"
            "  kubectl port-forward svc/postgres 5432:5432    # service\n"
            "  kubectl port-forward pod/api-xyz 8080:8080     # specific pod\n"
            "  kubectl port-forward deploy/redis 6379:6379    # deployment\n"
            "Runs in foreground — use & to background or tmux.\n"
            "Combine with local tooling: psql, redis-cli, curl, Postman."
        ),
        "tags": ["kubectl", "port-forward", "tunnel", "debug", "local", "kubernetes", "k8s"],
    },
    {
        "id": "kubectl-context",
        "title": "kubectl config use-context — Switch clusters quickly",
        "category": "configuration",
        "when_to_use": "switch kubernetes cluster change context prod staging dev multiple clusters kubeconfig",
        "example": "kubectl config use-context prod-aks",
        "content": (
            "SKILL: kubectl context switching\n"
            "Manage multiple clusters without editing kubeconfig:\n"
            "  kubectl config get-contexts           # list all contexts\n"
            "  kubectl config current-context        # show active context\n"
            "  kubectl config use-context prod-aks   # switch to prod\n"
            "  kubectl config use-context staging    # switch to staging\n"
            "  kubectl config set-context --current --namespace=myteam  # set default ns\n"
            "Always verify context before kubectl apply in prod."
        ),
        "tags": ["kubectl", "context", "cluster", "kubeconfig", "switch", "kubernetes", "k8s"],
    },
]

# ---------------------------------------------------------------------------
# Pre-built catalog registry — maps tool name → capabilities list
# ---------------------------------------------------------------------------

TOOL_CAPABILITY_CATALOGS: dict[str, list[dict]] = {
    "claude-code": CLAUDE_CODE_CAPABILITIES,
    "gh": GH_CAPABILITIES,
    "docker": DOCKER_CAPABILITIES,
    "kubectl": KUBECTL_CAPABILITIES,
}
