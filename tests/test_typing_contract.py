import ast
import pathlib
import unittest


class IcaTypingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = pathlib.Path(__file__).parents[1] / "ica_typing.py"
        cls.tree = ast.parse(source.read_text(encoding="utf-8"))
        cls.ica_status = next(
            node
            for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "IcaStatus"
        )

    def test_runtime_names_are_declared(self) -> None:
        names = {
            node.name
            for node in self.ica_status.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("heap_used", names)
        self.assertNotIn("head_used", names)
        self.assertIn("loaded_messages_count", names)
        self.assertIn("loaded_messages_count_for", names)

    def test_room_count_argument_is_present(self) -> None:
        method = next(
            node
            for node in self.ica_status.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "loaded_messages_count_for"
        )
        self.assertEqual([argument.arg for argument in method.args.args], ["self", "room_id"])


if __name__ == "__main__":
    unittest.main()
