"""
Phase 5A: PostgreSQL-oriented persistence foundation.

Scope of this package (bounded, per the Phase 5A instructions): database
configuration, session/transaction management, canonical identity/
reference tables, source-artifact provenance, model-version storage,
pipeline-run storage, data-quality-issue storage, and typed idempotent
repositories for exactly those tables. Detailed statistics, roster,
injury, ranking, benchmark, market-snapshot, prediction, simulation,
recommendation, publication, and result tables are explicitly deferred to
later Phase 5 batches -- not built here.

Nothing in this package connects to a production database, performs live
scraping/API calls, or reads/prints/validates `.env` secret values.
"""
