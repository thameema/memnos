# memnos-pin

Summarise the current exchange and pin it to memnos as a **decision** memory.
Use this at the end of a significant conversation to preserve its outcome.

## What to do
1. Write a 2-4 sentence summary of what was decided or accomplished in this session.
2. Call:
```python
remember(
    "<your summary>",
    memory_type="decision",
)
```
3. Confirm to the user: "Pinned to memnos: <summary>"
