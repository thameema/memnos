#!/usr/bin/env python3
"""
LoCoMo Benchmark Runner for memnos

Evaluates memnos memory system on the LoCoMo benchmark dataset
(ACL 2024, snap-research/locomo).

Usage:
    python3 benchmarks/locomo_runner.py \
        --url http://localhost:8766 \
        --key memnos-local-dev-key \
        --model claude-haiku-4-5-20251001 \
        --sample-ids all \
        --output benchmarks/results.json
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# LLM client setup — prefers `claude --print` (subscription), then API keys
# ---------------------------------------------------------------------------

def _claude_print_available() -> bool:
    """Return True if `claude --print` is available (Claude Code subscription)."""
    try:
        r = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_llm_client(force_api: bool = False):
    """Return (client_type, client).

    Priority:
      1. ANTHROPIC_API_KEY  (direct API — clean completion, no tool interference)
      2. OPENAI_API_KEY
      3. claude --print     (last resort — NOTE: claude --print runs with full
                             Claude Code system prompt + MCP tools, which causes
                             it to try using memnos MCP tools instead of answering
                             the prompt directly. Not suitable for benchmarking.)
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if anthropic_key:
        try:
            import anthropic
            print("LLM backend: Anthropic API (claude-haiku)", flush=True)
            return "anthropic", anthropic.Anthropic(api_key=anthropic_key)
        except ImportError:
            print("WARNING: anthropic SDK not installed; trying openai", file=sys.stderr)

    if openai_key:
        try:
            import openai
            print("LLM backend: OpenAI API", flush=True)
            return "openai", openai.OpenAI(api_key=openai_key)
        except ImportError:
            print("ERROR: neither anthropic nor openai SDK is installed.", file=sys.stderr)
            sys.exit(1)

    # Last resort: claude --print (not recommended for benchmarks — see docstring)
    if _claude_print_available():
        print("WARNING: using claude --print — answers may be incorrect due to tool interference.", flush=True)
        print("  Set ANTHROPIC_API_KEY for reliable benchmark results.", flush=True)
        return "claude-cli", None

    print(
        "ERROR: no LLM backend found.\n"
        "  Set ANTHROPIC_API_KEY or OPENAI_API_KEY",
        file=sys.stderr,
    )
    sys.exit(1)


def llm_complete(client_type, client, model: str, prompt: str) -> str:
    """Call LLM and return the text response."""
    if client_type == "claude-cli":
        # Use Claude Code's `claude --print` — no API key needed
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True, text=True, timeout=60,
        )
        return result.stdout.strip()

    if client_type == "anthropic":
        response = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    # openai
    response = client.chat.completions.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# memnos API helpers
# ---------------------------------------------------------------------------

def write_memory(
    http: httpx.Client,
    base_url: str,
    api_key: str,
    content: str,
    namespace: str,
    tags: list[str],
    retries: int = 3,
) -> dict:
    """POST /api/v1/memory/ — write a memory to memnos."""
    payload = {
        "content": content,
        "namespace": namespace,
        "memory_type": "fact",
        "tags": tags,
    }
    for attempt in range(retries):
        try:
            r = http.post(
                f"{base_url}/api/v1/memory/",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    return {}


def search_memories(
    http: httpx.Client,
    base_url: str,
    api_key: str,
    query: str,
    namespace: str,
    top_k: int = 5,
) -> list[dict]:
    """GET /api/v1/memory/search — search memnos memories."""
    params = {"q": query, "ns": namespace, "top_k": top_k}
    r = http.get(
        f"{base_url}/api/v1/memory/search",
        params=params,
        headers={"X-API-Key": api_key},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    # Handle both list response and {"results": [...]} envelope
    if isinstance(data, list):
        return data
    return data.get("results", data.get("memories", []))


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

def load_dataset(path_or_url: str) -> list[dict]:
    """Load locomo10.json from a local path or URL."""
    if path_or_url.startswith("http"):
        print(f"Downloading dataset from {path_or_url} …")
        with httpx.Client(follow_redirects=True, timeout=60) as http:
            r = http.get(path_or_url)
            r.raise_for_status()
            return r.json()
    else:
        with open(path_or_url) as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Core benchmark logic
# ---------------------------------------------------------------------------

def ingest_conversation(
    http: httpx.Client,
    base_url: str,
    api_key: str,
    sample: dict,
    sample_id: str,
    verbose: bool = False,
    force_ingest: bool = False,
    ns_prefix: str = "locomo",
) -> int:
    """Write all dialog turns for a sample into memnos. Returns turn count."""
    namespace = f"{ns_prefix}:{sample_id}"
    turns_written = 0

    # Guard: skip if already populated to avoid duplicates. --force-ingest overrides.
    if not force_ingest:
        try:
            check = http.get(f"{base_url}/api/v1/memory/search",
                             params={"q": "speaker", "ns": namespace, "top_k": 1},
                             headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
            if check.status_code == 200 and check.json():
                print(f"  Namespace {namespace} already populated — skipping (use --force-ingest to re-ingest)", flush=True)
                return 0
        except Exception:
            pass  # proceed with ingestion on error

    # conversation is a dict: {speaker_a, speaker_b, session_1, session_2, ...}
    conv = sample.get("conversation", {})
    # Collect session keys in order: session_1, session_2, ...
    session_keys = sorted(
        [k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
        key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
    )

    for session_idx, sess_key in enumerate(session_keys):
        turns = conv[sess_key]
        if not isinstance(turns, list):
            continue

        # Pull session date — stored as session_N_date_time in the dataset
        session_date = conv.get(f"{sess_key}_date_time", "")

        for turn in turns:
            if isinstance(turn, dict):
                speaker = turn.get("speaker", turn.get("role", "unknown"))
                text = turn.get("text", turn.get("content", ""))
            else:
                # plain string
                speaker = "unknown"
                text = str(turn)

            if not text:
                continue

            content = f"[{speaker}]: {text}"
            tags = ["locomo", f"session_{session_idx + 1}"]
            write_memory(http, base_url, api_key, content, namespace, tags)
            turns_written += 1

    if verbose:
        print(f"  Ingested {turns_written} turns into namespace {namespace}")
    return turns_written


def answer_question(
    http: httpx.Client,
    base_url: str,
    api_key: str,
    client_type: str,
    client,
    model: str,
    question: str,
    namespace: str,
    top_k: int = 5,
) -> tuple[str, list[dict]]:
    """Search memnos then call LLM to produce an answer. Returns (answer, results)."""
    results = search_memories(http, base_url, api_key, question, namespace, top_k)

    context_parts = []
    for r in results:
        content = r.get("content", r.get("memory", ""))
        if content:
            context_parts.append(content)

    context = "\n".join(context_parts) if context_parts else "(no relevant memories found)"

    # Route to appropriate prompt based on question type
    q_lower = question.lower().strip()
    is_date_question = q_lower.startswith("when ") or "what date" in q_lower or "how long ago" in q_lower
    is_inference_question = q_lower.startswith("would ") or q_lower.startswith("could ") or q_lower.startswith("is it likely")

    if is_date_question:
        # Strict: date questions need exact facts, no hallucination
        prompt = (
            f"You are answering questions about a multi-session conversation.\n\n"
            f"Retrieved memories:\n{context}\n\n"
            f"Question: {question}\n\n"
            f"Rules:\n"
            f"- Answer ONLY from the retrieved memories. Do not guess or hallucinate dates.\n"
            f"- If the answer contains a specific date, state it exactly as it appears.\n"
            f"- If the exact date is not in the memories, reply: 'I don't know.'\n"
            f"- Be concise — one sentence.\n\n"
            f"Answer:"
        )
    elif is_inference_question:
        # Inference: temporal/hypothetical questions need character reasoning
        prompt = (
            f"You are answering questions about a person based on what you know about them.\n\n"
            f"What we know about them:\n{context}\n\n"
            f"Question: {question}\n\n"
            f"Rules:\n"
            f"- Use the retrieved context to reason about the answer.\n"
            f"- These are hypothetical/inferential questions — reason from personality, interests, and values.\n"
            f"- Give a direct YES or NO answer with a brief explanation from the context.\n"
            f"- Be concise — one sentence.\n\n"
            f"Answer:"
        )
    else:
        # General: factual recall with light grounding
        prompt = (
            f"You are answering questions about a multi-session conversation.\n\n"
            f"Retrieved memories:\n{context}\n\n"
            f"Question: {question}\n\n"
            f"Rules:\n"
            f"- Answer from the retrieved memories. Make reasonable inferences when needed.\n"
            f"- Be concise and direct — one sentence.\n\n"
            f"Answer:"
        )

    answer = llm_complete(client_type, client, model, prompt)
    return answer, results


def judge_answer(
    client_type: str,
    client,
    model: str,
    question: str,
    expected: str,
    predicted: str,
) -> int:
    """LLM judge: returns 1 if correct, 0 if not."""
    prompt = (
        f"Question: {question}\n"
        f"Expected: {expected}\n"
        f"Predicted: {predicted}\n"
        f"Is the predicted answer correct? Reply with just YES or NO."
    )
    verdict = llm_complete(client_type, client, model, prompt).upper()
    return 1 if verdict.startswith("YES") else 0


def run_sample(
    http: httpx.Client,
    base_url: str,
    api_key: str,
    client_type: str,
    client,
    model: str,
    sample: dict,
    sample_id: str,
    verbose: bool = False,
    max_qa: int = 0,
    top_k: int = 20,
    ns_prefix: str = "locomo",
) -> list[dict]:
    """Run all QA pairs for one sample. Returns list of result records."""
    namespace = f"{ns_prefix}:{sample_id}"
    qa_results = []

    # QA pairs are stored under different keys depending on dataset version
    qa_pairs = sample.get("qa", sample.get("questions", []))
    if max_qa > 0:
        qa_pairs = qa_pairs[:max_qa]

    for qa in qa_pairs:
        question = qa.get("question", qa.get("q", ""))
        expected = qa.get("answer", qa.get("a", ""))
        raw_cat = qa.get("category", qa.get("type", "unknown"))
        category = CATEGORY_MAP.get(raw_cat, str(raw_cat))

        if not question or not expected:
            continue

        if verbose:
            print(f"    Q [{category}]: {question[:80]}")

        try:
            predicted, search_results = answer_question(
                http, base_url, api_key, client_type, client, model,
                question, namespace, top_k=top_k,
            )
            score = judge_answer(client_type, client, model, question, expected, predicted)
        except Exception as e:
            print(f"    ERROR on question: {e}", file=sys.stderr)
            predicted = ""
            search_results = []
            score = 0

        record = {
            "sample_id": sample_id,
            "category": category,
            "question": question,
            "expected": expected,
            "predicted": predicted,
            "score": score,
            "search_hits": len(search_results),
        }
        qa_results.append(record)

        if verbose:
            status = "CORRECT" if score else "wrong"
            print(f"      -> {status} | predicted: {predicted[:60]}")

    return qa_results


# ---------------------------------------------------------------------------
# Scoring and display
# ---------------------------------------------------------------------------

# LoCoMo category mapping: numeric → string (from paper)
CATEGORY_MAP = {1: "single_hop", 2: "multi_hop", 3: "temporal", 4: "open_domain", 5: "open_domain"}
KNOWN_CATEGORIES = ["single_hop", "multi_hop", "temporal", "open_domain"]


def compute_scores(results: list[dict]) -> dict[str, dict]:
    """Aggregate results by category."""
    by_cat: dict[str, list[int]] = {}
    for r in results:
        cat = r["category"]
        by_cat.setdefault(cat, []).append(r["score"])

    scores = {}
    all_scores = []
    for cat in KNOWN_CATEGORIES + sorted(set(by_cat) - set(KNOWN_CATEGORIES)):
        if cat in by_cat:
            correct = sum(by_cat[cat])
            total = len(by_cat[cat])
            scores[cat] = {"correct": correct, "total": total, "pct": 100 * correct / total}
            all_scores.extend(by_cat[cat])

    if all_scores:
        scores["OVERALL"] = {
            "correct": sum(all_scores),
            "total": len(all_scores),
            "pct": 100 * sum(all_scores) / len(all_scores),
        }
    return scores


def print_table(scores: dict[str, dict]) -> None:
    col_w = 14
    print()
    header = f"{'Category':<{col_w}} | {'Correct':>7} | {'Total':>5} | {'Score':>6}"
    print(header)
    print("-" * len(header))
    for cat, s in scores.items():
        sep = "=" * len(header) if cat == "OVERALL" else ""
        if sep:
            print(sep)
        print(
            f"{cat:<{col_w}} | {s['correct']:>7} | {s['total']:>5} | {s['pct']:>5.1f}%"
        )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

LOCOMO_URL = (
    "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
)


def parse_args():
    p = argparse.ArgumentParser(description="LoCoMo benchmark runner for memnos")
    p.add_argument("--url", default="http://localhost:8766", help="memnos base URL")
    p.add_argument("--key", default="memnos-local-dev-key", help="memnos API key")
    p.add_argument("--ns-prefix", default="locomo", help="Namespace prefix (default: locomo). Change to avoid reusing polluted namespaces.")
    p.add_argument(
        "--model",
        default="claude-haiku-4-5-20251001",
        help="LLM model for answering and judging",
    )
    p.add_argument(
        "--dataset",
        default=LOCOMO_URL,
        help="Path or URL to locomo10.json",
    )
    p.add_argument(
        "--sample-ids",
        default="all",
        help="Comma-separated sample indices (0-based) or 'all'",
    )
    p.add_argument(
        "--output",
        default="benchmarks/results.json",
        help="Path to write full results JSON",
    )
    p.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Skip memory ingestion (assume already loaded)",
    )
    p.add_argument("--top-k", type=int, default=10, help="Top-k search results")
    p.add_argument("--force-ingest", action="store_true", help="Re-ingest even if namespace already has memories")
    p.add_argument("--max-qa", type=int, default=0, help="Max QA pairs per sample (0=all). Use 30 for a quick representative run.")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    # Load dataset
    dataset = load_dataset(args.dataset)
    print(f"Dataset loaded: {len(dataset)} samples")

    # Resolve sample IDs
    if args.sample_ids == "all":
        indices = list(range(len(dataset)))
    else:
        indices = [int(x.strip()) for x in args.sample_ids.split(",")]

    # LLM client
    client_type, client = get_llm_client()
    print(f"LLM backend: {client_type}, model: {args.model}")

    all_results: list[dict] = []

    with httpx.Client() as http:
        for idx in indices:
            sample = dataset[idx]
            sample_id = str(sample.get("sample_id", sample.get("id", idx)))
            print(f"\n[{idx+1}/{len(indices)}] Sample {sample_id}")

            if not args.skip_ingest:
                n = ingest_conversation(
                    http, args.url, args.key, sample, sample_id, args.verbose,
                    force_ingest=args.force_ingest, ns_prefix=args.ns_prefix,
                )
                print(f"  Ingested {n} turns", flush=True)

            qa_results = run_sample(
                http, args.url, args.key,
                client_type, client, args.model,
                sample, sample_id, args.verbose,
                max_qa=args.max_qa, ns_prefix=args.ns_prefix,
                top_k=args.top_k,
            )
            all_results.extend(qa_results)
            print(f"  Answered {len(qa_results)} questions")

    # Score
    scores = compute_scores(all_results)
    print_table(scores)

    # Save
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "model": args.model,
        "memnos_url": args.url,
        "sample_count": len(indices),
        "scores": scores,
        "results": all_results,
    }
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Full results saved to {output_path}")


if __name__ == "__main__":
    main()
