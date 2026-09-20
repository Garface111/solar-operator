# Railway data storage and invoice evidence

## Retention policy

Utility source bytes used for billing, invoice calculation snapshots, rendered invoices, payments, settlement records, delivery deduplication records and uploaded source documents have no automatic expiry. Existing bills and daily generation history are not deleted. Old invoices that lack original field-level provenance must not be labeled as having complete source lineage.

New GMP interval downloads use tenant-scoped, content-addressed compressed artifacts. Content-defined CSV chunks share unchanged intervals between overlapping downloads. No columns, dates, whitespace or source bytes are discarded. Changed downloads retain prior immutable versions and original capture metadata. Reads validate chunk lengths, SHA-256 hashes and the complete reconstructed artifact hash. Cross-tenant references fail closed.

New invoices freeze their normalized calculation, relevant configuration and available source evidence. Authenticated evidence endpoints are under `/v1/array-operator/billing/invoices/{id}/source-evidence`; downloads require both tenant ownership and membership in that invoice's manifest. Evidence availability and limitations are explicit. No archive step rewrites an existing issued snapshot.

Disposable capture diagnostics and succeeded `pull_bills` jobs expire after 30 days; capture errors and failed completed `pull_bills` jobs after 90 days. Active jobs and other job types are excluded. Cleanup is bounded and runs daily. New capture diagnostics admit only a small bounded profile/account summary, excluding arbitrary credentials and response payloads.

## Historical source conversion

`scripts/compact_gmp_sources.py` previews by default. Each apply transaction locks one source, stores exact bytes, verifies reconstruction, preserves version metadata and only then clears its legacy inline field. Source IDs and rows remain unchanged. A failed transaction leaves its inline source intact. Completed rows leave a partial-index work queue, so interrupted work resumes safely. Byte, row and time budgets limit each batch.

Background conversion is off unless `GMP_SOURCE_COMPACTION_ENABLED=1`. It checks every five minutes by default (`GMP_SOURCE_COMPACTION_INTERVAL_MINUTES`, clamped to 1–60). Each run defaults to at most 1,000 rows, 64MiB of original bytes and 15 seconds between atomic source transactions; the corresponding `MAX_ROWS`, `MAX_MIB` and `MAX_SECONDS` settings can only reduce those maximums. A global advisory lock coordinates manual commands and workers. Empty queues perform only a bounded index lookup.

Build the work-queue index concurrently in PostgreSQL. Take a native database-volume backup, verify schema migration, run a small production pilot and compare reconstructed sources and report results before enabling gradual background conversion.

Compaction initially adds the shared artifacts while freeing inline PostgreSQL TOAST values. Freed database pages can be reused before the filesystem shrinks. Ordinary VACUUM may reclaim free pages at the end of relations; a reduced logical payload does not guarantee an immediate reduction in Railway's billed volume. Do not run VACUUM FULL automatically: it rewrites tables and takes exclusive locks.

## Memory

Tenant overview, tree and inverter caches have entry and age bounds. API responses are disposed after parsing, including error paths. Harvester browser contexts close after source capture and session-state extraction, before ingestion delivery. Collection frequency, browser concurrency, report availability and billing history remain unchanged.

## Verification and cost interpretation

A September 19, 2026 measurement found about $55.44 accrued project usage, approximately $48.62 in RAM and $2.69 in database volume storage. Thus storage cleanup alone cannot remove most of the bill. The sampled overlapping utility sources showed about 86% lower estimated encoded payload storage with shared compressed chunks; this is a sample, not a promise for the whole database. Actual steady-state cost changes need a normal usage window after deployment.

Official references: [Railway pricing](https://docs.railway.com/pricing), [Railway volume usage](https://docs.railway.com/volumes/reference), [PostgreSQL vacuum behavior](https://www.postgresql.org/docs/18/routine-vacuuming.html).
