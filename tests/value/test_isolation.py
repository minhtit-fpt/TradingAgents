"""Mechanical enforcement of the isolation contract (plan section 2).

Subsystem 3 must not reach into the stock/crypto graphs. Reviewing imports by
eye does not survive contact with a year of edits; this test does.
"""

import ast
import unittest
from pathlib import Path

import tradingagents.value

VALUE_ROOT = Path(tradingagents.value.__file__).parent

# The only modules the value package may borrow from the rest of the repo.
IMPORT_ALLOWLIST = {
    "tradingagents.llm_clients.factory",
    "tradingagents.llm_clients.capabilities",
    "tradingagents.agents.utils.structured",
}


def _imported_modules(path: Path) -> set[str]:
    """Absolute module names imported by ``path``. Relative imports are internal."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


class IsolationTest(unittest.TestCase):
    def test_value_package_only_imports_allowlisted_repo_modules(self):
        offenders = []
        for path in sorted(VALUE_ROOT.rglob("*.py")):
            for module in _imported_modules(path):
                if not module.startswith("tradingagents."):
                    continue
                if module.startswith("tradingagents.value"):
                    continue
                if module in IMPORT_ALLOWLIST:
                    continue
                offenders.append(f"{path.name}: {module}")

        self.assertEqual(
            offenders,
            [],
            "value module imports outside the allowlist (plan section 2)",
        )

    def test_no_value_module_imports_the_graph_subsystems(self):
        banned = (
            "tradingagents.graph",
            "tradingagents.agents.analysts",
            "tradingagents.dataflows.interface",
        )
        for path in sorted(VALUE_ROOT.rglob("*.py")):
            for module in _imported_modules(path):
                for prefix in banned:
                    self.assertFalse(
                        module == prefix or module.startswith(prefix + "."),
                        f"{path.name} imports {module}, which belongs to subsystem 1/2",
                    )

    def test_value_package_uses_its_own_env_namespace(self):
        """VALUE_* only — a TRADINGAGENTS_* read here would couple the configs.

        Matches string literals rather than raw text, so prose that merely names
        the other namespace (as this module's own docstrings do) is not a hit.
        """
        for path in sorted(VALUE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertFalse(
                        node.value.startswith("TRADINGAGENTS_"),
                        f"{path.name} references {node.value!r}; the value module owns VALUE_*",
                    )


if __name__ == "__main__":
    unittest.main()
