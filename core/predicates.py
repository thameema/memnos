"""Predicate classification shared by the write path (core/service.py) and the
read-side contradiction signal (core/store.py — BrainStore.contradiction_summary).

Leaf module (stdlib only) so both can import it without a circular import (service.py
imports store.py). ONE definition of "is this (predicate, object) single-valued /
supersedable?" — the contradiction detector must never disagree with the write path
about which predicates can conflict (issue #156: it used to count additive predicates
like did_activity / includes / has as contradictions)."""
from __future__ import annotations

import re

# Belief-change supersession applies ONLY to SINGLE-VALUED attributes (a person has one
# current home/job/age — a new value replaces the old). MULTI-VALUED relations
# ('did_activity','met_person','visited','likes','owns') are ADDITIVE — a new martial art
# does NOT replace a previous one. Over-superseding list items corrupts aggregation +
# temporal recall, so default is ADDITIVE unless the predicate matches a single-valued cue.
_SINGLE_VALUED_CUES = ("live", "reside", "home", "based", "located", "current_",
                       "work_at", "works_at", "employ", "employer", "job_title", "occupation",
                       "role_at", "age", "marital", "married", "spouse", "status",
                       # work/ops functional attributes (field issue: ops facts were never
                       # superseded — the cue list was LoCoMo-personal-tuned). Each reviewed
                       # against multi-valued corruption: all describe ONE current state of a
                       # system/work item. did/visited/likes/met/owns stay additive.
                       "blocked",            # is_blocked_by / blocked_on — one current blocker state
                       "runs_on", "can_handle", "capacity", "deployed_", "version",
                       "recommended_action",
                       # value-attribute cues (field issue #10 retest: 'rate_limit' never
                       # superseded). Each reviewed against multi-valued corruption: all
                       # name ONE current scalar setting of a system. visited/did/met and
                       # the other additive relations stay out of this list.
                       "quota", "threshold", "timeout", "max_", "min_", "count_of")
# Exact-match-only cues: substring matching would be unsafe ('uses' is inside 'causes' /
# 'houses'; bare 'recommended' is inside plausibly multi-valued 'recommended_books').
_SINGLE_VALUED_EXACT = {"uses", "recommended", "recommendation"}
# Token-match-only cues: substring matching would be unsafe ('rate' is inside 'operates' /
# 'celebrates'; 'limit' is inside 'unlimited'), so these must match a whole _-separated
# predicate token ('rate_limit' → {'rate','limit'} → single-valued; 'operates_in' stays out).
_SINGLE_VALUED_TOKENS = {"rate", "limit"}

# QUANTIFIED-OBJECT default (issue #10 residual A, the safer general rule): same subject +
# IDENTICAL predicate + DIFFERENT object where the NEW object is a quantity ("200 rps",
# "100 requests per second", "$5,000", "30%") is a value UPDATE — single-valued by
# default regardless of the cue list. Guarded by _MULTI_VALUED_TOKENS: additive relations
# (visited/did/met/likes/owns...) must NEVER supersede, even with a quantified object
# ("ran 10 km" does not replace "ran 5 km" — separate events).
_QUANTIFIED_OBJ_RE = re.compile(
    r"^\s*(?:~|≈|<=|>=|<|>)?\s*[$€£]?\d[\d,.]*\s*(?:[a-zA-Z%/][\w\s/%.-]*)?$")
_MULTI_VALUED_TOKENS = {"visited", "visit", "did", "met", "meets", "likes", "liked",
                        "like", "owns", "own", "attended", "attends", "watched", "read",
                        "tried", "went", "ate", "played", "bought", "experienced",
                        "activity", "hobby", "enjoys"}


def _pred_tokens(pred: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (pred or "").lower()) if t}


def _is_single_valued(pred: str) -> bool:
    if not pred:
        return False
    p = pred.lower()
    return (p in _SINGLE_VALUED_EXACT or any(cue in p for cue in _SINGLE_VALUED_CUES)
            or bool(_SINGLE_VALUED_TOKENS & _pred_tokens(p)))


def _supersedable(pred: str, obj: str | None) -> bool:
    """Should a new (subject, pred, obj) fact close out the prior value for the same
    subject+predicate? True when the predicate matches a single-valued cue, OR — the
    general value-update rule — when the NEW object is quantified (number/unit pattern)
    and the predicate is not a known additive/multi-valued relation."""
    if not pred:
        return False
    if _is_single_valued(pred):
        return True
    return bool(obj and _QUANTIFIED_OBJ_RE.match(obj)
                and not (_pred_tokens(pred) & _MULTI_VALUED_TOKENS))
