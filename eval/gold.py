"""Hand-labeled extraction eval set — mixed domains (eng / legal / clinical).

Each item: text + session_date + gold entities (names) + gold facts (subject,
predicate, object). Predicates are open-vocab so scoring is lenient on the
predicate (subject+object carry the signal). Small but representative; the goal
is a directional accuracy signal on *your kind* of text, not a leaderboard.
"""

GOLD = [
    {
        "text": "Jane moved from the Litigation team to the M&A team in March.",
        "session_date": "2026-03-20",
        "entities": ["jane", "litigation team", "m&a team"],
        "facts": [("jane", "moved to", "m&a team"), ("jane", "was on", "litigation team")],
    },
    {
        "text": "We decided to use Postgres with pgvector for the memory engine.",
        "session_date": "2026-06-01",
        "entities": ["postgres", "pgvector", "memory engine"],
        "facts": [("memory engine", "uses", "postgres"), ("memory engine", "uses", "pgvector")],
    },
    {
        "text": "Dr. Lee prescribed amoxicillin to the patient yesterday.",
        "session_date": "2026-06-02",
        "entities": ["dr. lee", "amoxicillin", "patient"],
        "facts": [("dr. lee", "prescribed", "amoxicillin")],
    },
    {
        "text": "The Acme merger is handled by the M&A team. Bob is Jane's paralegal.",
        "session_date": "2026-04-10",
        "entities": ["acme", "m&a team", "bob", "jane"],
        "facts": [("acme merger", "handled by", "m&a team"), ("bob", "paralegal of", "jane")],
    },
    {
        "text": "The contract with Globex was signed on 2025-11-15 and expires in 2027.",
        "session_date": "2026-01-05",
        "entities": ["globex", "contract"],
        "facts": [("contract", "signed with", "globex"), ("contract", "expires", "2027")],
    },
    {
        "text": "Avoid Redis for session storage; it caused the outage last quarter.",
        "session_date": "2026-05-01",
        "entities": ["redis", "session storage"],
        "facts": [("redis", "prohibited for", "session storage"), ("redis", "caused", "outage")],
    },
    {
        "text": "Sarah is the lead architect and reports to the CTO, Mark.",
        "session_date": "2026-02-14",
        "entities": ["sarah", "cto", "mark"],
        "facts": [("sarah", "reports to", "mark"), ("sarah", "role", "lead architect")],
    },
    {
        "text": "The clinic switched its EHR from Epic to Cerner in January.",
        "session_date": "2026-01-31",
        "entities": ["clinic", "epic", "cerner"],
        "facts": [("clinic", "switched to", "cerner"), ("clinic", "used", "epic")],
    },
    {
        "text": "Project Phoenix depends on the auth-service, which Priya owns.",
        "session_date": "2026-03-03",
        "entities": ["project phoenix", "auth-service", "priya"],
        "facts": [("project phoenix", "depends on", "auth-service"), ("priya", "owns", "auth-service")],
    },
    {
        "text": "Client Wayne Corp requested a SaaS deployment, not on-prem.",
        "session_date": "2026-04-22",
        "entities": ["wayne corp", "saas"],
        "facts": [("wayne corp", "wants", "saas")],
    },
    {
        "text": "The deposition for the Smith case was rescheduled to next Friday.",
        "session_date": "2026-06-03",
        "entities": ["smith case", "deposition"],
        "facts": [("deposition", "for", "smith case")],
    },
    {
        "text": "Tom left the company; Nadia took over the billing module.",
        "session_date": "2026-05-19",
        "entities": ["tom", "nadia", "billing module"],
        "facts": [("nadia", "owns", "billing module"), ("tom", "left", "company")],
    },
]
