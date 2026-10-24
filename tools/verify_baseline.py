"""Compare original methods to a sanitized source baseline, using Python's AST.

    python tools/verify_baseline.py /path/to/baseline/plugin.py
"""
import ast
import sys
from pathlib import Path


def methods(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AffectionPlugin")
    return {node.name: ast.dump(node, include_attributes=False) for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def main():
    baseline = methods(sys.argv[1])
    current = methods(Path(__file__).resolve().parents[1] / "plugin.py")
    allowed = {"__init__", "on_load", "on_unload", "check_affection", "index_message_speaker"}
    changed = {name for name, body in baseline.items() if current.get(name) != body}
    assert changed <= allowed, f"Unexpected changes: {sorted(changed - allowed)}"
    print(f"PASS: {len(baseline) - len(changed)} original methods unchanged; adapter changes: {', '.join(sorted(changed))}")


if __name__ == "__main__":
    main()
