# M4 technical qualification, 2026-09-26

The fresh T1–T8 campaign ran in order against clean revision
`7b75006c6adb1f01d84ab675e46960d47c12cc64`; all eight scenarios
reported `PASSED`. The source run at `D:\Argos-m4-technical-20260926-v1`
has not been modified. This directory publishes an independently verifiable
copy of its bytes, including the C: fixture used by T8.

The `proof/proof-index.json` binds the exact ZIP and file-manifest bytes. The
manifest hashes every archived file, including the original result, config,
checkpoint, raw and runner files. The index is a post-run inventory and does
not pretend that the campaign was prospectively preregistered. No predictive
performance or calibration claim follows from technical qualification.

Verify from a clean checkout:

```text
python scripts/m4_technical_proof.py verify --proof experiments/m4-technical-20260926-v1/proof
```

The verifier checks every archive member against its manifest, all declared
scenario artifacts, the T1–T8 partition and order, bounds, `PASSED` results,
runner terminal state, empty stderr, and the two T8 fixture bytes. The
campaign's more detailed independent local audit is preserved inside the
archive as `campaign-summary.md`.
