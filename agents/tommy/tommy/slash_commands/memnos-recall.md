# memnos-recall

Search memnos long-term memory for a topic. Falls back to `recall_wide` if
nothing relevant is found in the current namespace.

## Usage
```
/memnos-recall <topic>
```

## What to do
1. Call `recall("$ARGUMENTS")` in the current namespace.
2. If fewer than 2 results, call `recall_wide("$ARGUMENTS")` and note the source namespace.
3. Present the results, noting which namespace each came from.
4. If nothing found, say "No memories found for: $ARGUMENTS".
