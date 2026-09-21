"""Vendor CLI adapters. Importing this package registers the built-in drivers."""

from .base import (DialogRules, Driver, driver_for_lane, driver_names,
                   get_driver, register_driver, validate_options)
from . import claude, codex, grok  # noqa: F401  (imported for registration)

__all__ = ["DialogRules", "Driver", "driver_for_lane", "driver_names",
           "get_driver", "register_driver", "validate_options"]
