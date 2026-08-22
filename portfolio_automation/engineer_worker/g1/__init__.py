"""G1 — supervisor measurement.

EW-0B answered "does the loop fail safely?". G1 answers a different and harder
question: "is the independent supervisor actually good at judging engineering
work?" Those must not be confused. A deterministic guard catching a
protected-path violation is excellent system evidence and says NOTHING about
GPT's judgement, because GPT was never called.

Everything here is ``experimental_noncanonical`` engineering measurement. It
defines no canonical Northstar contract, grants no authority, and changes no
capital, prediction or production semantics.
"""
from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

G1_SCHEMA_KIND = EXPERIMENTAL_MARKER
G1_NAMESPACE = "engineering.g1"

__all__ = ["G1_SCHEMA_KIND", "G1_NAMESPACE"]
