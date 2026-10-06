from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins"
SUPPORTED_HOOKS = {
    "on_load": (),
    "on_unload": (),
    "on_ica_message": ("IcaNewMessage", "IcaClient"),
    "on_ica_system_message": ("IcaNewMessage", "IcaClient"),
    "on_ica_delete_message": ("IcaType.MessageId", "IcaClient"),
    "on_ica_join_request": ("IcaJoinRequest", "IcaClient"),
    "on_tailchat_message": ("TailchatReciveMessage", "TailchatClient"),
}


def parse_source(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def assigned_value(tree: ast.Module, name: str) -> ast.expr | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                return node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node.value
    return None


def keyword_value(call: ast.Call, name: str) -> ast.expr | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


class PluginContractTests(unittest.TestCase):
    def active_plugins(self) -> list[Path]:
        return sorted(PLUGIN_DIR.glob("*.py"))

    def test_active_plugins_have_standard_manifest(self) -> None:
        plugin_ids: dict[str, Path] = {}
        for path in self.active_plugins():
            with self.subTest(plugin=path.name):
                tree = parse_source(path)
                future_annotations = any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "__future__"
                    and any(item.name == "annotations" for item in node.names)
                    for node in tree.body
                )
                self.assertTrue(future_annotations, "必须启用延迟注解")

                version = assigned_value(tree, "VERSION")
                self.assertIsInstance(version, ast.Constant, "必须定义字符串 VERSION")
                self.assertIsInstance(version.value, str)
                self.assertTrue(version.value)

                manifest = assigned_value(tree, "PLUGIN_MANIFEST")
                self.assertIsInstance(manifest, ast.Call, "必须定义 PLUGIN_MANIFEST")
                self.assertIsInstance(manifest.func, ast.Name)
                self.assertEqual(manifest.func.id, "PluginManifest")

                plugin_id = keyword_value(manifest, "plugin_id")
                name = keyword_value(manifest, "name")
                manifest_version = keyword_value(manifest, "version")
                authors = keyword_value(manifest, "authors")
                self.assertIsInstance(plugin_id, ast.Constant)
                self.assertIsInstance(plugin_id.value, str)
                self.assertTrue(plugin_id.value)
                self.assertIsInstance(name, ast.Constant)
                self.assertIsInstance(name.value, str)
                self.assertTrue(name.value)
                self.assertIsInstance(manifest_version, ast.Name)
                self.assertEqual(manifest_version.id, "VERSION")
                self.assertIsInstance(authors, (ast.List, ast.Tuple))
                self.assertGreater(len(authors.elts), 0)

                duplicate = plugin_ids.get(plugin_id.value)
                self.assertIsNone(
                    duplicate,
                    f"plugin_id {plugin_id.value!r} 与 {duplicate} 重复",
                )
                plugin_ids[plugin_id.value] = path

    def test_hooks_match_plugin_host_contract(self) -> None:
        for path in self.active_plugins():
            tree = parse_source(path)
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("on_"):
                    continue
                with self.subTest(plugin=path.name, hook=node.name):
                    self.assertIn(node.name, SUPPORTED_HOOKS, "存在宿主不会调用的 hook")
                    expected = SUPPORTED_HOOKS[node.name]
                    self.assertEqual(len(node.args.args), len(expected))
                    actual = tuple(
                        ast.unparse(argument.annotation)
                        if argument.annotation
                        else None
                        for argument in node.args.args
                    )
                    self.assertEqual(actual, expected)
                    self.assertIsNotNone(node.returns, "hook 必须标注 -> None")
                    self.assertEqual(ast.unparse(node.returns), "None")

    def test_ica_typing_is_only_imported_for_type_checking(self) -> None:
        for path in self.active_plugins():
            tree = parse_source(path)
            with self.subTest(plugin=path.name):
                direct_imports = [
                    node
                    for node in tree.body
                    if isinstance(node, ast.ImportFrom) and node.module == "ica_typing"
                ]
                self.assertEqual(direct_imports, [], "不能在运行时导入 ica_typing")
                for node in tree.body:
                    if (
                        isinstance(node, ast.If)
                        and isinstance(node.test, ast.Name)
                        and node.test.id == "TYPE_CHECKING"
                    ):
                        self.assertEqual(
                            node.orelse, [], "TYPE_CHECKING 不需要 TypeVar 回退"
                        )

    def test_plugins_with_raw_threads_have_unload_hook(self) -> None:
        for path in self.active_plugins():
            tree = parse_source(path)
            uses_thread = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "threading"
                and node.func.attr == "Thread"
                for node in ast.walk(tree)
            )
            hooks = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            with self.subTest(plugin=path.name):
                if uses_thread:
                    self.assertIn("on_unload", hooks)

    def test_declared_dependencies_cover_plugin_imports(self) -> None:
        import_to_distribution = {
            "PIL": "pillow",
            "psutil": "psutil",
            "psycopg": "psycopg",
            "pydantic": "pydantic",
            "requests": "requests",
            "selenium": "selenium",
            "tomli": "tomli",
        }
        declared = {
            line.strip().lower()
            for line in (ROOT / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        sources = [*PLUGIN_DIR.rglob("*.py"), *PLUGIN_DIR.glob("*.py_disable")]
        imported: set[str] = set()
        for path in sources:
            for node in ast.walk(parse_source(path)):
                if isinstance(node, ast.Import):
                    imported.update(item.name.split(".", 1)[0] for item in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".", 1)[0])
        required = {
            distribution
            for module, distribution in import_to_distribution.items()
            if module in imported
        }
        self.assertEqual(required - declared, set())

    def test_disabled_plugins_use_explicit_suffix_and_still_parse(self) -> None:
        self.assertEqual(list(PLUGIN_DIR.glob("*.pyi")), [])
        disabled = sorted(PLUGIN_DIR.glob("*.py_disable"))
        self.assertGreater(len(disabled), 0)
        for path in disabled:
            with self.subTest(plugin=path.name):
                parse_source(path)


if __name__ == "__main__":
    unittest.main()
