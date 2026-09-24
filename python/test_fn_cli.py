"""Stdlib-only tests for the `fn` CLI parser (REQ-72 `fn types`)."""
import unittest

from fn_cli import build_parser


class TypesCommand(unittest.TestCase):
    def test_types_parses_function_id_lang_and_out(self):
        ns = build_parser().parse_args(["types", "12", "--lang", "go", "-o", "types.go"])
        assert ns.command == "types"
        assert ns.function_id == 12
        assert ns.lang == "go"
        assert ns.out == "types.go"
        assert ns.func.__name__ == "cmd_types"


if __name__ == "__main__":
    unittest.main()
