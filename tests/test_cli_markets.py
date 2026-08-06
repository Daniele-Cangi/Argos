"""The discovery and audit commands, driven end to end against a mocked Gamma."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import orjson
import pytest
import respx
from typer.testing import CliRunner

from argos.cli import app

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gamma"
BASE = "https://gamma-api.polymarket.com"
runner = CliRunner()


@pytest.fixture(name="page")
def page_fixture() -> bytes:
    return (FIXTURES / "markets_list.raw.json").read_bytes()


@pytest.fixture(name="single")
def single_fixture() -> bytes:
    return (FIXTURES / "market_by_id.raw.json").read_bytes()


# --- discover ---------------------------------------------------------------------


@respx.mock
def test_discover_reports_the_whole_sample(page: bytes) -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=page))
    result = runner.invoke(app, ["markets", "discover", "--limit", "3", "--json"])
    assert result.exit_code == 0, result.output

    report = orjson.loads(result.stdout)
    assert report["returned"] == report["normalized"] + report["quarantined"]
    assert report["selected"] <= report["normalized"]
    assert report["raw_sha256"]
    assert "policy" in report


@respx.mock
def test_discover_records_the_policy_it_applied(page: bytes) -> None:
    """A sample without its policy cannot be interpreted later."""
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=page))
    result = runner.invoke(app, ["markets", "discover", "--min-liquidity", "5000", "--json"])
    assert result.exit_code == 0
    assert orjson.loads(result.stdout)["policy"]["min_liquidity"] == "5000"
    assert orjson.loads(result.stdout)["policy"]["require_binary"] is True


@respx.mock
def test_discover_accounts_for_markets_it_could_not_read(page: bytes) -> None:
    broken = json.loads(page)
    broken[0]["clobTokenIds"] = '["1", "2", "3"]'
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, content=orjson.dumps(broken))
    )
    result = runner.invoke(app, ["markets", "discover", "--json"])
    report = orjson.loads(result.stdout)
    assert report["quarantined"] == 1
    assert report["quarantine_reasons"] == {"quarantined_mapping": 1}


@respx.mock
def test_discover_prints_a_human_summary(page: bytes) -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=page))
    result = runner.invoke(app, ["markets", "discover"])
    assert result.exit_code == 0
    assert "normalized" in result.stdout
    assert "quarantined" in result.stdout
    assert "selected" in result.stdout


@respx.mock
def test_discover_fails_loudly_when_gamma_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # One attempt: the CLI runs on a LiveClock, so a full retry budget here would
    # buy nothing but real backoff seconds.
    monkeypatch.setenv("ARGOS_HTTP_MAX_ATTEMPTS", "1")
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(500))
    result = runner.invoke(app, ["markets", "discover"])
    assert result.exit_code == 1
    assert "argos.source_unavailable" in result.output


@respx.mock
def test_a_non_numeric_liquidity_floor_is_refused(page: bytes) -> None:
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=page))
    result = runner.invoke(app, ["markets", "discover", "--min-liquidity", "lots"])
    assert result.exit_code == 2
    assert "decimal" in result.output


@respx.mock
def test_a_liquidity_floor_is_never_rounded_through_a_float(page: bytes) -> None:
    """`Decimal("0.1")` and `Decimal(str(0.1))` differ; money takes the exact path."""
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=page))
    result = runner.invoke(
        app, ["markets", "discover", "--min-liquidity", "0.1000000000000000055", "--json"]
    )
    assert result.exit_code == 0
    policy = orjson.loads(result.stdout)["policy"]
    assert policy["min_liquidity"] == "0.1000000000000000055"


@respx.mock
def test_discover_can_archive_the_raw_payload(
    page: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGOS_DATA_DIR", str(tmp_path))
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=page))
    result = runner.invoke(app, ["markets", "discover", "--save-raw", "--json"])
    assert result.exit_code == 0, result.output

    report = orjson.loads(result.stdout)
    archived = Path(report["raw_archive_path"])
    assert archived.read_bytes() == page
    assert archived.stem.startswith(report["raw_sha256"][:16])


@respx.mock
def test_discover_does_not_archive_unless_asked(
    page: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGOS_DATA_DIR", str(tmp_path))
    respx.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, content=page))
    result = runner.invoke(app, ["markets", "discover", "--json"])
    assert orjson.loads(result.stdout)["raw_archive_path"] is None
    assert list(tmp_path.iterdir()) == []


# --- audit ------------------------------------------------------------------------


@respx.mock
def test_audit_renders_a_reviewable_report(single: bytes) -> None:
    market_id = json.loads(single)["id"]
    respx.get(f"{BASE}/markets/{market_id}").mock(return_value=httpx.Response(200, content=single))
    result = runner.invoke(app, ["markets", "audit", market_id])
    assert result.exit_code == 0, result.output

    assert "# Market audit" in result.stdout
    assert json.loads(single)["question"] in result.stdout
    assert "Outcome to token mapping" in result.stdout
    assert "no probability claim" in result.stdout


@respx.mock
def test_audit_emits_a_versioned_record_as_json(single: bytes) -> None:
    market_id = json.loads(single)["id"]
    respx.get(f"{BASE}/markets/{market_id}").mock(return_value=httpx.Response(200, content=single))
    result = runner.invoke(app, ["markets", "audit", market_id, "--json"])
    record = orjson.loads(result.stdout)
    assert record["schema_version"] == "market_audit.v1"
    assert record["contract"]["review_status"] in {"unreviewed", "machine_checked"}
    assert record["contract"]["yes_condition"] is None


@respx.mock
def test_audit_of_an_unknown_market_exits_with_the_source_error() -> None:
    respx.get(f"{BASE}/markets/0").mock(return_value=httpx.Response(404))
    result = runner.invoke(app, ["markets", "audit", "0"])
    assert result.exit_code == 1
    assert "argos.source_protocol" in result.output


@respx.mock
def test_audit_of_an_unreadable_market_reports_the_rejection_reason() -> None:
    respx.get(f"{BASE}/markets/7").mock(
        return_value=httpx.Response(200, content=orjson.dumps({"id": "7"}))
    )
    result = runner.invoke(app, ["markets", "audit", "7"])
    assert result.exit_code == 1
    assert "argos.ingestion" in result.output
