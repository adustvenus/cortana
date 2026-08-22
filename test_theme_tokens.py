"""Tests for the colour tokens the dashboard pushes through the bridge.

The palette now travels: dashboard page -> bridge -> phone, and the phone
parses every value into a colour int. A malformed token there is not a cosmetic
bug - it either crashes the parse or paints an invisible UI on a device you
cannot open a console on. So the bridge is the choke point, and it is strict.

`bridge.state` pulls in the whole Cortana runtime at import time, which needs
the venv. These tests only exercise the token sanitiser, so it is loaded out of
the module source directly - that keeps them runnable anywhere, including the
dev box, which has no venv.
"""
import ast
import pathlib
import re
import unittest

_SRC = pathlib.Path(__file__).with_name("bridge") / "state.py"


def _load_sanitiser():
    """Exec just _THEME_KEYS and _clean_theme out of bridge/state.py."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    wanted = {"_THEME_KEYS", "_RGB_TRIPLE", "_clean_theme"}
    keep = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted for t in node.targets
        ):
            keep.append(node)
    ns = {"frozenset": frozenset, "re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<state>", "exec"), ns)
    return ns["_clean_theme"], ns["_THEME_KEYS"]


clean, THEME_KEYS = _load_sanitiser()

GOOD = {
    "--bg-rgb": "23,31,41",
    "--accent-rgb": "225,170,132",
    "--orb-hi-rgb": "253,227,200",
    "--orb-mid-rgb": "211,142,155",
    "--orb-rgb": "80,101,129",
}


class Accepts(unittest.TestCase):
    def test_a_normal_palette_survives_intact(self):
        self.assertEqual(clean(dict(GOOD)), GOOD)

    def test_every_allowed_token_survives(self):
        full = {k: "1,2,3" for k in THEME_KEYS}
        self.assertEqual(len(clean(full)), len(THEME_KEYS))


class TokenSetsAgree(unittest.TestCase):
    """The palette crosses three languages on its way to a phone:

        palette.js (emits)  ->  main.js (guards, for the bubble orb)
                            ->  bridge/state.py (guards, for the phone)

    Each keeps its own list of token names. A token added to the engine but
    missed in either guard is dropped silently, and the symptom - a phone or a
    bubble wearing half the theme - only appears on the Linux box, which is not
    reachable from where this code gets written. So the three lists are
    compared here instead.
    """

    ROOT = pathlib.Path(__file__).parent

    @staticmethod
    def _tokens(text, start_marker):
        """Token names quoted after `start_marker` in a JS source file."""
        tail = text[text.index(start_marker):]
        return set(re.findall(r"'(--[a-z0-9-]+-rgb)'", tail))

    def test_engine_matches_both_guards(self):
        engine_src = (self.ROOT / "Dashboard" / "package" / "palette.js").read_text(encoding="utf-8")
        shell_src = (self.ROOT / "Dashboard" / "app" / "main.js").read_text(encoding="utf-8")

        emitted = self._tokens(engine_src, "vars: {")
        guarded_by_shell = self._tokens(shell_src, "const THEME_KEYS")

        self.assertTrue(emitted, "found no tokens in palette.js - did the vars block move?")
        self.assertEqual(emitted, set(THEME_KEYS),
                         "palette.js and bridge/state.py disagree on the token set")
        self.assertEqual(emitted, guarded_by_shell,
                         "palette.js and Dashboard/app/main.js disagree on the token set")

    def test_the_page_defaults_cover_every_token(self):
        # applyTheme() merges defaults <- extracted <- pinned. A token absent
        # from THEME_DEFAULTS is never reset, so it keeps the PREVIOUS
        # background's colour after you pick a new one.
        page = (self.ROOT / "Dashboard" / "package" / "Dusk Dashboard.dc.html").read_text(encoding="utf-8")
        defaults = self._tokens(page[page.index("const THEME_DEFAULTS"):], "const THEME_DEFAULTS")
        self.assertEqual(defaults, set(THEME_KEYS),
                         "THEME_DEFAULTS is missing a token palette.js emits")

    def test_bounds_are_inclusive(self):
        self.assertEqual(clean({"--bg-rgb": "0,0,0"}), {"--bg-rgb": "0,0,0"})
        self.assertEqual(clean({"--bg-rgb": "255,255,255"}), {"--bg-rgb": "255,255,255"})

    def test_zero_padding_is_normalised(self):
        # Re-emitted from parsed ints, so padding cannot reach the phone.
        self.assertEqual(clean({"--bg-rgb": "007,031,041"}), {"--bg-rgb": "7,31,41"})


class Rejects(unittest.TestCase):
    def test_unknown_keys_are_dropped(self):
        out = clean({"--bg-rgb": "1,2,3", "--evil-rgb": "4,5,6", "background": "red"})
        self.assertEqual(out, {"--bg-rgb": "1,2,3"})

    def test_a_css_injection_attempt_is_not_a_colour(self):
        # These land in a style attribute on the phone and in the bubble's CSS.
        for bad in ("red;position:fixed", "0,0,0) url(http://x", "expression(1)",
                    "var(--x)", "1,2,3,4", "1,2", "",
                    # int() would accept all three of these; the regex must not.
                    # "1_0" is the nasty one - int() reads it as TEN.
                    "1, 2, 3", "+1,2,3", "1_0,2,3"):
            self.assertIsNone(clean({"--bg-rgb": bad}), bad)

    def test_out_of_range_channels(self):
        for bad in ("256,0,0", "-1,0,0", "0,0,999"):
            self.assertIsNone(clean({"--bg-rgb": bad}), bad)

    def test_non_string_and_non_dict_inputs(self):
        self.assertIsNone(clean({"--bg-rgb": [1, 2, 3]}))
        self.assertIsNone(clean({"--bg-rgb": None}))
        self.assertIsNone(clean(None))
        self.assertIsNone(clean("--bg-rgb: 1,2,3"))
        self.assertIsNone(clean([]))

    def test_all_bad_yields_none_not_empty(self):
        # None is what makes the bridge KEEP the last good palette; an empty
        # dict would be truthy-adjacent enough to invite a regression that
        # blanks the phone's theme whenever one bad push arrives.
        self.assertIsNone(clean({"--bg-rgb": "nope"}))


if __name__ == "__main__":
    unittest.main()
