from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_tree(file_name: str) -> ast.Module:
    return ast.parse((ROOT / file_name).read_text(encoding="utf-8-sig"), filename=file_name)


def class_methods(tree: ast.Module, class_name: str) -> dict[str, ast.FunctionDef]:
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    return {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}


class OicqApiContractTests(unittest.TestCase):
    """锁定插件侧声明的兼容合同；业务执行通过查询插件 mock 和 Bot 自身测试验证。"""

    def setUp(self) -> None:
        self.tree = source_tree("ica_typing.py")
        self.client = class_methods(self.tree, "IcaClient")
        self.message = class_methods(self.tree, "IcaNewMessage")

    def test_public_request_and_query_argument_order_and_defaults(self) -> None:
        # 位置参数顺序/None 占位直接影响 oicq Socket.IO 合同，timeout 不得挤占筛选参数。
        expected = {
            "new_message": (["room_id", "content", "reply"], ["", None]),
            "emit_event": (["event", "args"], [None]),
            "request_event": (["event", "args", "timeout"], [None, 15.0]),
            "get_loaded_messages": (["room_id", "limit"], [200]),
            "fetch_messages": (["room_id", "options", "timeout"], [None, 15.0]),
            "search_messages": (
                ["room_id", "keyword", "offset", "sender_id", "start_time", "end_time", "timeout"],
                [0, None, None, None, 15.0],
            ),
            "fetch_messages_by_sender": (
                ["room_id", "sender_id", "offset", "snapshot_time", "timeout"],
                [0, None, 15.0],
            ),
            "fetch_messages_around": (
                ["room_id", "message_id", "before", "after", "timeout"],
                [20, 20, 15.0],
            ),
            "fetch_image_messages": (["room_id", "offset", "end_time", "timeout"], [0, None, 15.0]),
            "get_system_messages": (["timeout"], [15.0]),
            "get_friends": (["timeout"], [15.0]),
            "get_groups": (["timeout"], [15.0]),
            "get_forward_messages": (["res_id", "file_name", "timeout"], [None, 15.0]),
            "send_merged_forward": (["nodes", "target", "dm", "origin"], [None, None]),
            "upload_file": (["file_name", "data"], []),
            "send_file": (
                ["room_id", "file_name", "data", "content", "mime", "reply", "at"],
                ["", "application/octet-stream", None, None],
            ),
            "upload_group_file": (["group_id", "file_name", "data", "parent"], ["/"]),
            "file_manager_request": (["group_id", "event", "args", "timeout"], [None, 15.0]),
            "send_raw_message": (["raw_msg", "room_id"], []),
        }
        for name, (arguments, defaults) in expected.items():
            with self.subTest(method=name):
                self.assertIn(name, self.client)
                method = self.client[name]
                self.assertEqual([arg.arg for arg in method.args.args], ["self", *arguments])
                self.assertEqual([ast.literal_eval(value) for value in method.args.defaults], defaults)
                self.assertEqual(method.args.kwonlyargs, [])
                self.assertIsNone(method.args.vararg)
                self.assertIsNone(method.args.kwarg)

    def test_query_results_are_consumable_messages_not_counts(self) -> None:
        for name in (
            "get_loaded_messages",
            "fetch_messages",
            "search_messages",
            "fetch_messages_by_sender",
            "fetch_messages_around",
            "fetch_image_messages",
            "get_forward_messages",
        ):
            with self.subTest(method=name):
                self.assertEqual(ast.unparse(self.client[name].returns), "list[IcaNewMessage]")
        for name in ("send_raw_message", "emit_event", "send_file", "send_merged_forward"):
            with self.subTest(method=name):
                self.assertEqual(ast.unparse(self.client[name].returns), "bool")
        self.assertEqual(ast.unparse(self.client["request_event"].returns), "JSON")
        self.assertEqual(ast.unparse(self.client["file_manager_request"].returns), "JSON")
        self.assertEqual(ast.unparse(self.client["new_message"].returns), "IcaSendMessage")

    def test_received_at_and_time_are_not_sending_mentions_or_display_strings(self) -> None:
        self.assertEqual(ast.unparse(self.message["at"].returns), "bool | Literal['all'] | None")
        self.assertEqual(ast.unparse(self.message["time"].returns), "int | None")
        self.assertEqual(ast.unparse(self.message["code"].returns), "JSON")
        self.assertEqual(ast.unparse(self.message["raw_msg"].returns), "JSON")
        self.assertEqual(ast.unparse(self.message["raw_data"].returns), "dict")
        self.assertEqual(ast.unparse(self.message["to_dict"].returns), "dict")
        self.assertEqual(ast.unparse(self.message["to_forward_node"].returns), "dict")
        self.assertEqual(ast.unparse(self.message["files"].returns), "list[dict]")
        send_at = class_methods(self.tree, "IcaSendMessage")["at"]
        self.assertEqual(ast.unparse(send_at.args.args[1].annotation), "list[dict]")
        merged_origin = self.client["send_merged_forward"].args.args[-1]
        self.assertEqual(ast.unparse(merged_origin.annotation), "int | None")

    def test_old_reply_and_delete_api_remains_available_to_existing_plugins(self) -> None:
        for field in ("reply_msg_id", "reply_msg_content", "reply_msg_sender_name", "reply_with", "as_deleted"):
            with self.subTest(field=field):
                self.assertIn(field, self.message)
        for name, args in (
            ("delete_msg_raw", ["self", "room_id", "message_id"]),
            ("report_read", ["self", "message_id"]),
            ("handle_request", ["self", "request_type", "flag", "accept"]),
        ):
            with self.subTest(method=name):
                self.assertEqual([arg.arg for arg in self.client[name].args.args], args)

    def test_new_hook_contract_is_client_first(self) -> None:
        hooks = {
            node.targets[0].id: node.value
            for node in self.tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        }
        expected = {
            "on_ica_message_updated": ["IcaClient", "int", "str", "dict"],
            "on_ica_bridge_event": ["IcaClient", "str", "List[JSON]"],
        }
        for hook, arguments in expected.items():
            with self.subTest(hook=hook):
                alias = hooks[hook]
                self.assertEqual(ast.unparse(alias.value), "Callable")
                self.assertEqual([ast.unparse(item) for item in alias.slice.elts[0].elts], arguments)
                self.assertEqual(ast.unparse(alias.slice.elts[1]), "None")

    def test_ica_api_version_is_separate_from_bridge_protocol(self) -> None:
        module = source_tree("shenbot_api.py")
        versions = {
            node.target.id: node.value
            for node in module.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        # 宿主发布版本与两个后端 API 分别管理，旧 _version_ 别名继续指向宿主版本。
        self.assertEqual(ast.literal_eval(versions["__version__"]), "0.9.5")
        self.assertEqual(ast.unparse(versions["_version_"]), "__version__")
        self.assertEqual(ast.literal_eval(versions["_ica_version_"]), "2.0.6")
        self.assertEqual(ast.literal_eval(versions["_tailchat_version_"]), "2.0.0")


if __name__ == "__main__":
    unittest.main()
