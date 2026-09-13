"""Memory Atlas: a static, dependency-free explorer for a YantrikDB store.

This subpackage holds two files and deliberately imports nothing:

- ``export_atlas.py``: reads one or more ``.db`` files read-only with the
  standard library's ``sqlite3`` and writes ``data.json`` next to a copy of
  the page. It must run in a process that has NOT opened the store with the
  engine (CONCURRENCY.md rule 9: never two SQLite libraries on one store in
  one process), so callers run it as a child process by absolute script
  path, never via ``python -m yantrikdb.atlas...`` which would import the
  engine first. See ``yantrikdb atlas`` in ``yantrikdb.cli``.
- ``index.html``: the page (one sphere per store and namespace, lines for
  shared entities, an inspector with claims, revision history and tasks).

``examples/memory_atlas/`` in the repository is a pointer to this package.
"""
