"""Adversarial guards for the synthetic V2 capture/snapshot contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import orjson
import pytest
from test_cohort_protocol_v2 import START
from test_cohort_snapshot_v2 import _synthetic_snapshot

from argos.baselines.quote import MarketQuoteV1
from argos.config.manifest import RunMode, WorkingTreeStatus
from argos.evaluation.cohort_selection_v2 import BookAttemptStatus
from argos.evaluation.cohort_snapshot_v2 import (
    _capture_frames,
    _capture_manifest_matches,
    _selected_target,
    _verify_receipt,
    build_cohort_capture_close_id,
    build_cohort_frozen_forecast_snapshot_id,
)
from argos.evaluation.prospective import EvidenceArtifactKind, persist_evidence_record
from argos.store.raw_archive import read_raw_payload


def _unchecked(model: Any, **updates: Any) -> Any:
    """Build malformed evidence deliberately, bypassing the validated copy API."""
    return type(model).model_construct(**{**dict(model), **updates})


def _context(archive: Path):
    close, close_receipt = _synthetic_snapshot(archive)
    snapshot = close.forecast_snapshot
    decision = snapshot.selection.page_decisions[snapshot.entry_index]
    assert decision.market is not None
    return close, close_receipt, snapshot, decision


def _snapshot_with_consistent_id(snapshot: Any, **updates: Any) -> Any:
    malformed = _unchecked(snapshot, **updates)
    return _unchecked(
        malformed,
        snapshot_id=build_cohort_frozen_forecast_snapshot_id(
            protocol=malformed.protocol,
            protocol_receipt=malformed.protocol_receipt,
            selection=malformed.selection,
            selection_receipt=malformed.selection_receipt,
            entry_index=malformed.entry_index,
            target_rank=malformed.target_rank,
            target_id=malformed.target_id,
            capture_run_manifest=malformed.capture_run_manifest,
            forecasts=malformed.forecasts,
            frozen_at=malformed.frozen_at,
        ),
    )


def _close_with_consistent_id(close: Any, **updates: Any) -> Any:
    malformed = _unchecked(close, **updates)
    return _unchecked(
        malformed,
        capture_close_id=build_cohort_capture_close_id(
            forecast_snapshot=malformed.forecast_snapshot,
            forecast_snapshot_receipt=malformed.forecast_snapshot_receipt,
            started_at=malformed.started_at,
            closed_at=malformed.closed_at,
            frame_count=malformed.frame_count,
            capture_archive_provenance=malformed.capture_archive_provenance,
        ),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", RunMode.REPLAY),
        ("capture_run_id", None),
        ("run_id", "different-run"),
        ("created_at", START + timedelta(days=1)),
        ("code_revision", "different-revision"),
        ("working_tree", WorkingTreeStatus.DIRTY),
        ("config_fingerprint", "different-config"),
    ],
)
def test_capture_manifest_rejects_runtime_identity_mismatches(
    tmp_path: Path, field: str, value: Any
) -> None:
    close, _, snapshot, decision = _context(tmp_path)
    manifest = _unchecked(snapshot.capture_run_manifest, **{field: value})
    market = decision.market
    assert market is not None

    with pytest.raises(ValueError, match="capture manifest disagrees"):
        _capture_manifest_matches(
            snapshot.protocol,
            manifest,
            target_id=snapshot.target_id,
            yes_token_id=market.token_id_for("Yes"),
            no_token_id=market.token_id_for("No"),
            started_at=close.started_at,
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda params: params.update(subscribed_token_ids=None),
        lambda params: params.update(subscribed_token_ids="yes,no"),
        lambda params: params.update(subscribed_token_ids=("yes",)),
        lambda params: params.update(subscribed_token_ids=("wrong", "pair")),
        lambda params: params.update(subscribed_token_ids=(701, "702")),
        lambda params: params.update(target_id="wrong-target"),
        lambda params: params.update(max_seconds="not-a-number"),
        lambda params: params.update(max_seconds="NaN"),
        lambda params: params.update(max_seconds=1),
        lambda params: params.update(max_frames=True),
        lambda params: params.update(max_frames=1),
        lambda params: params.update(max_bytes=False),
        lambda params: params.update(max_bytes=1),
        lambda params: params.update(raw_archive=False),
        lambda params: params.update(separate_database_per_target=False),
        lambda params: params.update(target_database_id="  "),
    ],
)
def test_capture_manifest_rejects_bad_caps_scope_and_archive_policy(
    tmp_path: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    close, _, snapshot, decision = _context(tmp_path)
    parameters = dict(snapshot.capture_run_manifest.run_parameters)
    change(parameters)
    manifest = _unchecked(snapshot.capture_run_manifest, run_parameters=parameters)
    market = decision.market
    assert market is not None

    with pytest.raises(ValueError):
        _capture_manifest_matches(
            snapshot.protocol,
            manifest,
            target_id=snapshot.target_id,
            yes_token_id=market.token_id_for("Yes"),
            no_token_id=market.token_id_for("No"),
            started_at=close.started_at,
        )


@pytest.mark.parametrize(
    "schema_versions",
    [(), ("m4_cohort_capture_close.v1",), ("m4_cohort_frozen_forecast_snapshot.v1",)],
)
def test_capture_manifest_requires_both_v2_evidence_schemas(
    tmp_path: Path, schema_versions: tuple[str, ...]
) -> None:
    close, _, snapshot, decision = _context(tmp_path)
    manifest = _unchecked(snapshot.capture_run_manifest, schema_versions=schema_versions)
    market = decision.market
    assert market is not None

    with pytest.raises(ValueError, match="omits required V2 capture evidence schemas"):
        _capture_manifest_matches(
            snapshot.protocol,
            manifest,
            target_id=snapshot.target_id,
            yes_token_id=market.token_id_for("Yes"),
            no_token_id=market.token_id_for("No"),
            started_at=close.started_at,
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda frame: frame.update(sequence=True),
        lambda frame: frame.update(sequence=2),
        lambda frame: frame.update(token_id="other-token"),
        lambda frame: frame.update(condition_id="other-condition"),
        lambda frame: frame.update(received_at=1),
        lambda frame: frame.update(observation_id=""),
        lambda frame: frame.update(information_state_hash="z" * 64),
        lambda frame: frame.update(information_state_hash="short"),
    ],
)
def test_capture_frame_replay_rejects_invalid_identity_fields(
    tmp_path: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    close, _, _snapshot, decision = _context(tmp_path)
    raw, _ = read_raw_payload(tmp_path, close.capture_archive_provenance.raw_sha256)
    frames = orjson.loads(raw)
    change(frames[0])
    market = decision.market
    assert market is not None

    with pytest.raises(ValueError, match="frame identity or order is invalid"):
        _capture_frames(
            orjson.dumps(frames),
            yes_token_id=market.token_id_for("Yes"),
            no_token_id=market.token_id_for("No"),
            condition_id=market.condition_id,
            started_at=close.started_at,
            closed_at=close.closed_at,
        )


def test_capture_frame_replay_rejects_bad_json_shape_time_and_duplicates(tmp_path: Path) -> None:
    close, _, _snapshot, decision = _context(tmp_path)
    raw, _ = read_raw_payload(tmp_path, close.capture_archive_provenance.raw_sha256)
    frames = orjson.loads(raw)
    market = decision.market
    assert market is not None
    kwargs = {
        "yes_token_id": market.token_id_for("Yes"),
        "no_token_id": market.token_id_for("No"),
        "condition_id": market.condition_id,
        "started_at": close.started_at,
        "closed_at": close.closed_at,
    }
    cases = [
        (b"{bad", "not valid JSON"),
        (orjson.dumps({}), "nonempty JSON frame list"),
        (orjson.dumps([]), "nonempty JSON frame list"),
        (orjson.dumps(["not-an-object"]), "nonempty JSON frame list"),
    ]
    for payload, message in cases:
        with pytest.raises(ValueError, match=message):
            _capture_frames(payload, **kwargs)

    bad_time = [dict(frames[0], received_at="not-a-date")]
    with pytest.raises(ValueError, match="invalid receive time"):
        _capture_frames(orjson.dumps(bad_time), **kwargs)

    before_start = [
        dict(frames[0], received_at=(close.started_at - timedelta(seconds=1)).isoformat())
    ]
    with pytest.raises(ValueError, match="outside the close interval"):
        _capture_frames(orjson.dumps(before_start), **kwargs)

    after_close = [
        dict(frames[0], received_at=(close.closed_at + timedelta(seconds=1)).isoformat())
    ]
    with pytest.raises(ValueError, match="outside the close interval"):
        _capture_frames(orjson.dumps(after_close), **kwargs)

    duplicate = [frames[0], dict(frames[1], observation_id=frames[0]["observation_id"])]
    with pytest.raises(ValueError, match="frame identity or order is invalid"):
        _capture_frames(orjson.dumps(duplicate), **kwargs)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p, s: (p, _unchecked(s, block_ordinal=0), 0),
        lambda p, s: (p, _unchecked(s, block_ordinal=999), 0),
        lambda p, s: (_unchecked(p, experiment_id="other"), s, 0),
        lambda p, s: (p, _unchecked(s, experiment_id="other"), 0),
        lambda p, s: (p, _unchecked(s, protocol_sha256="0" * 64), 0),
        lambda p, s: (p, _unchecked(s, selected_at=START - timedelta(days=1)), 0),
        lambda protocol, selection: (protocol, selection, 999),
    ],
)
def test_selected_target_rejects_invalid_protocol_block_or_target(
    tmp_path: Path, mutate: Callable[..., tuple[Any, Any, int]]
) -> None:
    _close, _, snapshot, _ = _context(tmp_path)
    protocol, selection, entry_index = mutate(snapshot.protocol, snapshot.selection)

    with pytest.raises(ValueError):
        _selected_target(protocol, selection, entry_index)


def test_selected_target_requires_matching_review_and_book_receipts(tmp_path: Path) -> None:
    _close, _, snapshot, decision = _context(tmp_path)
    selection = snapshot.selection

    missing_review = _unchecked(decision, review_receipt_id="missing-review")
    missing_review_selection = _unchecked(
        selection, page_decisions=(missing_review, *selection.page_decisions[1:])
    )
    with pytest.raises(ValueError, match="no matching semantic-review receipt"):
        _selected_target(snapshot.protocol, missing_review_selection, snapshot.entry_index)

    review = selection.reviews[0]
    review_mismatch = _unchecked(review, entry_index=review.entry_index + 1)
    review_selection = _unchecked(selection, reviews=(review_mismatch,))
    with pytest.raises(ValueError, match="semantic review does not bind"):
        _selected_target(snapshot.protocol, review_selection, snapshot.entry_index)

    missing_book = _unchecked(decision, book_attempt_receipt_id="missing-book")
    missing_book_selection = _unchecked(
        selection, page_decisions=(missing_book, *selection.page_decisions[1:])
    )
    with pytest.raises(ValueError, match="no matching CLOB book receipt"):
        _selected_target(snapshot.protocol, missing_book_selection, snapshot.entry_index)

    attempt = _unchecked(selection.book_attempts[0], status=BookAttemptStatus.FAILED)
    failed_book_selection = _unchecked(selection, book_attempts=(attempt,))
    with pytest.raises(ValueError, match="pre-selection Yes-token book"):
        _selected_target(snapshot.protocol, failed_book_selection, snapshot.entry_index)


def test_receipt_scope_is_checked_after_canonical_record_bytes(tmp_path: Path) -> None:
    close, receipt, _, _ = _context(tmp_path)
    wrong_scope = _unchecked(receipt, artifact_id="different-close")

    with pytest.raises(ValueError, match="receipt names different evidence"):
        _verify_receipt(
            wrong_scope,
            close,
            experiment_id=close.forecast_snapshot.protocol.experiment_id,
            kind=EvidenceArtifactKind.COHORT_CAPTURE_CLOSE,
            artifact_id=close.capture_close_id,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"target_rank": 99}, "target identity or deterministic rank"),
        ({"target_id": "wrong-target"}, "target identity or deterministic rank"),
        ({"frozen_at": START + timedelta(seconds=29)}, "predates durable target admission"),
        ({"snapshot_id": "wrong-snapshot"}, "identity disagrees with its frozen evidence"),
    ],
)
def test_snapshot_validator_rejects_target_freeze_and_identity_tampering(
    tmp_path: Path, updates: dict[str, Any], message: str
) -> None:
    _, _, snapshot, _ = _context(tmp_path)
    malformed = (
        _snapshot_with_consistent_id(snapshot, **updates)
        if "frozen_at" in updates
        else _unchecked(snapshot, **updates)
    )

    with pytest.raises(ValueError, match=message):
        malformed._one_blind_shared_information_state()


def test_snapshot_validator_rejects_freeze_at_the_blind_cutoff(tmp_path: Path) -> None:
    _, _, snapshot, decision = _context(tmp_path)
    review = next(
        item for item in snapshot.selection.reviews if item.entry_index == decision.entry_index
    )
    blind_cutoff = review.earliest_outcome_knowable_at - timedelta(
        seconds=snapshot.protocol.outcome_blind_margin_seconds
    )
    malformed = _snapshot_with_consistent_id(snapshot, frozen_at=blind_cutoff)

    with pytest.raises(ValueError, match="outcome-blind margin"):
        malformed._one_blind_shared_information_state()


def test_snapshot_validator_rejects_protocol_durability_and_manifest_order(
    tmp_path: Path,
) -> None:
    _, _, snapshot, _ = _context(tmp_path)
    late_protocol_receipt = _unchecked(
        snapshot.protocol_receipt,
        persisted_at=snapshot.protocol.declared_at + timedelta(seconds=1),
    )
    malformed = _unchecked(snapshot, protocol_receipt=late_protocol_receipt)
    with pytest.raises(ValueError, match="not durable by its declared time"):
        malformed._one_blind_shared_information_state()

    early_manifest = _unchecked(
        snapshot.capture_run_manifest,
        created_at=snapshot.selection.selected_at - timedelta(seconds=1),
    )
    malformed = _unchecked(snapshot, capture_run_manifest=early_manifest)
    with pytest.raises(ValueError, match="manifest predates durable V2 target admission"):
        malformed._one_blind_shared_information_state()


def test_snapshot_validator_rejects_missing_mixed_and_unavailable_baselines(
    tmp_path: Path,
) -> None:
    _, _, snapshot, _ = _context(tmp_path)
    malformed = _unchecked(snapshot, forecasts=snapshot.forecasts[:-1])
    with pytest.raises(ValueError, match="exactly one forecast per baseline method"):
        malformed._one_blind_shared_information_state()

    changed = _unchecked(snapshot.forecasts[0], evaluation_run_id="different-evaluation-run")
    malformed = _unchecked(snapshot, forecasts=(changed, *snapshot.forecasts[1:]))
    with pytest.raises(ValueError, match="share one evaluation information state"):
        malformed._one_blind_shared_information_state()

    changed = _unchecked(snapshot.forecasts[0], market_id="different-market")
    malformed = _unchecked(snapshot, forecasts=(changed, *snapshot.forecasts[1:]))
    with pytest.raises(ValueError, match="different target or capture"):
        malformed._one_blind_shared_information_state()

    changed_forecasts = tuple(
        _unchecked(forecast, as_of_received_time=snapshot.frozen_at + timedelta(seconds=1))
        for forecast in snapshot.forecasts
    )
    malformed = _unchecked(snapshot, forecasts=changed_forecasts)
    with pytest.raises(ValueError, match="unavailable at the frozen information state"):
        malformed._one_blind_shared_information_state()


@pytest.mark.parametrize("anchor", ["quote", "as_of_event_time"])
def test_snapshot_validator_rejects_different_quote_or_event_anchors(
    tmp_path: Path, anchor: str
) -> None:
    _, _, snapshot, _ = _context(tmp_path)
    forecast = snapshot.forecasts[0]
    if anchor == "quote":
        changed_quote = MarketQuoteV1.model_validate(
            {
                **forecast.quote.model_dump(),
                "best_bid": Decimal("0.39"),
                "midpoint": Decimal("0.42"),
                "spread": Decimal("0.06"),
            }
        )
        changed_forecast = _unchecked(forecast, quote=changed_quote)
    else:
        changed_forecast = _unchecked(
            forecast,
            as_of_event_time=forecast.as_of_event_time + timedelta(seconds=1),
        )
    malformed = _unchecked(
        snapshot,
        forecasts=(changed_forecast, *snapshot.forecasts[1:]),
    )

    with pytest.raises(ValueError, match="share one evaluation information state"):
        malformed._one_blind_shared_information_state()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"started_at": START + timedelta(seconds=93)}, "freeze must occur during capture"),
        ({"closed_at": START + timedelta(seconds=30)}, "freeze must occur during capture"),
        ({"closed_at": START + timedelta(days=1)}, "duration exceeds the per-target V2 cap"),
        ({"frame_count": 501}, "frame count exceeds the per-target V2 cap"),
        ({"capture_close_id": "wrong-close"}, "identity disagrees with its evidence"),
    ],
)
def test_capture_close_validator_rejects_timing_caps_and_identity(
    tmp_path: Path, updates: dict[str, Any], message: str
) -> None:
    close, _, _, _ = _context(tmp_path)
    if "capture_close_id" in updates:
        malformed = _unchecked(close, **updates)
    elif "started_at" in updates or updates.get("closed_at") == START + timedelta(seconds=30):
        malformed = _close_with_consistent_id(close, **updates)
    else:
        malformed = _close_with_consistent_id(close, **updates)

    with pytest.raises(ValueError, match=message):
        malformed._capture_is_bound_and_bounded()


def test_capture_close_validator_requires_durable_freeze_and_raw_archive(
    tmp_path: Path,
) -> None:
    close, _, snapshot, _ = _context(tmp_path)
    earlier_receipt = _unchecked(
        close.forecast_snapshot_receipt,
        persisted_at=snapshot.frozen_at - timedelta(seconds=1),
    )
    malformed = _unchecked(close, forecast_snapshot_receipt=earlier_receipt)
    with pytest.raises(ValueError, match="persisted before it was frozen"):
        malformed._capture_is_bound_and_bounded()

    empty_archive = _unchecked(close.capture_archive_provenance, byte_length=0)
    malformed = _unchecked(close, capture_archive_provenance=empty_archive)
    with pytest.raises(ValueError, match="archive must contain bytes"):
        malformed._capture_is_bound_and_bounded()

    late_archive = _unchecked(
        close.capture_archive_provenance,
        retrieved_at=close.closed_at - timedelta(seconds=1),
    )
    malformed = _unchecked(close, capture_archive_provenance=late_archive)
    with pytest.raises(ValueError, match="first-hand and post-close"):
        malformed._capture_is_bound_and_bounded()


def test_capture_close_rejects_start_before_selection_receipt(tmp_path: Path) -> None:
    close, _, snapshot, _ = _context(tmp_path)
    late_selection_receipt = _unchecked(
        snapshot.selection_receipt,
        persisted_at=START + timedelta(seconds=40),
    )
    admitted_snapshot = _unchecked(
        snapshot,
        selection_receipt=late_selection_receipt,
        frozen_at=START + timedelta(seconds=45),
    )
    snapshot_receipt = persist_evidence_record(
        tmp_path,
        record=admitted_snapshot,
        experiment_id=snapshot.protocol.experiment_id,
        artifact_kind=EvidenceArtifactKind.FROZEN_FORECAST_SNAPSHOT,
        artifact_id=snapshot.snapshot_id,
        persisted_at=START + timedelta(seconds=50),
    )
    malformed = _close_with_consistent_id(
        close,
        forecast_snapshot=admitted_snapshot,
        forecast_snapshot_receipt=snapshot_receipt,
        started_at=START + timedelta(seconds=39),
    )

    with pytest.raises(ValueError, match="capture began before its admitted"):
        malformed._capture_is_bound_and_bounded()
