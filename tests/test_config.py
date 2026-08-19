import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import orjson
import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from argos.clock import ReplayClock
from argos.config import (
    FingerprintScope,
    RunManifest,
    RunMode,
    Settings,
    WorkingTreeStatus,
    build_run_manifest,
    fingerprint_scope,
    load_settings,
)
from argos.config.settings import unknown_environment_keys
from argos.domain.provenance import SourceProvenanceV1
from argos.domain.versioning import VersionedModel
from argos.errors import (
    ConfigurationError,
    ExecutionProhibitedError,
    NaiveDatetimeError,
    SchemaVersionError,
)

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _provenance(**overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "gamma",
        "endpoint": "/markets",
        "http_status": 200,
        "retrieved_at": START,
        "raw_sha256": "a" * 64,
        "byte_length": 12,
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _manifest(**overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "settings": Settings(),
        "clock": ReplayClock(START),
        "run_id": "run-1",
        "mode": RunMode.REPLAY,
    }
    fields.update(overrides)
    return build_run_manifest(**fields)


def test_defaults_point_at_public_endpoints() -> None:
    settings = Settings()
    assert settings.gamma_base_url.startswith("https://")
    assert settings.clob_market_ws_url.startswith("wss://")
    assert settings.execution_enabled is False


def test_settings_are_immutable() -> None:
    settings = Settings()
    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_execution_enabled_is_rejected() -> None:
    with pytest.raises(ExecutionProhibitedError) as caught:
        Settings(execution_enabled=True)
    assert caught.value.code == "argos.execution_prohibited"


def test_execution_enabled_is_rejected_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", "true")
    with pytest.raises(ExecutionProhibitedError):
        load_settings()


def test_environment_overrides_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGOS_LOG_LEVEL", "debug")
    monkeypatch.setenv("ARGOS_DATA_DIR", "/tmp/argos-capture")
    settings = load_settings()
    assert settings.log_level == "DEBUG"
    assert settings.data_dir == Path("/tmp/argos-capture")


def test_invalid_url_scheme_is_reported_as_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGOS_GAMMA_BASE_URL", "ftp://gamma-api.polymarket.com")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert caught.value.code == "argos.configuration"


def test_unknown_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGOS_PRIVATE_KEY", "0xdeadbeef")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_fingerprint_is_stable_and_sensitive() -> None:
    baseline = Settings().fingerprint()
    assert baseline == Settings().fingerprint()
    assert baseline != Settings(gamma_base_url="https://gamma-api.polymarket.com/v2").fingerprint()


def test_fingerprint_is_sensitive_to_the_jitter_seed() -> None:
    """ADR-0009: the seed is configuration, not a runtime accident, so a capture
    that changed it must be traceable through the manifest's config fingerprint."""
    assert Settings().fingerprint() != Settings(source_jitter_seed=1).fingerprint()


# --- fingerprint scope (M3 blocker R1) --------------------------------------------


def test_the_fingerprint_does_not_change_when_only_the_output_location_does() -> None:
    """The M3 blocker this mechanism exists to close.

    `docs/02_ARCHITECTURE.md` lists output storage location among the components
    that may differ between live and replay, and `docs/04_DATA_CONTRACTS.md`
    requires that repeated replay of identical input, code and config produce an
    identical state hash. Before this, two replays of one capture into two
    directories recorded two different `config_fingerprint`s -- the same
    experiment, described as two.
    """
    a = Settings(data_dir=Path("/tmp/replay-a"))
    b = Settings(data_dir=Path("/tmp/replay-b"))
    assert a.fingerprint() == b.fingerprint()
    # ...and the difference is still recorded, just not hashed.
    assert a.snapshot()["data_dir"] != b.snapshot()["data_dir"]


def test_the_fingerprint_does_not_change_when_only_log_verbosity_does() -> None:
    """`log_level` reaches structlog's level filter and no record. A run an
    operator re-ran with `DEBUG` to diagnose something is the same experiment."""
    assert Settings().fingerprint() == Settings(log_level="DEBUG").fingerprint()


def test_environment_settings_are_absent_from_the_hashed_view_not_blanked() -> None:
    """Absent, so that reclassifying a field changes the fingerprint.

    A blanked-but-present key would let `data_dir` move from ENVIRONMENT to
    EXPERIMENT without the hash noticing, and a reclassification is a change to
    what "the same configuration" means.
    """
    hashed = Settings().experiment_snapshot()
    assert "data_dir" not in hashed
    assert "log_level" not in hashed
    assert set(hashed) < set(Settings().snapshot())


@pytest.mark.parametrize("field_name", sorted(Settings.model_fields))
def test_every_setting_declares_a_fingerprint_scope(field_name: str) -> None:
    """No default exists, deliberately: "hash it" would silently re-create the
    `data_dir` defect for the next field somebody adds, and "do not hash it"
    would silently drop a real experiment variable out of the record that
    exists to identify the experiment."""
    assert fingerprint_scope(field_name) in set(FingerprintScope)


def test_a_field_with_no_declared_scope_fails_closed() -> None:
    """The guard above is only worth having if the lookup refuses rather than
    guesses, so the refusal is exercised directly rather than assumed."""

    class _Unclassified(BaseSettings):
        forgotten: int = 1

    patched = {"forgotten": _Unclassified.model_fields["forgotten"]}
    with mock.patch.dict(Settings.model_fields, patched), pytest.raises(ConfigurationError):
        fingerprint_scope("forgotten")


def test_an_unknown_field_name_is_refused_rather_than_silently_scoped() -> None:
    with pytest.raises(ConfigurationError):
        fingerprint_scope("no_such_setting")


def test_manifest_uses_the_injected_clock_and_is_reproducible() -> None:
    settings = Settings()
    first = build_run_manifest(
        settings=settings, clock=ReplayClock(START), run_id="run-1", mode=RunMode.REPLAY
    )
    second = build_run_manifest(
        settings=settings, clock=ReplayClock(START), run_id="run-1", mode=RunMode.REPLAY
    )
    assert first.created_at == START
    assert first.to_record() == second.to_record()
    assert first.config_fingerprint == settings.fingerprint()


def test_manifest_round_trips_with_its_schema_version() -> None:
    manifest = build_run_manifest(
        settings=Settings(), clock=ReplayClock(START), run_id="run-1", mode=RunMode.REPLAY
    )
    record = manifest.to_record()
    assert record["schema_version"] == "run_manifest.v5"
    assert RunManifest.from_record(record) == manifest


def test_manifest_rejects_a_foreign_schema_version() -> None:
    manifest = build_run_manifest(
        settings=Settings(), clock=ReplayClock(START), run_id="run-1", mode=RunMode.REPLAY
    )
    record = manifest.to_record()
    record["schema_version"] = "run_manifest.v1"
    with pytest.raises(SchemaVersionError):
        RunManifest.from_record(record)


def test_manifest_is_immutable() -> None:
    manifest = build_run_manifest(
        settings=Settings(), clock=ReplayClock(START), run_id="run-1", mode=RunMode.REPLAY
    )
    with pytest.raises(ValidationError):
        manifest.run_id = "run-2"  # type: ignore[misc]


def test_the_settings_snapshot_cannot_be_edited_after_the_fact() -> None:
    """frozen=True only blocks attribute assignment; a mutable dict field would
    let any holder rewrite recorded provenance and have it reach to_record()."""
    manifest = _manifest()
    with pytest.raises(TypeError):
        manifest.settings_snapshot["log_level"] = "MUTATED"  # type: ignore[index]
    assert manifest.to_record()["settings_snapshot"]["log_level"] == "INFO"


def test_editing_a_serialized_record_does_not_reach_back_into_the_manifest() -> None:
    manifest = _manifest()
    record = manifest.to_record()
    record["settings_snapshot"]["log_level"] = "MUTATED"
    assert manifest.to_record()["settings_snapshot"]["log_level"] == "INFO"


# --- the execution prohibition, spelled every way an operator might spell it ------


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "YES", "on", "t", "y"])
def test_every_truthy_spelling_of_execution_enabled_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """ADR-0007 forbids execution; a case or synonym gap would be a silent bypass."""
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", value)
    with pytest.raises(ExecutionProhibitedError) as caught:
        load_settings()
    assert caught.value.code == "argos.execution_prohibited"


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "off", "f", "n"])
def test_falsy_spellings_of_execution_enabled_load_normally(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", value)
    assert load_settings().execution_enabled is False


def test_a_nonsense_execution_flag_fails_rather_than_defaulting_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``maybe`` must not be quietly coerced; an unparseable flag is a config error."""
    monkeypatch.setenv("ARGOS_EXECUTION_ENABLED", "maybe")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_execution_enabled_is_rejected_when_passed_as_an_override() -> None:
    with pytest.raises(ExecutionProhibitedError):
        load_settings(execution_enabled=True)


# --- unknown / misspelled variables ----------------------------------------------


@pytest.mark.parametrize("name", ["ARGOS_PRIVATE_KEY", "argos_private_key", "Argos_Log_Levl"])
def test_unknown_variables_are_detected_whatever_their_case(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Environment lookup is case-insensitive, so the guard must be too."""
    monkeypatch.setenv(name, "x")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    variables = caught.value.context["variables"]  # type: ignore[assignment]
    assert name.casefold() in {item.casefold() for item in variables}


@pytest.mark.parametrize("name", ["argos_log_level", "Argos_Log_Level", "ARGOS_LOG_LEVEL"])
def test_a_known_field_in_any_case_is_accepted_and_applied(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "warning")
    assert unknown_environment_keys({name: "warning"}) == []
    assert load_settings().log_level == "WARNING"


def test_a_non_argos_variable_is_left_alone() -> None:
    assert unknown_environment_keys({"PATH": "/usr/bin", "ARGOSAURUS": "1"}) == []


def test_unknown_overrides_are_reported_as_configuration_errors() -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(bogus_field=1)
    assert caught.value.code == "argos.configuration"


def test_env_example_names_only_real_settings_fields() -> None:
    """A drifted example file hands operators a config that fails to load."""
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    declared = {
        line.split("=", 1)[0].strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert declared, "the example file should document the ARGOS_* surface"
    assert unknown_environment_keys(dict.fromkeys(declared, "")) == []


# --- boundary validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"http_timeout_seconds": 0},
        {"http_timeout_seconds": -1.0},
        {"http_max_attempts": 0},
        {"http_max_attempts": -3},
        {"source_jitter_seed": -1},
        {"source_jitter_seed": 2**32},
        {"log_level": "LOUD"},
        {"gamma_base_url": "gamma-api.polymarket.com"},
        {"gamma_base_url": "wss://gamma-api.polymarket.com"},
        {"clob_market_ws_url": "https://ws-subscriptions-clob.polymarket.com/ws/market"},
        {"clob_base_url": "https://"},
    ],
)
def test_invalid_values_are_refused_at_the_boundary(override: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(**override)


@pytest.mark.parametrize("attempts", [1, 2, 10])
def test_every_legal_retry_count_is_accepted(attempts: int) -> None:
    assert Settings(http_max_attempts=attempts).http_max_attempts == attempts


@pytest.mark.parametrize("seed", [0, 1, 2**32 - 1])
def test_every_legal_jitter_seed_is_accepted(seed: int) -> None:
    assert Settings(source_jitter_seed=seed).source_jitter_seed == seed


@pytest.mark.parametrize("override", [{"http_max_attempts": 11}, {"http_timeout_seconds": 121.0}])
def test_retry_and_timeout_ceilings_are_enforced(override: dict[str, object]) -> None:
    """docs/09_SECURITY.md caps retry rates: no floor without a ceiling."""
    with pytest.raises(ConfigurationError):
        load_settings(**override)


# --- fingerprint reproducibility --------------------------------------------------


def test_fingerprint_ignores_how_a_value_arrived(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-sourced and argument-sourced configuration are the same experiment."""
    monkeypatch.setenv("ARGOS_LOG_LEVEL", "debug")
    assert load_settings().fingerprint() == Settings(log_level="DEBUG").fingerprint()


def test_fingerprint_ignores_a_cosmetic_trailing_slash() -> None:
    assert (
        Settings(gamma_base_url="https://gamma-api.polymarket.com/").fingerprint()
        == Settings().fingerprint()
    )


def test_fingerprint_is_identical_in_a_fresh_process() -> None:
    """A fingerprint that depended on hash seeding or dict order could not be cited."""
    program = "from argos.config import Settings; print(Settings().fingerprint())"
    seen = set()
    for seed in ("0", "1", "524287"):
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if not key.upper().startswith("ARGOS_")
                },
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
            timeout=60,
        )
        seen.add(result.stdout.strip())
    assert seen == {Settings().fingerprint()}


def test_snapshot_is_canonical_json_and_hides_nothing_from_the_manifest() -> None:
    snapshot = Settings().snapshot()
    assert orjson.loads(orjson.dumps(snapshot)) == snapshot
    assert set(snapshot) == set(Settings.model_fields)
    assert snapshot["data_dir"] == ".data"


# --- record contracts -------------------------------------------------------------


def test_from_record_neither_mutates_nor_consumes_its_input() -> None:
    """A reader that popped the version out of the caller's dict would break re-reads."""
    record = _manifest().to_record()
    original = dict(record)
    first = RunManifest.from_record(record)
    assert record == original
    assert RunManifest.from_record(record) == first


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.update(unexpected_field="x"), id="extra-key"),
        pytest.param(lambda r: r.pop("run_id"), id="missing-key"),
        pytest.param(lambda r: r.update(runId=r.pop("run_id")), id="renamed-key"),
        pytest.param(lambda r: r.update(created_at="not-a-timestamp"), id="bad-timestamp"),
        pytest.param(lambda r: r.update(settings_snapshot="not-a-mapping"), id="bad-snapshot"),
    ],
)
def test_a_drifted_record_is_refused_rather_than_partially_loaded(
    mutate: Any,
) -> None:
    record = _manifest().to_record()
    mutate(record)
    with pytest.raises((ValidationError, SchemaVersionError)):
        RunManifest.from_record(record)


@pytest.mark.parametrize("version", [None, "", 1, "market_definition.v1", "run_manifest.v0"])
def test_a_record_without_a_usable_schema_version_is_refused(version: object) -> None:
    record = _manifest().to_record()
    if version is None:
        record.pop("schema_version")
    else:
        record["schema_version"] = version
    with pytest.raises(SchemaVersionError) as caught:
        RunManifest.from_record(record)
    assert caught.value.code == "argos.schema_version"


def test_the_schema_version_cannot_be_smuggled_in_as_a_field() -> None:
    """It describes the class; accepting it as data would let a record relabel itself."""
    with pytest.raises(ValidationError):
        RunManifest(
            run_id="run-1",
            mode=RunMode.REPLAY,
            created_at=START,
            argos_version="0.0.0",
            config_fingerprint="f",
            settings_snapshot={},
            schema_version="run_manifest.v9",
        )


def test_serializing_a_record_twice_produces_identical_bytes() -> None:
    manifest = _manifest()
    canonical = orjson.dumps(manifest.to_record(), option=orjson.OPT_SORT_KEYS)
    assert canonical == orjson.dumps(manifest.to_record(), option=orjson.OPT_SORT_KEYS)
    assert canonical == orjson.dumps(
        RunManifest.from_record(manifest.to_record()).to_record(), option=orjson.OPT_SORT_KEYS
    )


def test_a_versioned_model_must_declare_its_own_version() -> None:
    with pytest.raises(SchemaVersionError):

        class Unversioned(VersionedModel):
            pass


def test_an_empty_schema_version_is_refused_at_class_definition() -> None:
    with pytest.raises(SchemaVersionError):

        class Blank(VersionedModel):
            schema_version: ClassVar[str] = ""


# --- timestamp anchoring (regressions for defects found in the M0 review) -----------


def test_manifest_refuses_a_naive_created_at() -> None:
    """The model contract, not just build_run_manifest, must anchor the timestamp:
    a manifest read back from disk has to be as trustworthy as one just built."""
    with pytest.raises(NaiveDatetimeError):
        RunManifest(
            run_id="run-1",
            mode=RunMode.REPLAY,
            created_at=datetime(2026, 1, 1, 12, 0),
            argos_version="0.0.0",
            config_fingerprint="f",
            settings_snapshot={},
        )


def test_manifest_normalizes_a_non_utc_created_at() -> None:
    """Two manifests describing the same instant must serialize identically, or
    M3's "identical input produces identical output hash" cannot hold."""
    offset = datetime(2026, 1, 1, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    manifest = RunManifest(
        run_id="run-1",
        mode=RunMode.REPLAY,
        created_at=offset,
        argos_version="0.0.0",
        config_fingerprint="f",
        settings_snapshot={},
    )
    assert manifest.to_record()["created_at"] == "2026-01-01T12:00:00Z"


def test_a_subclass_cannot_silently_reuse_its_parents_schema_version() -> None:
    """A subclass with different fields must not write records labelled with the
    parent's schema version — readers would accept them and the drift is invisible."""
    with pytest.raises(SchemaVersionError):

        class ExtendedManifest(RunManifest):
            extra_note: str = ""


# --- mode is an enum, not a free-form string (carried from the M0 closure) --------


def test_mode_rejects_a_string_outside_the_enum() -> None:
    with pytest.raises(ValidationError):
        _manifest(mode="some-future-milestone")


@pytest.mark.parametrize(
    "mode", [RunMode.DISCOVER, RunMode.AUDIT, RunMode.CAPTURE, RunMode.REPLAY, RunMode.INSPECT]
)
def test_every_declared_mode_round_trips(mode: RunMode) -> None:
    manifest = _manifest(mode=mode)
    record = manifest.to_record()
    assert record["mode"] == mode.value
    assert RunManifest.from_record(record).mode is mode


def test_mode_accepts_its_own_string_value_like_any_other_enum_field() -> None:
    """A stored record round-trips through its serialized value, not the Python name."""
    manifest = _manifest(mode="capture")
    assert manifest.mode is RunMode.CAPTURE


# --- schema versions and data provenance (carried from the M0 closure) ------------


def test_schema_versions_default_to_empty_for_a_run_that_touches_no_schema() -> None:
    manifest = _manifest(mode=RunMode.INSPECT)
    assert manifest.schema_versions == ()
    assert manifest.input_provenance == ()


def test_schema_versions_are_deduplicated_and_sorted() -> None:
    manifest = _manifest(
        schema_versions=[
            "compiled_market_contract.v1",
            "market_definition.v1",
            "market_definition.v1",
        ]
    )
    assert manifest.schema_versions == ("compiled_market_contract.v1", "market_definition.v1")


def test_schema_versions_order_does_not_affect_the_record() -> None:
    forward = _manifest(schema_versions=["b.v1", "a.v1"])
    backward = _manifest(schema_versions=["a.v1", "b.v1"])
    assert forward.to_record() == backward.to_record()


def test_an_empty_schema_version_entry_is_refused() -> None:
    with pytest.raises(ValidationError):
        _manifest(schema_versions=["market_definition.v1", ""])


def test_input_provenance_round_trips_and_links_raw_payloads() -> None:
    provenance = _provenance(raw_sha256="b" * 64)
    manifest = _manifest(
        mode=RunMode.CAPTURE,
        schema_versions=["market_definition.v1"],
        input_provenance=[provenance],
    )
    record = manifest.to_record()
    assert record["input_provenance"][0]["raw_sha256"] == "b" * 64
    assert record["input_provenance"][0]["endpoint"] == "/markets"
    restored = RunManifest.from_record(record)
    assert restored.input_provenance == (provenance,)


def test_input_provenance_order_does_not_affect_the_record() -> None:
    first = _provenance(raw_sha256="a" * 64, endpoint="/markets")
    second = _provenance(raw_sha256="c" * 64, endpoint="/markets/2")
    forward = _manifest(input_provenance=[first, second])
    backward = _manifest(input_provenance=[second, first])
    assert forward.to_record() == backward.to_record()


def test_input_provenance_rejects_a_non_provenance_item() -> None:
    with pytest.raises(ValidationError):
        _manifest(input_provenance=[{"not": "provenance"}])


def test_differently_ordered_inputs_serialize_to_identical_bytes() -> None:
    """Dict equality (as used elsewhere in this file) can hide an ordering
    difference orjson would still serialize distinctly; this asserts the actual
    canonical bytes a persisted manifest would write are identical regardless of
    the order the caller happened to collect schema versions and provenance in."""
    first = _provenance(raw_sha256="1" * 64, endpoint="/markets")
    second = _provenance(raw_sha256="2" * 64, endpoint="/markets/2")

    forward = _manifest(
        schema_versions=["market_definition.v1", "compiled_market_contract.v1"],
        input_provenance=[first, second],
    )
    backward = _manifest(
        schema_versions=["compiled_market_contract.v1", "market_definition.v1"],
        input_provenance=[second, first],
    )

    forward_bytes = orjson.dumps(forward.to_record(), option=orjson.OPT_SORT_KEYS)
    backward_bytes = orjson.dumps(backward.to_record(), option=orjson.OPT_SORT_KEYS)
    assert forward_bytes == backward_bytes


# --- working-tree state alongside code_revision (carried from the M0 closure) -----


def test_working_tree_defaults_to_unknown() -> None:
    manifest = _manifest()
    assert manifest.working_tree is WorkingTreeStatus.UNKNOWN


@pytest.mark.parametrize("status", [WorkingTreeStatus.CLEAN, WorkingTreeStatus.DIRTY])
def test_working_tree_requires_a_trusted_revision(status: WorkingTreeStatus) -> None:
    """A manifest cannot claim clean/dirty without the revision that makes the
    claim meaningful — otherwise a wheel install could report a stale CLEAN."""
    with pytest.raises(ValidationError):
        _manifest(code_revision=None, working_tree=status)


@pytest.mark.parametrize("status", [WorkingTreeStatus.CLEAN, WorkingTreeStatus.DIRTY])
def test_working_tree_is_recorded_alongside_a_trusted_revision(status: WorkingTreeStatus) -> None:
    manifest = _manifest(code_revision="a" * 40, working_tree=status)
    assert manifest.working_tree is status
    assert RunManifest.from_record(manifest.to_record()).working_tree is status


def test_a_dirty_tree_and_a_clean_tree_produce_different_records() -> None:
    clean = _manifest(code_revision="a" * 40, working_tree=WorkingTreeStatus.CLEAN)
    dirty = _manifest(code_revision="a" * 40, working_tree=WorkingTreeStatus.DIRTY)
    assert clean.to_record() != dirty.to_record()


def test_unknown_working_tree_is_also_allowed_alongside_a_trusted_revision() -> None:
    """The validator's one rule is "no clean/dirty claim without a revision" — it
    does not require the converse. A revision paired with `UNKNOWN` is legitimate:
    for example a revision resolved from git while `git status` itself failed."""
    manifest = _manifest(code_revision="a" * 40, working_tree=WorkingTreeStatus.UNKNOWN)
    assert manifest.code_revision == "a" * 40
    assert manifest.working_tree is WorkingTreeStatus.UNKNOWN
