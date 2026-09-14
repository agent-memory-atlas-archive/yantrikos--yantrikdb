# yantrikdb-tui

A read-only terminal explorer for one YantrikDB store.

```
cargo run -p yantrikdb-tui -- path/to/store.db
# or, installed:
cargo install --path crates/yantrikdb-tui
yantrikdb-tui path/to/store.db
```

Three panes:

- **namespaces** with live memory counts, `(all)` first;
- **memories**, newest first and paged, or the results of a semantic
  search (`/`, type, Enter; `Esc` clears);
- **inspector** for the selected memory: type, namespace, importance,
  status, created date, domain, source, metadata, the full text, the
  entities the store linked to it, the claims it backs (with their read-time
  status and validity window), and its revision history with the prior
  text and the reason for each correction. The namespace's tasks sit below.

Keys: `Tab`/`Shift-Tab` move between panes, `↑`/`↓` or `j`/`k` move,
`Enter` opens, `/` searches, `Esc` clears a search, `n`/`p` page,
`PgUp`/`PgDn` scroll the inspector, `r` refreshes, `q` quits.

## What it promises

- **The source is never opened by the engine.** An ordinary engine open is
  not read-only (journal switch to WAL, schema migrations, backfills), so
  the explorer first takes a consistent snapshot with SQLite's online
  backup through a plain read-only connection of the engine's own library,
  then builds the engine on that private copy. The source's bytes, journal
  mode and schema stamp stay as they were; an agent writing to it from
  another process is undisturbed; `r` takes a fresh snapshot, and the
  header shows when the current one was taken.
- **Non-reinforcing reads on the copy.** `recall(..., skip_reinforce =
  true, ...)`, `list_memories`, `get` and the `engine::inspect` reads.
- **Searches with the store's own model, verified.** It reads the embedder
  identity the store recorded, attaches that model by name when needed (a
  store built with the default `potion-base-8M` reuses the engine's cached
  tarball), and verifies the attached model's digest against the record.
  Anything short of a verified match disables search and says why; a
  matching dimension alone is never taken as proof.
- **Complete namespace list.** Namespaces with memories carry their live
  count; namespaces that only hold tasks are listed with 0. The scope you
  chose survives a refresh by name.
- **Zero model calls beyond the local embedder.** No network, no LLM.

## Not (yet)

Encrypted stores open only with their key, which the explorer does not
take; conflicts, triggers and sessions are not shown; there is no editing,
by design.
