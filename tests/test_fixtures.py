"""Recorded fixtures must stay honest about what they are.

docs/13_TEST_STRATEGY.md requires every real fixture to declare its source
endpoint, retrieval date, redaction note, schema, raw hash, and parser version.
A fixture edited by hand without updating its metadata would quietly turn a
contract test into a test of the edit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REQUIRED_METADATA = frozenset(
    {
        "source",
        "source_endpoint",
        "payload_kind",
        "retrieved_at",
        "raw_sha256",
        "byte_length",
        "parser_version",
        "redaction",
        "note",
    }
)


def _raw_fixtures() -> list[Path]:
    return sorted(FIXTURES.rglob("*.raw.json"))


def test_the_fixture_directory_is_not_empty() -> None:
    assert _raw_fixtures(), "adapter contract tests need recorded payloads"


@pytest.mark.parametrize("raw_path", _raw_fixtures(), ids=lambda p: p.stem)
def test_every_recorded_payload_has_provenance(raw_path: Path) -> None:
    meta_path = raw_path.with_name(raw_path.name.replace(".raw.json", ".meta.json"))
    assert meta_path.exists(), f"{raw_path.name} has no .meta.json"

    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    missing = REQUIRED_METADATA - set(metadata)
    assert not missing, f"{meta_path.name} is missing {sorted(missing)}"

    raw = raw_path.read_bytes()
    assert metadata["raw_sha256"] == hashlib.sha256(raw).hexdigest(), (
        f"{raw_path.name} no longer matches the hash recorded in its metadata"
    )
    assert metadata["byte_length"] == len(raw)
    datetime.fromisoformat(str(metadata["retrieved_at"]).replace("Z", "+00:00"))


@pytest.mark.parametrize("raw_path", _raw_fixtures(), ids=lambda p: p.stem)
def test_recorded_payloads_carry_no_credentials(raw_path: Path) -> None:
    """Public endpoints only — nothing here should ever look like a secret."""
    text = raw_path.read_text(encoding="utf-8").lower()
    for token in ("authorization", "poly_api_key", "passphrase", "private_key", "secret"):
        assert token not in text, f"{raw_path.name} contains {token!r}"
