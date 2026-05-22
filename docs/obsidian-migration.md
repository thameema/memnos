# Migrating from Obsidian to engram

This guide walks through importing your existing Obsidian vaults into the engram knowledge graph so you can immediately start using engram with all of your accumulated notes, decisions, and context.

## What gets migrated

| Obsidian | engram |
|----------|--------|
| Markdown note | Memory entry (content + tags) |
| YAML frontmatter tags | Memory tags |
| Inline `#tags` | Memory tags |
| `[[Wikilink]]` | Graph edge: note A `references` note B |
| Folder structure | Sub-namespace (`vault:folder:subfolder`) |
| Frontmatter `title` / `date` | Memory metadata |

**What is not migrated:**
- Images and attachments (skipped — engram stores text)
- Canvas files (`.canvas`)
- Plugins and themes (`.obsidian/` folder is skipped automatically)
- Embedded file content (`![[file.png]]`)

---

## Prerequisites

```bash
pip install requests pyyaml
```

The script uses only these two packages and the Python standard library.

---

## Basic usage

```bash
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/my-vault \
  --namespace obsidian:my-vault \
  --api-key your-engram-api-key
```

This migrates every `.md` file in the vault into the `obsidian:my-vault` namespace (with sub-namespaces per folder), then creates graph edges for all `[[wikilinks]]`.

---

## Namespace mapping

The script maps your vault's folder hierarchy to engram namespaces automatically:

```
~/vaults/my-vault/
  note.md                      → obsidian:my-vault
  projects/
    backend.md                 → obsidian:my-vault:projects
    backend/
      auth-design.md           → obsidian:my-vault:projects:backend
  daily-notes/
    2026-05-21.md              → obsidian:my-vault:daily-notes
```

This means you can search within a specific area of your vault with `namespace: "obsidian:my-vault:projects"` or search the whole vault with `namespace: "obsidian:my-vault"`.

---

## Migrating multiple vaults

Run the script once per vault, each with its own namespace:

```bash
# Work vault
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/work \
  --namespace obsidian:work \
  --api-key your-key

# Personal vault
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/personal \
  --namespace obsidian:personal \
  --api-key your-key

# Research vault
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/research \
  --namespace obsidian:research \
  --api-key your-key
```

After migration, search across all vaults by using namespace prefixes. From Claude Code:
```
Use memory_search to find: "authentication design"
in namespace: "obsidian:work"
```

Or search everything including non-Obsidian memories:
```
Use memory_search to find: "authentication design"
in namespace: "personal:default"   ← your active working namespace
```

---

## Dry run first

Always run with `--dry-run` before the real migration to preview what will be imported:

```bash
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/my-vault \
  --namespace obsidian:my-vault \
  --api-key your-key \
  --dry-run
```

Output:
```
Found 847 notes in /Users/you/vaults/my-vault
DRY RUN — no writes to engram

Parsed 847 notes
Top tags: project(142), meeting(89), decision(67), architecture(45), todo(38)...
Total wikilinks: 2,341
```

---

## All options

```
--vault         Path to Obsidian vault directory (required)
--namespace     Target base namespace, e.g. obsidian:my-vault (required)
--api-key       engram API key (required)
--engram-url    engram REST API URL (default: http://localhost:8766)
--dry-run       Parse and report without writing anything
--batch-size    Notes to write per batch before a short pause (default: 10)
--folder        Only migrate notes under this subfolder (e.g. "projects")
--limit         Only migrate first N notes — useful for testing a small sample
--verbose       Print each note title as it is written
```

---

## Migrating a large vault (500+ notes)

For large vaults, migrate in batches by folder:

```bash
# Migrate the most important folder first
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/work \
  --namespace obsidian:work \
  --api-key your-key \
  --folder "architecture"

# Then the rest
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/work \
  --namespace obsidian:work \
  --api-key your-key \
  --folder "projects"
```

Or test with a small sample first:
```bash
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/work \
  --namespace obsidian:work \
  --api-key your-key \
  --limit 20 --verbose
```

---

## Verifying the migration

After migration, test from Claude Code:

```
Use memory_search to find: "your most distinctive note title or concept"
in namespace: "obsidian:my-vault"
```

Or use the engram dashboard at `http://localhost:8766/dashboard` — switch the namespace to `obsidian:my-vault` to see your notes in the knowledge graph visualization.

Or with curl:
```bash
curl -s \
  -H "Authorization: Bearer your-api-key" \
  "http://localhost:8766/api/v1/memory/search?q=architecture+decisions&ns=obsidian:my-vault&top_k=5" \
  | python3 -m json.tool
```

---

## After migration: integrating with your workflow

Once your vault is in engram, you do not need to maintain Obsidian as a separate system. Going forward:

1. **Write to engram directly from Claude Code** using `memory_write` instead of switching to Obsidian to take notes
2. **Search engram instead of Obsidian search** — engram's semantic search finds relevant context even when you don't use the exact words from the note
3. **Let engram capture automatically** — decisions, patterns, and discoveries from your Claude Code sessions are stored without any manual action
4. **Keep Obsidian for personal writing** if you prefer its editor — just run the migration script periodically to sync new notes into engram:

```bash
# Sync new notes added since last migration (safe to re-run — duplicate content
# will be added as new memories, so use --folder to limit scope on re-runs)
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/my-vault \
  --namespace obsidian:my-vault \
  --api-key your-key \
  --folder "new-notes"
```

---

## Troubleshooting

**"Connection refused" when running the script**

engram is not running. Start it with `engram start` and wait for the health check to pass.

**HTTP 401 Unauthorized**

Check your `--api-key` matches the key in `engram.yaml` under `auth.api_keys`.

**Many notes failing with HTTP 500**

Usually a Qdrant dimension mismatch from a previous embedding model. Run:
```bash
curl -X DELETE http://localhost:6333/collections/engram_vectors
engram restart
```
Then re-run the migration.

**Wikilinks not resolving (many "skipped" edges)**

Wikilinks are resolved by note title (case-insensitive). If your vault uses a different title in the frontmatter than the filename, the resolution may fail. Check with `--verbose` to see which links are not resolving, and verify the target note was imported successfully.

**Notes truncated**

Notes longer than 8000 characters are truncated (marked with `[truncated]`). Very long notes (meeting transcripts, research papers) may lose their tail content. Consider splitting very long notes in Obsidian before migrating.
