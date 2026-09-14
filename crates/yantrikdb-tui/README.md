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

- **Read-only by construction.** Every read goes through the engine's
  non-reinforcing paths (`recall(..., skip_reinforce = true, ...)`,
  `list_memories`, `get`, the `engine::inspect` reads), so opening a store
  here leaves no access-count trace and writes nothing.
- **Searches with the store's own model.** It reads the embedder identity
  the store recorded and attaches that model by name (a store built with
  the default `potion-base-8M` needs the cached 28 MB tarball the engine
  already keeps). If that fails, the status line says search is degraded
  rather than pretending.
- **Safe beside a live agent in another process.** The store is opened
  with the engine's own SQLite in this process; separate processes are
  serialised by the kernel (CONCURRENCY.md rule 9 forbids a second SQLite
  *library in one process*, which this is not).
- **Zero model calls beyond the local embedder.** No network, no LLM.

## Not (yet)

Encrypted stores open only with their key, which the explorer does not
take; conflicts, triggers and sessions are not shown; there is no editing,
by design.
