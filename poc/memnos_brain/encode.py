"""B1 — write-time ENCODING pipeline (cheap, no-LLM by default).

Per message: append raw_turn → accumulate into an EVENT → on a boundary, flush the
event into `episodic` with timestamp, salience, embedding, entities (regex NER),
and co-mention edges. Event segmentation mirrors human boundary detection (Zacks):
a new event starts on session change, a large time gap, an embedding "surprise"
vs the current event, or when the event grows too long.

Entity extraction here is intentionally cheap (proper-noun regex); rich entity
resolution happens in B2 consolidation. Salience is a simple write-time heuristic.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .store import BrainStore

# crude proper-noun NER (no LLM). B2 consolidation refines/resolves entities.
_PROPER = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_STOP = {
    "The", "This", "That", "These", "Those", "There", "Then", "They", "Their", "Them",
    "And", "But", "For", "Not", "You", "Your", "Yours", "Our", "Out", "Are", "Was", "Were",
    "What", "When", "Where", "Which", "Who", "Why", "How", "Have", "Has", "Had", "Will",
    "Would", "Could", "Should", "Yeah", "Yes", "Hey", "Hi", "Hello", "Oh", "Okay", "Ok",
    "Sure", "Well", "Just", "Now", "Got", "Get", "Let", "Lot", "Some", "Any", "All", "One",
    "Two", "Day", "Days", "Week", "Month", "Year", "Time", "Thing", "Things", "Really", "Maybe",
    "Did", "Does", "Can", "May", "Might", "Must", "His", "Her", "Its", "Also", "Even", "Still",
    "Here", "Great", "Cool", "Wow", "Thanks", "Thank", "Nice", "Update", "See", "Awesome",
    "Good", "Glad", "Sounds", "Wonderful", "Amazing", "Right", "Sorry", "Congrats",
}


def extract_entities(text: str) -> list[str]:
    seen, out = set(), []
    for m in _PROPER.findall(text):
        if m in _STOP or m.lower() in seen:
            continue
        seen.add(m.lower()); out.append(m)
    return out


def salience(text: str, n_entities: int) -> float:
    """Write-time importance ~ entity density + length, squashed to [0,1]."""
    return 1.0 / (1.0 + math.exp(-(0.4 * n_entities + 0.002 * len(text) - 1.2)))


def _cos(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)


class Encoder:
    """Stateful per-namespace encoder. Feed turns in order; it flushes events."""

    def __init__(self, store: BrainStore, schema: str, ns: str, embed_fn,
                 gap_seconds: int = 6 * 3600, max_event_turns: int = 8, surprise: float = 0.55):
        self.store, self.schema, self.ns, self.embed = store, schema, ns, embed_fn
        self.gap, self.max_turns, self.surprise = gap_seconds, max_event_turns, surprise
        self._buf = []          # [(turn_id, speaker, text, vec, observed_at)]
        self._sid = None        # current session
        self._centroid = None   # running mean embedding of the event

    def ingest_turn(self, session_id, speaker, text, observed_at: datetime | None = None) -> int:
        observed_at = observed_at or datetime.now(timezone.utc)
        vec = self.embed(text)
        tid = self.store.insert_raw_turn(self.schema, self.ns, session_id, speaker, text, observed_at, vec)

        if self._buf and self._is_boundary(session_id, vec, observed_at):
            self.flush_event()
        self._buf.append((tid, speaker, text, vec, observed_at))
        self._sid = session_id
        # update running centroid
        if self._centroid is None:
            self._centroid = list(vec)
        else:
            n = len(self._buf)
            self._centroid = [(c * (n - 1) + v) / n for c, v in zip(self._centroid, vec)]
        if len(self._buf) >= self.max_turns:
            self.flush_event()
        return tid

    def _is_boundary(self, session_id, vec, observed_at) -> bool:
        if session_id != self._sid:
            return True
        last_at = self._buf[-1][4]
        if last_at and observed_at and (observed_at - last_at).total_seconds() > self.gap:
            return True
        if self._centroid is not None and (1.0 - _cos(vec, self._centroid)) > self.surprise:
            return True
        return False

    def flush_event(self) -> int | None:
        if not self._buf:
            return None
        turn_ids = [t[0] for t in self._buf]
        text = "\n".join(f"{spk}: {txt}" for _, spk, txt, _, _ in self._buf)
        t_start, t_end = self._buf[0][4], self._buf[-1][4]
        ents = extract_entities(text)
        sal = salience(text, len(ents))
        evec = self.embed(text)
        eid = self.store.insert_episodic(
            self.schema, self.ns, self._sid, text, t_start=t_start, t_end=t_end,
            observed_at=t_end, salience=sal, source_turn_ids=turn_ids, vec=evec)
        # entities + mentions + co-mention edges
        ent_ids = []
        for name in ents:
            enid = self.store.upsert_entity(self.schema, self.ns, name[:100],
                                            vec=self.embed(name))
            ent_ids.append(enid)
            self.store.add_mention(self.schema, enid, eid, "episodic")
        for i in range(len(ent_ids)):
            for j in range(i + 1, len(ent_ids)):
                self.store.bump_edge(self.schema, self.ns, ent_ids[i], ent_ids[j])
        self._buf = []
        self._centroid = None
        return eid

    def close(self):
        self.flush_event()
