"""Tests-as-data contract runner.

The runner owns *all* assertions. Test intent lives in data files (CSV tables, YAML scenarios, generated
Gherkin, property definitions) under ``.factory/specs/<feature>/``; a thin, assertion-free driver
translates DSL ops into calls on the system's public interface.
"""

from .layout import SpecLayout

__all__ = ["SpecLayout"]
