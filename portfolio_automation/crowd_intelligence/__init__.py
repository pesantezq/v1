"""Probe-gated FMP Crowd Intelligence layer (observe-only).

Phase 1: capability registry + probe + persistence. Discovers which FMP endpoints
the current plan exposes; produces outputs/latest/fmp_endpoint_capabilities.json.
Never feeds decision_plan.json, scoring, allocation, or any trade execution.
"""
