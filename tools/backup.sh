#!/usr/bin/env bash
# backup.sh — Consistent backup of engram ArcadeDB data.
#
# Strategy: brief container stop → rsync → restart.
# ArcadeDB WAL guarantees the copy is crash-consistent.
# Total downtime: ~15 seconds.
#
# Usage:
#   tools/backup.sh                            # backup to ~/.engram/backups/<timestamp>
#   tools/backup.sh /Volumes/External/engram   # backup to custom location
#   tools/backup.sh --verify                   # backup + verify record count matches
#
# Cron (daily at 2am):
#   0 2 * * * cd ~/git/engram && bash tools/backup.sh >> ~/.engram/backup.log 2>&1

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR="${ENGRAM_DATA_DIR:-$HOME/.engram}"
BACKUP_ROOT="${1:-$DATA_DIR/backups}"
KEEP_BACKUPS=7
COMPOSE_FILE="$(dirname "$0")/../docker-compose.yml"
ARCADEDB_URL="http://localhost:2480"
ARCADEDB_AUTH="root:${ARCADEDB_PASSWORD:-engram-dev-password}"
VERIFY=false

[[ "${1:-}" == "--verify" ]] && { VERIFY=true; BACKUP_ROOT="$DATA_DIR/backups"; }

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="$BACKUP_ROOT/$TIMESTAMP"

# ── Helpers ───────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

arcade_count() {
    curl -sf "$ARCADEDB_URL/api/v1/query/engram" \
        -H "Authorization: Basic $(printf '%s' "$ARCADEDB_AUTH" | base64)" \
        -H "Content-Type: application/json" \
        -d '{"language":"sql","command":"SELECT count(*) AS cnt FROM Memory"}' \
        2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['result'][0]['cnt'])" 2>/dev/null || echo "unknown"
}

# ── Pre-flight ────────────────────────────────────────────────────────────────
[[ -d "$DATA_DIR/arcadedb" ]] || die "ArcadeDB data directory not found: $DATA_DIR/arcadedb"
mkdir -p "$BACKUP_ROOT"

log "engram backup — $TIMESTAMP"
log "Source : $DATA_DIR/arcadedb"
log "Target : $BACKUP_DIR"

# ── Capture pre-backup count (if DB is running) ───────────────────────────────
PRE_COUNT=$(arcade_count)
log "Memory records before backup: $PRE_COUNT"

# ── Stop containers ───────────────────────────────────────────────────────────
log "Stopping engram and arcadedb containers..."
docker compose -f "$COMPOSE_FILE" stop engram arcadedb 2>/dev/null || true

# ── Copy data ─────────────────────────────────────────────────────────────────
log "Copying data..."
rsync -a --delete \
    "$DATA_DIR/arcadedb/" \
    "$BACKUP_DIR/arcadedb/"

# Also backup the SQLite sidecars (keys, tasks, learning)
for f in keys.db learning.db tasks.db; do
    [[ -f "$DATA_DIR/$f" ]] && cp "$DATA_DIR/$f" "$BACKUP_DIR/" && log "  + $f"
done

BACKUP_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
log "Backup size: $BACKUP_SIZE"

# ── Restart containers ────────────────────────────────────────────────────────
log "Restarting containers..."
docker compose -f "$COMPOSE_FILE" start arcadedb 2>/dev/null || true

# Wait for ArcadeDB to be healthy (up to 30s)
for i in $(seq 1 30); do
    if curl -sf "$ARCADEDB_URL/api/v1/ready" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

docker compose -f "$COMPOSE_FILE" start engram 2>/dev/null || true

# ── Verify (optional) ─────────────────────────────────────────────────────────
if [[ "$VERIFY" == "true" ]]; then
    sleep 5
    POST_COUNT=$(arcade_count)
    if [[ "$PRE_COUNT" == "$POST_COUNT" ]]; then
        log "Verify OK: $POST_COUNT memories (unchanged)"
    else
        log "WARNING: count changed — before=$PRE_COUNT after=$POST_COUNT"
    fi
fi

# ── Rotate old backups ────────────────────────────────────────────────────────
BACKUP_COUNT=$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
if (( BACKUP_COUNT > KEEP_BACKUPS )); then
    TO_DELETE=$(( BACKUP_COUNT - KEEP_BACKUPS ))
    log "Rotating: removing $TO_DELETE old backup(s) (keeping last $KEEP_BACKUPS)..."
    find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d | sort | head -"$TO_DELETE" | while read -r old; do
        rm -rf "$old"
        log "  removed: $(basename "$old")"
    done
fi

log "Done. Backup at: $BACKUP_DIR"
echo ""
echo "All backups:"
find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d | sort | while read -r d; do
    echo "  $(basename "$d")  $(du -sh "$d" | cut -f1)"
done
