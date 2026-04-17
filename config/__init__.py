"""Structured configuration support for the portfolio automation system."""

from config.loader import load_runtime_config_dict
from config.schema import ConfigValidationError

__all__ = [
    "ConfigValidationError",
    "load_runtime_config_dict",
]
