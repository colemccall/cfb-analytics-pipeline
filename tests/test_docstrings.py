"""The documentation rule in docs/CODING_STANDARDS.md §2, enforced.

Every module-level function or method in scripts/ and utils/ that is longer than
five lines, and every `main`, carries a docstring. Nested helpers are exempt —
they are read in the context of the function that holds them.

This exists because the rule was at 64% (180 of 282) before it was written
down, and a rule nobody checks drifts back. When this fails, the message names
each function; write the docstring, do not raise the threshold.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent
MAX_UNDOCUMENTED_LINES = 5


def _functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        elif isinstance(node, ast.ClassDef):
            yield from (n for n in node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))


def test_every_substantial_function_has_a_docstring():
    missing = []
    for path in sorted([*ROOT.glob("scripts/*.py"), *ROOT.glob("utils/*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in _functions(tree):
            size = fn.end_lineno - fn.lineno + 1
            if (size > MAX_UNDOCUMENTED_LINES or fn.name == "main") and not ast.get_docstring(fn):
                missing.append(f"{path.relative_to(ROOT)}:{fn.lineno} {fn.name} ({size} lines)")
    assert not missing, "undocumented functions:\n  " + "\n  ".join(missing)


def test_every_module_has_a_docstring():
    missing = [str(p.relative_to(ROOT))
               for p in sorted([*ROOT.glob("scripts/*.py"), *ROOT.glob("utils/*.py")])
               if p.name != "__init__.py"
               and not ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")))]
    assert not missing, f"modules with no docstring: {missing}"
