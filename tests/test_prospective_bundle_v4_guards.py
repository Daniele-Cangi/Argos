"""Branch-complete adversarial guards for prospective bundle V4."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from test_prospective_aggregation_v2 import _unchecked
from test_prospective_bundle import _redigest_v4, _valid_result

from argos.evaluation.prospective_bundle_v4 import EvaluationRunBundleV4


@pytest.mark.parametrize(
    ("manifest_updates", "parameter_updates", "message"),
    [
        ({"capture_run_id": None}, {}, "does not name the evaluated target run"),
        ({"mode": "replay"}, {}, "does not name the evaluated target run"),
        ({"code_revision": "f" * 40}, {}, "frozen protocol runtime"),
        ({}, {"subscribed_token_ids": "not-a-token-sequence"}, "subscribe exactly both"),
        ({}, {"max_seconds": "not-a-number"}, "not a numeric frozen bound"),
        ({}, {"max_frames": False}, "frame bound disagrees"),
        ({}, {"raw_archive": False}, "raw-archive policy disagrees"),
    ],
)
async def test_global_redigest_cannot_rewrite_v4_capture_manifest_semantics(
    tmp_path,
    manifest_updates: dict[str, Any],
    parameter_updates: dict[str, Any],
    message: str,
) -> None:
    record = deepcopy((await _valid_result(tmp_path, protocol_v2=True)).bundle.to_record())
    record["capture_run_manifest"].update(manifest_updates)
    record["capture_run_manifest"]["run_parameters"].update(parameter_updates)
    _redigest_v4(record)

    with pytest.raises(ValueError, match=message):
        EvaluationRunBundleV4.from_record(record)


async def test_v4_rejects_selected_cutoff_after_deadline_independently(tmp_path) -> None:
    bundle = (await _valid_result(tmp_path, protocol_v2=True)).bundle
    attacked_cutoff = _unchecked(
        bundle.cutoff_evidence,
        selected_cutoff=bundle.protocol.lifecycle_deadline + timedelta(seconds=1),
    )
    attacked = _unchecked(bundle, cutoff_evidence=attacked_cutoff)

    with pytest.raises(ValueError, match="after the frozen lifecycle deadline"):
        attacked._cutoff_is_inside_frozen_lifecycle()
