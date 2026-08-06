# Data integrity rule

- Raw source payloads are immutable and linked by hash.
- Preserve event time, received time, and ingest sequence separately.
- Never replace an invalid source timestamp with current time silently.
- Every drop, duplicate, parse failure, gap, late event, and fallback is counted and reasoned.
- Normalization and compiler updates create versioned records.
- Do not edit real capture files manually.
