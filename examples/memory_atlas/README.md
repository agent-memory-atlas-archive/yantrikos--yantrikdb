# YantrikDB Memory Atlas

A dependency-free, read-only dashboard for a snapshot of your YantrikDB stores.
One sphere represents one database and namespace. Search memory text, select
a memory, explore shared-entity connections, inspect revisions and claims,
and view tasks for a namespace.

## Export and run

```powershell
python examples/memory_atlas/export_atlas.py --stores PATH_TO_STORES --out scratch/memory-atlas
python -m http.server 8771 --bind 127.0.0.1 --directory scratch/memory-atlas
```

Open http://127.0.0.1:8771. The input directory must contain `.db` files. No
engine, embedding model, API key, or extra Python dependency is needed.
The page fetches only adjacent `data.json`; it has no external assets.
Re-export and reload to refresh. This is a snapshot, not a live change feed.
Use `--label "Fictional sample"` to add a visible provenance label when exporting
a demonstration database; otherwise the page says Database snapshot.

The output includes full memory text, metadata, prior revision text, claims
and tasks. Treat it as a copy of that data: review it before sharing or
hosting it. Local absolute database paths are not included. Static hosting
requires only `index.html` and `data.json`; the database stays local.

## Reading the graph

- Solid lines connect shared stored entities within a namespace; dashed teal
  lines connect namespaces within the same database. These are derived
  co-membership connections, not stored memory-to-memory claims.
- Exact entity names must occur in 2–30 memories in the database to draw
  connections. Common entities are excluded. Matching names across separate
  databases never establish shared identity.
- Up to 300 memories: all memories and eligible connections appear at rest.
  Larger collections show up to 60 memories per sphere in the overview and
  draw connections on selection. Selection draws up to 120 neighbors. Search
  scans all exported memories in scope; the page displays its drawing cap.
- Positions are deterministic visual layouts, not embedding similarity.
  Search and connection buttons provide keyboard access to memory details.

## Memory details

The inspector shows memory type, domain, importance, creation date, stored
status, full text, metadata, and entity memberships. Revision history shows
previous text, correction reason and application date; application time is
not necessarily the historical event date described in a memory.

Claims are listed separately, labelled stated (`agent_stated`) or extracted,
with their extractor and recorded validity dates. No validity end date is
invented. A claim is flagged **Source revised after this claim** when its
source memory has a revision applied after the claim's creation time. This
flags possible stale provenance; it does not assert the claim is false or
that the correction invalidated every claim on that memory. Non-tombstoned
claims are not automatically current truths. Claims without an exported
source memory remain counted but cannot be shown in an individual inspector.

Tasks are scoped by database and namespace. Session transcripts are not
exported. Tombstoned memories and claims are excluded. Optional fields or
tables absent in older schemas are omitted or represented as unknown.

## Export consistency

SQLite opens each source with `mode=ro`, `query_only`, and a read transaction.
Database-file SHA-256 hashes are checked before and after the read. These
checks do not hash WAL sidecars or provide one atomic snapshot across a
collection. Export settled stores, or a consistent backup, rather than
actively mutating databases. No source files are modified by the exporter.

`export-report.json` records counts and export time. `data.json` includes
format version, source filenames/hashes, and the exported records. Provenance
displayed by the page comes from this export, without fixed benchmark labels.

The older local BEAM snapshots at ports 8769 and 8770 are separate artifacts.
Updating this source template does not modify those snapshots. The fictional
Ari Vasquez sample at port 8771 demonstrates work/personal/learning namespaces
in one database; it is not a benchmark result or evidence of engine quality.
