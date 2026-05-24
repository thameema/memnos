"""
engram.decay.job — Memory decay job.

Scans namespaces for memories with non-none decay policies and marks
those that have exceeded their configured age/idle thresholds as
'deprecated'.

Usage (programmatic):
    from engram.decay.job import run_decay_job
    report = await run_decay_job(arcadedb_client, "org:my-team", dry_run=True)

Usage (CLI):
    engram-decay --namespace org:my-team [--dry-run] [--max-age-days 365]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AGE_DAYS  = 365   # time_weighted: deprecate after 1 year
_DEFAULT_MAX_IDLE_DAYS = 90    # access_weighted: deprecate after 90 days idle


@dataclass
class DecayReport:
    """Summary of a single decay job run."""
    namespace: str
    dry_run: bool
    time_weighted_deprecated: list[str] = field(default_factory=list)
    access_weighted_deprecated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_deprecated(self) -> int:
        return len(self.time_weighted_deprecated) + len(self.access_weighted_deprecated)


async def run_decay_job(
    arcadedb_client,
    namespace: str,
    *,
    dry_run: bool = False,
    max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    max_idle_days: int = _DEFAULT_MAX_IDLE_DAYS,
) -> DecayReport:
    """
    Scan ``namespace`` for memories with decay policies and mark stale ones
    as 'deprecated'.

    Parameters
    ----------
    arcadedb_client:
        ArcadeDBClient instance.
    namespace:
        The namespace to scan (includes ancestor namespaces via prefix).
    dry_run:
        If True, identify candidates but do NOT write any status changes.
    max_age_days:
        time_weighted memories older than this → deprecated.
    max_idle_days:
        access_weighted memories not accessed in this many days → deprecated.

    Returns
    -------
    DecayReport with lists of deprecated memory IDs.
    """
    report = DecayReport(namespace=namespace, dry_run=dry_run)
    now = datetime.now(timezone.utc)

    # --- time_weighted ---
    try:
        candidates = await arcadedb_client.get_decay_candidates(
            namespace, "time_weighted", limit=2000
        )
        to_deprecate = []
        for mem in candidates:
            created = mem.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = (now - created).total_seconds() / 86400
            if age_days > max_age_days:
                to_deprecate.append(mem.id)
                logger.info(
                    "decay: time_weighted memory %s age=%.0fd > %d → deprecate (dry=%s)",
                    mem.id, age_days, max_age_days, dry_run,
                )
        report.time_weighted_deprecated = to_deprecate
        if to_deprecate and not dry_run:
            await arcadedb_client.mark_deprecated_bulk(to_deprecate, namespace)
    except Exception as exc:
        msg = f"time_weighted scan failed: {exc}"
        logger.warning(msg)
        report.errors.append(msg)

    # --- access_weighted ---
    try:
        candidates = await arcadedb_client.get_decay_candidates(
            namespace, "access_weighted", limit=2000
        )
        to_deprecate = []
        for mem in candidates:
            ref = mem.last_accessed_at or mem.created_at
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=timezone.utc)
            idle_days = (now - ref).total_seconds() / 86400
            if idle_days > max_idle_days:
                to_deprecate.append(mem.id)
                logger.info(
                    "decay: access_weighted memory %s idle=%.0fd > %d → deprecate (dry=%s)",
                    mem.id, idle_days, max_idle_days, dry_run,
                )
        report.access_weighted_deprecated = to_deprecate
        if to_deprecate and not dry_run:
            await arcadedb_client.mark_deprecated_bulk(to_deprecate, namespace)
    except Exception as exc:
        msg = f"access_weighted scan failed: {exc}"
        logger.warning(msg)
        report.errors.append(msg)

    logger.info(
        "decay job complete: ns=%s total=%d (time=%d access=%d) dry=%s",
        namespace, report.total_deprecated,
        len(report.time_weighted_deprecated),
        len(report.access_weighted_deprecated),
        dry_run,
    )
    return report
