# memnos-constraint

Save a **constraint** (pinned rule) to memnos. Constraint memories are injected
into every future recall in this namespace — they govern behavior, not just describe it.

## Usage
```
/memnos-constraint <rule text>
```

## What to do
Call:
```python
remember(
    "$ARGUMENTS",
    memory_type="constraint",
)
```

Confirm to the user that the constraint has been saved and will apply to all future sessions.
