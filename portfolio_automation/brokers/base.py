"""Read-only broker client abstraction (Phase 9, spec §5 optional delta).

A structural :class:`Protocol` capturing the **read-only** surface every broker
client must expose. It deliberately declares ONLY getters — there is no method
for placing/submitting/cancelling an order, moving money, or any broker write.
Future brokers conform to this Protocol; ``schwab_client.SchwabClient`` already
satisfies it.

This is an additive abstraction — it changes no existing broker behavior.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReadOnlyBrokerClient(Protocol):
    """The read-only broker surface. NO trade/order/write methods exist here.

    Any broker integration must implement exactly these getters. The absence of
    write methods is intentional and is enforced by tests (AST scan of the whole
    brokers package + a structural check that clients expose only read methods).
    """

    def get_account_numbers(self) -> Any:  # pragma: no cover - structural
        """Return account identifiers (masked downstream). Read-only."""
        ...

    def get_accounts(self, positions: bool = True) -> Any:  # pragma: no cover - structural
        """Return accounts + positions snapshot. Read-only."""
        ...


# The methods a conforming read-only client exposes (used by tests to assert the
# surface stays read-only as new brokers are added).
READ_ONLY_METHODS = ("get_account_numbers", "get_accounts")

# Tokens that must NEVER appear as a broker-client method name.
FORBIDDEN_METHOD_TOKENS = (
    "place_order", "submit_order", "cancel_order", "execute_trade", "buy", "sell",
    "place_trade", "modify_order", "move_money", "transfer", "withdraw", "deposit",
)
