"""Temporal query analysis (no LLM) — resolve WHAT TIME a question is about, so
retrieval can filter/sort by event time (valid_from) instead of similarity alone.

This is the mechanism vector search can't do: "what did X do in May 2023?" /
"what is X's CURRENT job?" / "what happened FIRST?" need time filtering + ordering,
not just cosine. Pure regex/dateutil — runs at query time with zero LLM.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

try:
    from dateutil.relativedelta import relativedelta
    def _months(n): return relativedelta(months=n)
    def _years(n): return relativedelta(years=n)
except ImportError:
    def _months(n): return timedelta(days=30 * n)
    def _years(n): return timedelta(days=365 * n)

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

_IS_TEMPORAL = re.compile(
    r"\b(when|what year|what month|what day|how long|how many (days|weeks|months|years)|"
    r"before|after|first|last|earliest|latest|recent|recently|ago|date|since|until|"
    r"current(ly)?|now|these days|nowadays|" + "|".join(MONTHS) + r")\b", re.I)
_CURRENT = re.compile(r"\b(current(ly)?|now|these days|nowadays|latest|most recent|still)\b", re.I)
_ORDER_FIRST = re.compile(r"\b(first|earliest|initial(ly)?|originally|begin)\b", re.I)
_ORDER_LAST = re.compile(r"\b(last|latest|most recent|final|recently)\b", re.I)
_YEAR = re.compile(r"\b(19|20)\d\d\b")
_REL = re.compile(r"\blast (week|month|year)\b|\b(yesterday|recently)\b", re.I)


_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_RE = re.compile(
    rf"\b(\d{{4}}-\d{{2}}-\d{{2}}|{_MON}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{_MON}\.?\s+\d{{4}}|{_MON}\.?\s+\d{{4}})\b", re.I)
_PROPER = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_QSTOP = {"What", "When", "Where", "Which", "Who", "Why", "How", "Did", "Does", "Has", "Have",
          "Was", "Were", "Is", "Are", "The", "Tell", "Give", "List"}


def parse_event_date(text, fallback=None):
    """#1 — materialize an ABSOLUTE event date from a fact's text (the extraction
    already resolved relative→absolute), so valid_from is the real event time, not
    just the session date. Falls back to `fallback` (session date)."""
    m = _DATE_RE.search(text)
    if m:
        try:
            from dateutil import parser as _p
            d = _p.parse(m.group(0), fuzzy=True, default=datetime(2000, 1, 1, tzinfo=timezone.utc))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return fallback


def query_entities(query):
    """Proper-noun entities in the question (the timeline anchors)."""
    seen, out = set(), []
    for m in _PROPER.findall(query):
        if m in _QSTOP or m.lower() in seen:
            continue
        seen.add(m.lower()); out.append(m)
    return out


# adjacent run of proper nouns (allowing one connector token like "of"/"and"/"the")
# so "Interoperability Gateway" / "Record ID Crosswalk" survive as ONE phrase.
_PHRASE = re.compile(r"\b[A-Z][a-zA-Z]{2,}(?:\s+(?:of|and|the|for|de)?\s*[A-Z][a-zA-Z]{2,})*\b")


def query_entity_phrases(query):
    """Multi-word proper-noun PHRASES in the question (e.g. 'Interoperability Gateway'),
    leading stop-word stripped. Unlike query_entities() these are NOT split into single
    tokens — so the #17 entity arm can match the whole subject phrase against a fact's
    entity binding instead of bridging two subjects on one shared generic token."""
    seen, out = [], []
    for m in _PHRASE.findall(query):
        toks = m.split()
        # drop a leading interrogative/article ("What", "The", ...) the regex may have caught
        while toks and toks[0] in _QSTOP:
            toks.pop(0)
        if not toks:
            continue
        phrase = " ".join(toks)
        key = phrase.lower()
        if key not in seen:
            seen.append(key); out.append(phrase)
    return out


def entity_match(query_ent: str, fact_ent: str) -> bool:
    """Whole-word / whole-phrase containment between a query entity and a fact entity
    (subject_entity or a mention), case-insensitive and trimmed. A match holds when one
    is contained in the other AS A WORD BOUNDARY-DELIMITED phrase — so 'Gateway' matches
    'gateway service' (same subject family) but a single shared GENERIC token cannot
    bridge two different subjects via free-text substring (that was the #17 no-op bug)."""
    q = (query_ent or "").strip().lower()
    f = (fact_ent or "").strip().lower()
    if not q or not f:
        return False
    if q == f:
        return True
    # whole-word containment in either direction (word-boundary anchored, not substring)
    big, small = (f, q) if len(f) >= len(q) else (q, f)
    return re.search(r"(?:^|\W)" + re.escape(small) + r"(?:\W|$)", big) is not None


class TemporalIntent:
    def __init__(self):
        self.temporal = False
        self.current = False          # "what is X's current ...": prefer valid_to IS NULL
        self.order = None             # 'asc' (first) | 'desc' (last)
        self.start = None             # event-time window lower bound
        self.end = None               # upper bound

    def __repr__(self):
        return (f"TemporalIntent(temporal={self.temporal} current={self.current} "
                f"order={self.order} window={self.start}..{self.end})")


def analyze(query: str, now: datetime | None = None) -> TemporalIntent:
    now = now or datetime.now(timezone.utc)
    t = TemporalIntent()
    q = query.lower()
    # explicit "<month> <year>" or month / year
    ym = _YEAR.search(query)
    year = int(ym.group(0)) if ym else None
    month = next((n for m, n in MONTHS.items() if re.search(rf"\b{m}\b", q)), None)

    # temporal if a keyword OR an explicit year/month is present
    if not (_IS_TEMPORAL.search(query) or year or month):
        return t
    t.temporal = True
    t.current = bool(_CURRENT.search(query))
    if _ORDER_FIRST.search(query):
        t.order = "asc"
    elif _ORDER_LAST.search(query):
        t.order = "desc"
    if year and month:
        t.start = datetime(year, month, 1, tzinfo=timezone.utc)
        t.end = t.start + _months(1)
    elif year:
        t.start = datetime(year, 1, 1, tzinfo=timezone.utc)
        t.end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    elif month:
        # nearest occurrence of that month at or before now
        y = now.year if month <= now.month else now.year - 1
        t.start = datetime(y, month, 1, tzinfo=timezone.utc)
        t.end = t.start + _months(1)

    # relative windows
    rel = _REL.search(query)
    if rel and not t.start:
        token = rel.group(0).lower()
        if "week" in token or token == "yesterday":
            t.start, t.end = now - timedelta(days=10), now
        elif "month" in token:
            t.start, t.end = now - _months(1), now
        elif "year" in token:
            t.start, t.end = now - _years(1), now
        elif "recently" in token:
            t.start, t.end = now - _months(1), now
    return t
