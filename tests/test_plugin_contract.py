"""只检查会使插件无法被宿主发现或调用的入口错误。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins"
HOOK_ARITY = {
    "on_load": 0,
    "on_unload": 0,
    "on_ica_message": 2,
    "on_ica_system_message": 2,
    "on_ica_delete_message": 2,
    "on_ica_join_request": 2,
    "on_ica_message_updated": 4,
    "on_ica_bridge_event": 3,
    "on_tailchat_message": 2,
}


class PluginContractTests(unittest.TestCase):
    def test_enabled_plugins_have_unique_manifests_and_callable_hooks(self):
        identifiers = set()
        for path in sorted(PLUGIN_DIR.glob("*.py")):
            with self.subTest(plugin=path.name):
                tree = ast.parse(
                    path.read_text(encoding="utf-8-sig"), filename=str(path)
                )
                manifests = [
                    node.value
                    for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "PLUGIN_MANIFEST"
                        for target in node.targets
                    )
                ]
                self.assertEqual(len(manifests), 1, "宿主需要唯一的 PLUGIN_MANIFEST")
                self.assertIsInstance(manifests[0], ast.Call)
                identifier = next(
                    item.value
                    for item in manifests[0].keywords
                    if item.arg == "plugin_id"
                )
                identifier = ast.literal_eval(identifier)
                self.assertTrue(isinstance(identifier, str) and identifier)
                self.assertNotIn(identifier, identifiers, "重复 ID 会使插件被跳过")
                identifiers.add(identifier)
                for node in tree.body:
                    if isinstance(node, ast.FunctionDef) and node.name.startswith(
                        "on_"
                    ):
                        self.assertIn(node.name, HOOK_ARITY, "宿主不会调用未知 hook")
                        self.assertEqual(len(node.args.args), HOOK_ARITY[node.name])


if __name__ == "__main__":
    unittest.main()
