# M4 technical qualification, 2026-09-26

The fresh T1–T8 campaign ran in order against clean revision
`7b75006c6adb1f01d84ab675e46960d47c12cc64`; all eight scenarios
reported `PASSED`. The source run at `D:\Argos-m4-technical-20260926-v1`
has not been modified. This directory publishes an independently verifiable
copy of its bytes, including the C: fixture used by T8.

The original `proof/proof-index.json` binds the exact ZIP and file-manifest
bytes. Its successor `proof/proof-index-v2.json` preserves those identities
and also binds `proof/technical-campaign.json`, a validated
`TechnicalCampaignV1` aggregate derived after the run from its original
scenario results and configurations. The manifest hashes every archived file,
including the original result, config, checkpoint, raw and runner files. The
aggregate's derivation time is not a prospective preregistration. No
predictive performance or calibration claim follows from qualification.

Verify from a clean checkout:

```text
python scripts/m4_technical_proof.py verify --proof experiments/m4-technical-20260926-v1/proof
```

The verifier checks every archive member against its manifest, all declared
scenario artifacts, the T1–T8 partition and order, bounds, `PASSED` results,
runner terminal state, empty stderr, the two T8 fixture bytes, and every
field of the aggregate against the original records. The
campaign's more detailed independent local audit is preserved inside the
archive as `campaign-summary.md`.
