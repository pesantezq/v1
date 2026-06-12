"""
Rebalance policies for the backtest engine.

A policy decides (a) whether a rebalance is due on a date and (b) how to move
from current holding *values* toward target weights, given incoming cash. All
operate on per-ticker dollar values; the engine converts back to shares.
"""
from __future__ import annotations

from typing import Any


class RebalancePolicy:
    name = "base"

    def due(self, date: str, last_rebalance: str | None) -> bool:
        raise NotImplementedError

    def apply(self, holdings_value: dict[str, float], target: dict[str, float],
              date: str, cash_in: float) -> dict[str, float]:
        raise NotImplementedError


class BuyAndHold(RebalancePolicy):
    """Never rebalances; incoming cash is invested at current target weights."""
    name = "buy_and_hold"

    def due(self, date: str, last_rebalance: str | None) -> bool:
        return last_rebalance is None  # only the initial allocation

    def apply(self, holdings_value, target, date, cash_in):
        new = dict(holdings_value)
        if cash_in > 0:
            # Route new cash pro-rata to target weights (drift preserved otherwise).
            tw = {k: v for k, v in target.items() if v > 0}
            tot = sum(tw.values()) or 1.0
            for k, w in tw.items():
                new[k] = new.get(k, 0.0) + cash_in * (w / tot)
        return new


class Periodic(RebalancePolicy):
    """Rebalance to target weights on each month (default) boundary."""
    name = "periodic"

    def __init__(self, freq: str = "monthly"):
        self.freq = freq

    def due(self, date: str, last_rebalance: str | None) -> bool:
        if last_rebalance is None:
            return True
        if self.freq == "monthly":
            return date[:7] != last_rebalance[:7]
        if self.freq == "quarterly":
            return (date[:4], (int(date[5:7]) - 1) // 3) != \
                   (last_rebalance[:4], (int(last_rebalance[5:7]) - 1) // 3)
        return date[:4] != last_rebalance[:4]  # yearly

    def apply(self, holdings_value, target, date, cash_in):
        total = sum(holdings_value.values()) + cash_in
        tw = {k: v for k, v in target.items() if v > 0}
        tot = sum(tw.values()) or 1.0
        return {k: total * (w / tot) for k, w in tw.items()}


class ConfigRules(RebalancePolicy):
    """
    Operator's real rebalance_rules: only rebalance a position when it drifts
    beyond the band; prefer deploying incoming cash before selling.
    """
    name = "config_rules"

    def __init__(self, rebalance_rules: dict[str, Any] | None = None):
        rr = rebalance_rules or {}
        self.band = float(rr.get("band_threshold", 0.12))
        self.cash_first = bool(rr.get("use_cash_before_selling", True))

    def due(self, date: str, last_rebalance: str | None) -> bool:
        return last_rebalance is None or date[:7] != last_rebalance[:7]

    def apply(self, holdings_value, target, date, cash_in):
        new = dict(holdings_value)
        total = sum(new.values()) + cash_in
        tw = {k: v for k, v in target.items() if v > 0}
        tot = sum(tw.values()) or 1.0
        targets = {k: total * (w / tot) for k, w in tw.items()}

        # Deploy incoming cash toward the most-underweight names first.
        if cash_in > 0:
            gaps = sorted(((targets.get(k, 0.0) - new.get(k, 0.0), k) for k in tw),
                          reverse=True)
            remaining = cash_in
            for gap, k in gaps:
                if remaining <= 0 or gap <= 0:
                    break
                add = min(gap, remaining)
                new[k] = new.get(k, 0.0) + add
                remaining -= add
            if remaining > 0:  # leftover spread pro-rata
                for k, w in tw.items():
                    new[k] = new.get(k, 0.0) + remaining * (w / tot)

        # Only act on names drifted beyond the band; sell only if cash-first off
        # or there's no cash to close the gap.
        for k in list(new.keys()):
            tgt = targets.get(k, 0.0)
            cur = new.get(k, 0.0)
            drift = abs(cur - tgt) / total if total > 0 else 0.0
            if drift > self.band:
                if cur > tgt and self.cash_first and cash_in > 0:
                    continue  # avoided a sale by using cash this period
                new[k] = tgt
        return new


def make_policy(name: str, *, rebalance_rules: dict | None = None) -> RebalancePolicy:
    if name == "buy_and_hold":
        return BuyAndHold()
    if name == "periodic":
        return Periodic()
    if name == "config_rules":
        return ConfigRules(rebalance_rules)
    raise ValueError(f"unknown rebalance policy: {name}")
