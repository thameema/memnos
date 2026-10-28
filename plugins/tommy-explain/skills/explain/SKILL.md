---
name: explain
description: Explain code, a design, a bug, an error, a review finding or a trade-off in plain English with a concrete real-world scenario. Use when the user says "explain", "why", "what does this do", "I don't get it", "walk me through", "what could go wrong", or when a finding or decision needs to be understood by someone who has not read the code.
---

# Explain it so it sticks

The reader is smart and busy and has not read the code. Your job is that they understand in one pass and can decide what to do. Read the real code or output first; explain what is actually there, not what is typical.

## Shape of every explanation

1. **One-line answer first.** What it is, or what is wrong, in a sentence a non-author could repeat. No preamble.
2. **A real scenario.** Show it happening to someone or something concrete: a user, a request, a Tuesday-night deploy, two people clicking at once. Use real values from the code ("a customer with 3 items in the cart"), not "foo" and "bar". For a bug, tell the story of how it bites: who does what, what they see, what it costs.
3. **Why it works that way.** The one reason or trade-off behind it, in plain words. Define a term the first time you use it, in a few words, or avoid it.
4. **What to do.** The decision or next step, and what happens if they ignore it. If there are options, give a recommendation, not a survey.
5. **Where to look.** `file:line` for the real thing, so they can check you.

## Choosing the scenario

- Pick a situation from the reader's own world (their product, their users, their data), not a generic metaphor.
- An analogy is fine only when it fits in one line and maps onto the real mechanism. Say where the analogy stops being true if it matters.
- For risk, make it quantitative when you can: "at 50 requests a second this queue fills in about a minute".
- For a trade-off, show both sides with the same example so the difference is visible.

## Rules

- Short sentences. Concrete nouns. No filler, no "simply", no "basically".
- Do not flatter or hedge. If you are unsure, say what you checked and what you did not.
- Match depth to the question: a one-line question gets a short answer with the scenario in a sentence, not an essay.
- Never invent behaviour. If the code does not show it, say you did not verify it.
- Skip the scenario only when the question is purely factual (a flag name, a path).

## Example

Question: "why did the review flag this retry loop?"

> **It retries forever, so one outage can take the whole service down.** Picture the payment provider going down for ten minutes: every checkout request keeps retrying with no pause, each one holding a database connection, and within a minute the pool is empty. Now even the pages that don't touch payments time out. The fix is a cap of 3 tries with a growing pause between them, so a bad minute stays a bad minute. If you leave it, the first provider outage becomes your outage. (`billing/charge.py:88`)
