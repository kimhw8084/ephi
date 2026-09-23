"""Regression for the governed Episode AnalysisWorkspacePage slot composition."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _calls(nodes: list[ast.stmt], name: str) -> list[ast.Call]:
    return [
        node
        for statement in nodes
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and _call_name(node) == name
    ]


class EpisodeWorkspaceGeometryTests(unittest.TestCase):
    def test_episode_heading_and_host_use_governed_header_and_primary_slots(self):
        tree = ast.parse((ROOT / "src/ephi/ui/app.py").read_text(encoding="utf-8"))
        builder = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_episode_page"
        )
        workspaces = [
            node
            for node in ast.walk(builder)
            if isinstance(node, ast.With)
            and any(_call_name(item.context_expr) == "AnalysisWorkspacePage" for item in node.items)
        ]
        self.assertEqual(len(workspaces), 1)
        workspace = workspaces[0]
        self.assertIsInstance(workspace.items[0].optional_vars, ast.Name)
        self.assertEqual(workspace.items[0].optional_vars.id, "page")

        slots = [node for node in workspace.body if isinstance(node, ast.With)]
        self.assertEqual(len(slots), 2)
        self.assertEqual(
            [_call_name(slot.items[0].context_expr) for slot in slots],
            ["page.slot", "page.slot"],
        )
        slot_names = [
            slot.items[0].context_expr.args[0].attr
            for slot in slots
        ]
        self.assertEqual(slot_names, ["HEADER", "PRIMARY"])

        header, primary = slots
        self.assertEqual(len(_calls(header.body, "_semantic_heading")), 1)
        self.assertTrue(any(isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "episode_host"
            for target in node.targets
        ) for node in primary.body))
        self.assertEqual(len(_calls(primary.body, "view.render_no_selection")), 1)
        self.assertEqual(len(_calls(primary.body, "view.load")), 1)
        self.assertTrue(any(
            isinstance(node, ast.If)
            and any(isinstance(child, ast.Return) for child in ast.walk(node))
            for node in primary.body
        ))
        self.assertTrue(any(
            isinstance(node, ast.Call)
            and _call_name(node) == "workspace.state.set"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value is None
            for node in ast.walk(ast.Module(body=primary.body, type_ignores=[]))
        ))


if __name__ == "__main__":
    unittest.main()
