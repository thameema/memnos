"""Usage cost meter + hard budget cap.

Every LLM call routes through CostMeter.record(); it accumulates tokens + USD and
raises BudgetExceeded the moment a cap is crossed — so a run literally cannot
overspend (the v10 ~$1K safety rail). Mirrors the production `usage` ledger row.
"""
from __future__ import annotations

# USD per 1M tokens (input, output). Keep in sync with provider pricing.
PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}


class BudgetExceeded(RuntimeError):
    pass


class CostMeter:
    def __init__(self, budget_usd: float | None = None, verbose: bool = True):
        self.budget = budget_usd
        self.verbose = verbose
        self.cost = 0.0
        self.calls = 0
        self.by_op: dict[str, float] = {}

    def cost_of(self, model: str, in_tok: int, out_tok: int) -> float:
        pin, pout = PRICING.get(model, (0.0, 0.0))
        return in_tok / 1e6 * pin + out_tok / 1e6 * pout

    def record(self, op: str, model: str, in_tok: int, out_tok: int = 0) -> float:
        c = self.cost_of(model, in_tok, out_tok)
        self.cost += c
        self.calls += 1
        self.by_op[op] = self.by_op.get(op, 0.0) + c
        if self.budget is not None and self.cost > self.budget:
            raise BudgetExceeded(
                f"budget ${self.budget:.2f} exceeded: spent ${self.cost:.4f} after {self.calls} calls"
            )
        return c

    def summary(self) -> str:
        parts = "  ".join(f"{k}=${v:.4f}" for k, v in sorted(self.by_op.items()))
        cap = f" / ${self.budget:.2f} cap" if self.budget else ""
        return f"spent ${self.cost:.4f}{cap} over {self.calls} calls   [{parts}]"
