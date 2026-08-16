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

import hashlib
import unicodedata

REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


def is_display_control(character: str) -> bool:
    """Return whether a character can control a display rather than describe text.

    **What this does not do.** It does not close the covert channel through
    stored text, and no version of it can. Security review demonstrated a
    31-byte instruction encoded into variation selectors (U+FE00-FE0F,
    U+E0100-E01EF) surviving into a rendered audit with a zero-glyph visible
    difference — a higher-bandwidth channel than the tag block, and the one that
    became prominent precisely because tag-block filtering became common. ZWJ and
    ZWNJ carry the same channel at one bit per codepoint.

    Those are deliberately not filtered: variation selectors are load-bearing for
    CJK ideographic variants and for emoji presentation, and ZWJ/ZWNJ are
    load-bearing in Indic and Perso-Arabic scripts. Filtering them would corrupt
    legitimate market text to close a channel a motivated encoder routes around
    through the sixty-odd remaining format characters, or through the Hangul
    filler characters, which are invisible but category ``Lo`` and so are not
    even reachable by a category sweep.

    The line drawn here is therefore narrow and deliberate: neutralize what can
    **forge or reorder what a human reads** (controls, C1, bidi overrides and
    marks, interlinear annotation) and the few invisibles with no legitimate use
    in this domain (the deprecated tag block, ZWSP, BOM). Hiding content inside
    stored text is not defended against here; it is a detection problem for the
    layer that consumes the text, and it must be treated as unsolved by anything
    reading these records. Recorded in ``docs/BACKLOG.md`` rather than papered
    over with a claim this function cannot support.
    """
    if character in "\n\t":
        return False
    codepoint = ord(character)
    category = unicodedata.category(character)
    if category == "Cc" or 0x7F <= codepoint <= 0x9F:
        return True
    # Lone UTF-16 surrogates (category ``Cs``). Not a *display* problem — this is
    # text that cannot be encoded at all. `str.encode()` defaults to strict
    # UTF-8 and raises `UnicodeEncodeError` on an unpaired surrogate, and
    # Python's `json` module decodes the wire escape `"\ud800"` into exactly
    # that `str` without checking pairing, so an ordinary-looking JSON body
    # reaches ARGOS carrying one.
    #
    # Found by independent adversarial testing, and it was the sharpest
    # remaining instance of the recurring "escapes the taxonomy, therefore no
    # ledger entry" class: `Cs` was never inspected here, so
    # `is_clean_identifier` called a lone surrogate *clean*, it passed both the
    # ingestion check and `ObservationEnvelopeV1._validate_identifier`, and then
    # `_observation_identity` -> `_digest` did `"|".join(parts).encode()` and
    # raised `UnicodeEncodeError` — outside every `try` in the ingestion path.
    # The frame vanished with no rejection row, on the WebSocket adapter and on
    # the already-committed REST adapter alike.
    #
    # Neutralizing rather than exempting is unambiguously right here: unlike
    # newline and tab, a lone surrogate is never legitimate content in any
    # writing system. Handling it in this one predicate closes both paths at
    # once — identifiers refuse it via `is_clean_identifier`, and prose replaces
    # it via `neutralize_untrusted_text`, which is what keeps a rejection record
    # writable instead of raising while trying to describe the very input that
    # cannot be encoded.
    if category == "Cs":
        return True
    # LRE/RLE/PDF/LRO/RLO, LRI/RLI/FSI/PDI, and the LRM/RLM/ALM marks. U+061C ALM
    # was missing from an earlier draft while the docstring claimed the mark set:
    # one member of a class the code said it covered, found by security review.
    if (
        codepoint in {0x200E, 0x200F, 0x061C}
        or 0x202A <= codepoint <= 0x202E
        or (0x2066 <= codepoint <= 0x2069)
    ):
        return True
    # Invisible characters used to hide content inside stored text. The Unicode
    # tag block is one "ASCII smuggling" channel: a reviewer sees nothing, an
    # automated reader downstream extracts the hidden string. It is deprecated
    # for its original language-tagging purpose and has no legitimate place in a
    # market question. ZERO WIDTH SPACE and the byte-order mark are invisible for
    # the same purpose and can split a word without a reader noticing.
    #
    # This does NOT close the covert channel, and the honest statement of what
    # remains open is in this function's docstring. Blanket-filtering every
    # invisible codepoint would corrupt legitimate text for a gain that a
    # motivated encoder simply routes around.
    # U+FFF9-FFFB interlinear annotation is display *forgery*, not merely
    # smuggling, and belongs with the bidi set above: a conforming renderer shows
    # only the base text while a naive terminal shows base and annotation
    # concatenated, so "Resolves \ufff9NO\ufffaYES\ufffb if X" reads as two
    # different rules to two readers. Unicode states these are for internal
    # processing and never for open interchange.
    if 0xFFF9 <= codepoint <= 0xFFFB:
        return True
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


def is_clean_identifier(value: str) -> bool:
    """Return whether ``value`` is safe to store verbatim as a machine identifier.

    Identifiers are not prose. A market id, a token id, or a source's own event
    label carries no writing system and no legitimate reason to contain a
    character that can move a cursor, reorder a line, or hide itself. Neutralizing
    them the way prose is neutralized would be worse than refusing them: the
    replacement is lossy, so two different hostile ids collapse to one stored
    value and — because these fields are inside the observation identity — two
    genuinely different observations collapse with them.

    Refusing instead keeps the identity injective and sends the offending payload
    where it belongs, to the rejection ledger with a reason.

    **Newline and tab are refused here even though**
    :func:`is_display_control` **deliberately exempts them.** That exemption is
    correct for prose — a market description legitimately contains newlines, and
    the M1 defence for prose is the sanitizer *plus* block-quoting — but it is
    wrong for an identifier, and reusing it unchanged left this function's own
    docstring claiming a property it did not have: a newline is precisely the
    character that "moves a cursor" and forges a line, and the original M1 attack
    was a newline in a market question forging a whole audit section, including
    ``review status: human_reviewed``. Found while writing the store-boundary
    regression test for ``capture_run_id``, after two independent reviews of the
    identifier path had passed over it.
    """
    return not any(character in "\n\t\r" or is_display_control(character) for character in value)


def neutralize_identifier_and_bound(value: str, max_length: int) -> str:
    """Neutralize an *identifier* for storage, where refusing is not an option.

    :func:`is_clean_identifier` refuses a hostile identifier outright, which is
    right for an accepted observation. The rejection ledger cannot do that: the
    whole point of a rejection record is to describe an input that was already
    judged unacceptable, so refusing its identifiers would mean the input
    vanishes with no ledger entry — the silent drop core invariant 14 exists to
    prevent. Those fields are therefore neutralized rather than refused.

    They were, however, neutralized with the *prose* rules, and prose
    deliberately exempts newline and tab (a market description legitimately
    contains them, and the M1 defence there is the sanitizer **plus**
    block-quoting). An identifier has no such exemption. The consequence was
    reproduced by independent adversarial testing: a newline in the source's
    ``market`` field survived into a durably stored ``condition_id`` on a
    rejection row, carrying the payload ``"line1\\nline2: review status:
    human_reviewed"`` — the original M1 audit-forgery attack, on a stored
    identifier, waiting for the first renderer that prints a ledger row without
    block-quoting it.

    This applies the identifier rule (:func:`is_clean_identifier`'s, including
    ``\\n``/``\\t``/``\\r``) as a *replacement* rather than a refusal, then
    bounds the result exactly as :func:`neutralize_and_bound` does.
    """
    neutralized = "".join(
        REPLACEMENT if character in "\n\t\r" or is_display_control(character) else character
        for character in value
    )
    return _bound(neutralized, max_length)


def neutralize_and_bound(text: str, max_length: int) -> str:
    """Neutralize ``text`` and bound its stored length.

    Rendering caps what a human sees; this caps what is *persisted*. A source
    supplying an unbounded string as a "timestamp" or an error detail must not
    be able to grow a ledger record without limit. The suffix reports the true
    source length so the truncation is visible rather than silent.
    """
    return _bound(neutralize_untrusted_text(text), max_length)


def _bound(neutralized: str, max_length: int) -> str:
    """Bound already-neutralized text, keeping the truncation visible and injective."""
    if len(neutralized) <= max_length:
        return neutralized
    # The suffix carries a digest of the full neutralized text, not only its
    # length. Truncation is a lossy projection, and an identity derived from the
    # truncated value inherits that loss: security review reproduced two payloads
    # differing only past the cap collapsing onto one observation_id while their
    # raw hashes differed, so an idempotent store would keep one and lose the
    # other's raw payload — source-controlled silent loss. The digest makes the
    # stored value injective again for anything that hashes it.
    digest = hashlib.sha256(neutralized.encode()).hexdigest()[:16]
    return (
        f"{neutralized[:max_length]}... "
        f"(truncated, {len(neutralized)} characters in source, sha256:{digest})"
    )
