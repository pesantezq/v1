"""
agent/__main__.py — Package entry point.

Enables running the agent as a module:
    py -m agent --mode daily|weekly|monthly
"""
from agent.agent_runner import main

main()
