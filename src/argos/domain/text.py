"""Neutralization of source-authored text before it is stored or rendered.

Third-party text — a market question, a description, a source's own error
string — is written by whoever created it. Rendered raw it can clear a
reviewer's terminal, rewrite its title, or write to the clipboard via OSC 52,
so the reviewer would be reading an artifact the source controls. Bidirectional
overrides are the quieter version of the same attack: they visually reorder
text without changing it, so a clause can be made to read as its own opposite.
Both were found against the M1 audit renderer and are recorded in
``docs/05_RESEARCH_PROTOCOL.md``.

This lives in ``argos.domain`` because two layers need it and the domain is the
only one both may depend on: :mod:`argos.compiler.audit` renders it for a human,
and :mod:`argos.domain.observation` stores it in the rejection ledger. An
earlier draft duplicated the algorithm into the observation module on the
grounds that domain may not import compiler. That is true and irrelevant — the
legal direction is compiler to domain, which ``argos.compiler.audit`` already
uses. Two divergent copies of a security-relevant sanitizer with no parity test
is the drift the M1 security review exists because of.
"""

from __future__ import annotations

import unicodedata

REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


def is_display_control(character: str) -> bool:
    """Return whether a character can control a display rather than describe text."""
    if character in "\n\t":
        return False
    codepoint = ord(character)
    if unicodedata.category(character) == "Cc" or 0x7F <= codepoint <= 0x9F:
        return True
    # LRE/RLE/PDF/LRO/RLO, LRI/RLI/FSI/PDI, and the LRM/RLM marks.
    if (
        codepoint in {0x200E, 0x200F}
        or 0x202A <= codepoint <= 0x202E
        or (0x2066 <= codepoint <= 0x2069)
    ):
        return True
    # Invisible characters that hide content inside stored text rather than
    # forging what is displayed. The Unicode tag block is the "ASCII smuggling"
    # channel: a reviewer sees nothing, an automated reader downstream extracts
    # the hidden string. It is deprecated for its original language-tagging
    # purpose and has no legitimate place in a market question or a source's
    # error text. ZERO WIDTH SPACE and the byte-order mark are invisible for the
    # same purpose and can also split a word without a reader noticing.
    #
    # ZWJ (U+200D) and ZWNJ (U+200C) are deliberately NOT included: they are
    # load-bearing in Indic and Perso-Arabic scripts and in emoji sequences, and
    # cannot reorder or hide surrounding text. Neutralizing every Cf character
    # would corrupt legitimate text to close a channel these two do not open.
    return 0xE0000 <= codepoint <= 0xE007F or codepoint in {0x200B, 0xFEFF}


def neutralize_untrusted_text(text: str) -> str:
    """Replace display-controlling characters with a visible marker.

    Tabs and newlines survive. Everything neutralized is replaced rather than
    dropped, because a disappearing character is its own kind of forgery.
    Zero-width joiners are left alone: they are load-bearing in several writing
    systems and cannot reorder anything.
    """
    return "".join(
        REPLACEMENT if is_display_control(character) else character for character in text
    )


def neutralize_and_bound(text: str, max_length: int) -> str:
    """Neutralize ``text`` and bound its stored length.

    Rendering caps what a human sees; this caps what is *persisted*. A source
    supplying an unbounded string as a "timestamp" or an error detail must not
    be able to grow a ledger record without limit. The suffix reports the true
    source length so the truncation is visible rather than silent.
    """
    neutralized = neutralize_untrusted_text(text)
    if len(neutralized) <= max_length:
        return neutralized
    return f"{neutralized[:max_length]}... (truncated, {len(neutralized)} characters in source)"
