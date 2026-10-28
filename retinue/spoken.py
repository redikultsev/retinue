"""How a transcript of the owner's voice enters a session, and how it is taken off again where his words become a
title, a commit subject or a pull request's title: the mark tells the agent a strange word may be misheard; it is not
part of what he said."""

from __future__ import annotations

SPOKEN_MARK = "(голосом) "                  # a voice note through the router (`bridge`)
VERBATIM_MARK = "(голосом, дословно) "      # English dictated in the app, word for word (`face.Face.speech`)
MARKS = (VERBATIM_MARK, SPOKEN_MARK)


def unmarked(text: str) -> str:
    """The owner's words without a voice mark at their start."""
    for mark in MARKS:
        if text.startswith(mark):
            return text[len(mark):]
    return text
