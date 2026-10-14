"""Contract: nova/brain/address.py -- strip_address(text, names).

Returns the command after a leading "Nova" / "Hey Nova" / "OK Nova", "" for
a bare name, None when the sentence is not addressed to NOVA. A false
accept here means NOVA acting on overheard speech, so the negative cases
(mention-not-address, near-words) matter as much as the positive ones.
"""
from __future__ import annotations

import pytest

from nova.brain.address import strip_address


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Nova, open Spotify", "open Spotify"),
        ("Nova open Spotify", "open Spotify"),
        ("nova open spotify", "open spotify"),
        ("NOVA. open notepad", "open notepad"),
        ("Nova! open notepad", "open notepad"),
        ("Nova: open notepad", "open notepad"),
        ("Nova - open notepad", "open notepad"),
        ("Nova -- open notepad", "open notepad"),
        ("Nova—open notepad", "open notepad"),
        ("  Nova,   open notepad  ", "open notepad"),
        ("Hey Nova what time is it", "what time is it"),
        ("Hey, Nova! what time is it", "what time is it"),
        ("hey nova, what time is it?", "what time is it?"),
        ("OK Nova, open notepad", "open notepad"),
        ("Okay Nova open notepad", "open notepad"),
        ("Hi Nova. what time is it", "what time is it"),
        ("Hello, Nova, open notepad", "open notepad"),
        ("Yo Nova open notepad", "open notepad"),
        ("Novah, open notepad", "open notepad"),  # documented Whisper spelling
    ],
)
def test_addressed_sentences_return_the_command(text, expected):
    assert strip_address(text, ["nova"]) == expected


def test_default_names_is_nova():
    assert strip_address("Nova, open Spotify") == "open Spotify"


def test_command_keeps_internal_punctuation_and_case():
    assert strip_address("Nova, what's 2+2? Tell me.", ["nova"]) == "what's 2+2? Tell me."


@pytest.mark.parametrize(
    "text",
    ["Nova", "Nova.", "nova,", "NOVA!", "Hey Nova", "Hey, Nova!", "OK Nova.", "  Nova ?  "],
)
def test_bare_name_returns_empty_string_not_none(text):
    result = strip_address(text, ["nova"])
    assert result == ""
    assert result is not None


@pytest.mark.parametrize(
    "text",
    [
        "I told Nova yesterday",
        "so I told him the meeting was moved",
        "supernova open",
        "Supernova, open notepad",
        "novel idea",
        "Novel idea, open notepad",
        "no va",
        "No va open notepad",
        "over there",
        "Novas open notepad",
        "Nova's battery died",
        "the nova is bright",
        "hey there nova open notepad",
        "hey hey nova open notepad",
        "Casanova open notepad",
    ],
)
def test_mention_or_near_word_is_not_an_address(text):
    assert strip_address(text, ["nova"]) is None


@pytest.mark.parametrize("text", ["", "   ", "\t\n", ",.!?", "--", "123 456"])
def test_empty_whitespace_or_wordless_returns_none(text):
    assert strip_address(text, ["nova"]) is None


@pytest.mark.parametrize("text", ["hey", "Hey.", "ok", "okay!", "hi", "hello", "yo"])
def test_greeting_alone_is_not_an_address(text):
    assert strip_address(text, ["nova"]) is None


def test_greeting_followed_by_other_word_is_not_an_address():
    assert strip_address("hey what time is it", ["nova"]) is None


def test_custom_names_list_replaces_the_default():
    assert strip_address("Jarvis, open notepad", ["jarvis"]) == "open notepad"
    assert strip_address("Hey Jarvis what time is it", ["jarvis"]) == "what time is it"
    # "nova" is not one of the configured names any more
    assert strip_address("Nova, open notepad", ["jarvis"]) is None


def test_multiple_custom_names_each_accepted():
    names = ["nova", "Friday"]
    assert strip_address("Friday, open notepad", names) == "open notepad"
    assert strip_address("nova open notepad", names) == "open notepad"
    assert strip_address("Monday open notepad", names) is None


def test_custom_names_are_case_and_whitespace_insensitive():
    assert strip_address("nova open notepad", ["  NOVA  "]) == "open notepad"


def test_unknown_custom_name_gets_no_variant_spellings():
    """Variants are only for names listed in the variant table -- a custom
    name must match exactly, not pick up nova's 'novah'."""
    assert strip_address("Jarvish open notepad", ["jarvis"]) is None
    assert strip_address("Novah open notepad", ["jarvis"]) is None


@pytest.mark.parametrize("names", [[], ["", "  "], ()])
def test_empty_names_never_matches(names):
    assert strip_address("Nova, open notepad", names) is None
