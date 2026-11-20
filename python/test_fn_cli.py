"""Stdlib-only tests for the `fn` CLI parser (REQ-72 `fn types`)."""
import os
import unittest
from unittest import mock

import fn_cli
from fn_cli import build_parser


class TypesCommand(unittest.TestCase):
    def test_types_parses_function_id_lang_and_out(self):
        ns = build_parser().parse_args(["types", "12", "--lang", "go", "-o", "types.go"])
        assert ns.command == "types"
        assert ns.function_id == 12
        assert ns.lang == "go"
        assert ns.out == "types.go"
        assert ns.func.__name__ == "cmd_types"


class SpaceChoice(unittest.TestCase):
    """A function outside your default space is reached by naming its space: FNAI_SPACE."""

    def _client(self, **env):
        seen = mock.MagicMock()
        with mock.patch.object(fn_cli, "Client", return_value=seen), \
             mock.patch.object(fn_cli, "load_config", return_value={}), \
             mock.patch.dict(os.environ, {"FNAI_TOKEN": "fnp_x", **env}, clear=False):
            os.environ.pop("FNAI_SPACE", None) if "FNAI_SPACE" not in env else None
            fn_cli.get_client()
        return seen

    def test_a_named_space_is_used_for_every_call(self):
        self.assertEqual(self._client(FNAI_SPACE="Blog-Beaver").use_space.call_args.args, ("Blog-Beaver",))

    def test_with_no_space_named_nothing_changes(self):
        self._client().use_space.assert_not_called()


if __name__ == "__main__":
    unittest.main()
