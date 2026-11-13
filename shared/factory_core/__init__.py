"""Shared core of the AI software factory.

Everything that must be identical between the gh-aw factory and the LangGraph factory lives here:
the tests-as-data contract runner (``tad_runner``), the anti-cheat guard (``guard``), the deterministic
verification gates (``gates``), mutation testing for Python (``mutation``), coding-agent adapters
(``agents``) and report rendering (``reports``).
"""

__version__ = "0.1.0"
