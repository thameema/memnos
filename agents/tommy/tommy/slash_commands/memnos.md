# memnos — Quick Reference

Use this cheat sheet when you need to interact with memnos from Claude Code.

## Recall
```
recall("your query")                     # search current namespace
recall_wide("your query")                # search all readable namespaces
get_context("your query")               # formatted context block
get_entity("EntityName")                # entity + related facts
```

## Remember
```
remember("text", memory_type="fact")        # save a fact
remember("text", memory_type="decision")    # save a decision
remember("text", memory_type="constraint")  # save a pinned rule (always recalled)
```

## Corpus
```
corpus_ingest(name="doc-name", text="...", kind="doc")   # ingest architecture doc
corpus_check(snippet="code or task description")          # check against constraints
corpus_list()                                              # list ingested docs
```

## Leases (prevent duplicate work)
```
lease_acquire(key="mr:!79", holder_id="agent-session", ttl_seconds=1200)
lease_heartbeat(key="mr:!79", holder_id="agent-session")
lease_release(key="mr:!79", holder_id="agent-session")
```

## Episodes
```
segment_episodes()   # group turns into episodes
consolidate()        # distill episodes into durable facts
decay_episodes()     # age out old episode scores
```

## Pub/Sub
```
sub = namespace_subscribe()
namespace_feed(subscription_id=sub["subscription_id"])
```

## Namespaces (Health Chain)
| Namespace | Purpose |
|-----------|---------|
| `user:thameema:tommy` | Tommy personal journal |
| `org:hc:engineering` | Shared engineering knowledge |
| `org:hc` | Org-wide facts |
