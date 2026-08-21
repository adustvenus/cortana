"""Unit tests for audio_ducking.py's reference-counting and volume math. Stubs
subprocess.run so no real pactl/PulseAudio is needed. Run: python -m pytest test_audio_ducking.py
"""
import unittest
from unittest.mock import patch

import audio_ducking

SINK_INPUT_BLOCK = """Sink Input #42
    Driver: PipeWire
    Client: 7
    Sink: 0
    Volume: front-left: 45875 /  70% / -9.35 dB,   front-right: 45875 /  70% / -9.35 dB
            balance 0.00
    Properties:
        media.name = "Discover Weekly"
        application.name = "spotifyd"
"""

OTHER_APP_BLOCK = """Sink Input #7
    Driver: PipeWire
    Volume: front-left: 65536 / 100% / 0.00 dB
    Properties:
        application.name = "ffplay"
"""


def fake_run(cmd, **kwargs):
    result = unittest.mock.Mock()
    if cmd[:3] == ["pactl", "list", "sink-inputs"]:
        result.stdout = SINK_INPUT_BLOCK + OTHER_APP_BLOCK
    else:
        result.stdout = ""
    return result


class DuckTest(unittest.TestCase):
    def setUp(self):
        audio_ducking._active.clear()
        audio_ducking._saved.clear()

    def test_engage_ducks_only_matching_app(self):
        with patch("audio_ducking.DUCK_ENABLED", True), patch("audio_ducking.DUCK_FACTOR", 0.25), \
             patch("audio_ducking.subprocess.run", side_effect=fake_run) as run:
            audio_ducking.engage("speaking")
            set_calls = [c for c in run.call_args_list if c.args[0][0:2] == ["pactl", "set-sink-input-volume"]]
            self.assertEqual(len(set_calls), 1)                # only the spotifyd sink-input
            self.assertEqual(set_calls[0].args[0][2], "42")    # its id
            self.assertEqual(set_calls[0].args[0][3], "18%")   # round(70 * 0.25)

    def test_overlapping_reasons_duck_once_and_restore_once(self):
        with patch("audio_ducking.DUCK_ENABLED", True), patch("audio_ducking.subprocess.run", side_effect=fake_run) as run:
            audio_ducking.engage("speaking")
            audio_ducking.engage("ptt")             # overlapping: must not re-duck or re-save volume
            set_calls_after_double_engage = [c for c in run.call_args_list
                                              if c.args[0][0:2] == ["pactl", "set-sink-input-volume"]]
            self.assertEqual(len(set_calls_after_double_engage), 1)

            audio_ducking.release("speaking")       # one reason still active - must NOT restore yet
            set_calls_after_partial_release = [c for c in run.call_args_list
                                                if c.args[0][0:2] == ["pactl", "set-sink-input-volume"]]
            self.assertEqual(len(set_calls_after_partial_release), 1)

            audio_ducking.release("ptt")            # last reason cleared - restores to the ORIGINAL 70%
            restore_calls = [c for c in run.call_args_list
                              if c.args[0][0:2] == ["pactl", "set-sink-input-volume"]]
            self.assertEqual(len(restore_calls), 2)
            self.assertEqual(restore_calls[-1].args[0][3], "70%")

    def test_release_unknown_reason_is_a_noop(self):
        with patch("audio_ducking.DUCK_ENABLED", True), patch("audio_ducking.subprocess.run", side_effect=fake_run) as run:
            audio_ducking.release("never-engaged")
            run.assert_not_called()

    def test_disabled_never_touches_pactl(self):
        with patch("audio_ducking.DUCK_ENABLED", False), patch("audio_ducking.subprocess.run", side_effect=fake_run) as run:
            audio_ducking.engage("speaking")
            audio_ducking.release("speaking")
            run.assert_not_called()

    def test_no_matching_sink_input_is_a_quiet_noop(self):
        def fake_run_no_match(cmd, **kwargs):
            result = unittest.mock.Mock()
            result.stdout = OTHER_APP_BLOCK
            return result
        with patch("audio_ducking.DUCK_ENABLED", True), patch("audio_ducking.subprocess.run", side_effect=fake_run_no_match) as run:
            audio_ducking.engage("speaking")
            set_calls = [c for c in run.call_args_list if c.args[0][0:2] == ["pactl", "set-sink-input-volume"]]
            self.assertEqual(len(set_calls), 0)


if __name__ == "__main__":
    unittest.main()
