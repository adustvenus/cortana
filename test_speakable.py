"""Tests for voice/speakable.py - the TTS text normaliser.

The dangerous failure here is not a missed unit, it is a false positive: "in"
and "ft" are ordinary words, so anything that expands them outside a numeric
context would mangle every other sentence Cortana speaks. Half of these tests
exist to prove that does not happen.
"""
import unittest

from voice.speakable import speakable


class Units(unittest.TestCase):
    def test_the_reported_cases(self):
        self.assertIn("27 pounds", speakable("It weighs 27 lbs."))
        self.assertIn("27 inches", speakable('The monitor is 27" wide.'))

    def test_singular_vs_plural(self):
        self.assertIn("1 pound", speakable("Just 1 lb left."))
        self.assertIn("3 pounds", speakable("Just 3 lbs left."))
        self.assertIn("1 inch", speakable('1" of clearance'))

    def test_feet_and_inches_together(self):
        self.assertIn("6 feet 2 inches", speakable("6'2\" tall"))

    def test_currency_degrees_percent(self):
        self.assertIn("40 dollars", speakable("That's $40."))
        self.assertIn("1 dollar", speakable("$1 to start."))
        self.assertIn("degrees Fahrenheit", speakable("It is 72°F."))
        self.assertIn("degrees Celsius", speakable("It is 22°C."))
        self.assertIn("85 percent", speakable("Battery at 85%."))

    def test_symbols_and_abbreviations(self):
        self.assertIn("with milk", speakable("Coffee w/ milk"))
        self.assertIn("without sugar", speakable("Tea w/o sugar"))
        self.assertIn("and", speakable("R&D"))
        self.assertIn("number 42", speakable("See doc #42"))
        self.assertIn("about 30", speakable("~30 minutes"))
        self.assertIn("versus Go", speakable("Python vs. Go"))
        self.assertIn("for example", speakable("e.g. this"))


class DoesNotMangleProse(unittest.TestCase):
    """A unit only expands when a number precedes it."""

    def test_common_words_survive(self):
        for phrase in ("Check in at the front desk.",
                       "I left it in the car.",
                       "The meeting is in Boston.",
                       "Feet first, then hands.",
                       "Put it in the inbox."):
            self.assertEqual(speakable(phrase), phrase, phrase)

    def test_ambiguous_units_are_left_alone(self):
        # 5m is metres or million, 5g is grams or a network. Guessing is worse
        # than leaving the engine to decide.
        self.assertIn("5m", speakable("5m could go either way"))
        self.assertIn("5g", speakable("5g could go either way"))

    def test_unit_needs_a_number(self):
        self.assertEqual(speakable("lbs of pressure"), "lbs of pressure")


class Formatting(unittest.TestCase):
    def test_markdown_is_stripped(self):
        self.assertEqual(speakable("**Done**"), "Done")
        self.assertIn("Notes", speakable("# Notes"))

    def test_heading_hash_does_not_eat_a_number(self):
        self.assertIn("number 42", speakable("See #42"))

    def test_urls_reduce_to_the_host(self):
        self.assertIn("github.com", speakable("see https://github.com/a/b now"))
        self.assertNotIn("https", speakable("see https://github.com/a/b now"))

    def test_empty_and_none(self):
        self.assertEqual(speakable(""), "")
        self.assertEqual(speakable(None), "")

    def test_always_returns_a_string(self):
        self.assertIsInstance(speakable(12345), str)


if __name__ == "__main__":
    unittest.main()
