"""Serve a Memory Atlas export, and nothing else.

Shared by ``yantrikdb atlas`` (the CLI) and the MCP ``atlas`` tool so the two
cannot drift apart on the one promise that matters here: the local server
hands out the export artifacts and NOTHING else. A plain
``SimpleHTTPRequestHandler`` over an arbitrary ``--out`` would list the
directory and serve whatever sits beside the export, including the store
itself if someone pointed ``--out`` at the store's own directory (review
finding P1 on yantrikos/yantrikdb#232). So:

- :func:`atlas_out_dir` decides whether a directory may receive an export at
  all: never the store's own directory; an existing directory only if it is
  empty or already an atlas export (it holds ``export-report.json``).
- :func:`serve_export` binds a server whose handler answers exactly the
  allowlisted artifact names, resolves every path back into the export
  directory (no symlink escapes), and never lists a directory.

Standard library only; this module must not import the engine.
"""
from __future__ import annotations

import os
import posixpath
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: The complete set of files an export produces and the server will serve.
ARTIFACTS = ("index.html", "data.json", "export-report.json")
REPORT = "export-report.json"


def atlas_out_dir(out: Path, store: Path) -> Path:
    """Validate and resolve the export directory for ``store``.

    Refuses (``ValueError``) when ``out`` is the store's own directory, is an
    existing non-directory, or is an existing non-empty directory that is
    not already an atlas export (no ``export-report.json``). Symlinks are
    resolved first, so a link to the store's directory is refused too.
    """
    out_r = Path(out).resolve()
    store_r = Path(store).resolve()
    if out_r == store_r.parent:
        raise ValueError(
            f"refusing to export into the store's own directory {out_r}: the atlas "
            "server would then serve the store and its neighbours. Use a dedicated "
            f"directory (default: {store_r.name}.atlas next to the store)."
        )
    if out_r == store_r or (store_r.is_relative_to(out_r) if hasattr(store_r, "is_relative_to") else False):
        raise ValueError(f"refusing to export into {out_r}: it contains the store")
    if out_r.exists():
        if not out_r.is_dir():
            raise ValueError(f"{out_r} exists and is not a directory")
        entries = [p.name for p in out_r.iterdir()]
        if entries and not artifact_is_regular(out_r, REPORT):
            raise ValueError(
                f"{out_r} exists, is not empty and is not an atlas export directory "
                f"(no regular {REPORT}); choose an empty or atlas-owned directory so "
                "nothing unrelated is overwritten or served"
            )
        # Ownership means the artifacts are OUR regular files. A symlinked
        # artifact would be followed by a refresh and served by the server.
        for name in ARTIFACTS:
            candidate = out_r / name
            if (candidate.is_symlink() or candidate.exists()) and not artifact_is_regular(out_r, name):
                raise ValueError(
                    f"{candidate} is a symlink or not a regular file; refusing to refresh "
                    "or serve an export directory whose artifacts are not its own files"
                )
    return out_r


def artifact_is_regular(root: Path, name: str) -> bool:
    """True when ``root/name`` is a regular file (not a symlink) that
    resolves to exactly itself inside ``root``."""
    candidate = Path(root) / name
    if candidate.is_symlink():
        return False
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return False
    return resolved == Path(root).resolve() / name and resolved.is_file()


def write_artifact(path: Path, data: bytes) -> None:
    """Write an artifact atomically: exclusive temp file beside it, then
    ``os.replace``.

    The temp file is created by ``tempfile.mkstemp`` (``O_EXCL``, random
    name), never at a predictable path: a hard link or symlink planted at a
    guessable name would otherwise redirect the write into some other file
    (review finding on yantrikos/yantrikdb#232). ``os.replace`` swaps the
    directory entry, so a symlink at ``path`` is replaced rather than
    followed and a server already serving the directory never sees a
    partially written file. On any failure the temp file is removed.
    """
    import tempfile

    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def make_handler(out_dir: Path):
    """A request handler that serves only ``ARTIFACTS`` from ``out_dir``."""
    root = Path(out_dir).resolve()

    class AtlasHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def _artifact(self) -> str | None:
            """The canonical artifact NAME to serve, or None (404).

            Only an allowlisted name that is a regular file living directly
            in the export directory qualifies. A symlinked artifact is
            rejected outright: `index.html -> unrelated.txt` would otherwise
            pass a parent check and serve the link's target, and a link to
            a file outside the directory would serve that.
            """
            # Strip query/fragment, normalise, map "/" to the page.
            raw = self.path.split("?", 1)[0].split("#", 1)[0]
            name = posixpath.normpath(raw).lstrip("/")
            if name in ("", "."):
                name = "index.html"
            if name not in ARTIFACTS:
                return None
            return name if artifact_is_regular(root, name) else None

        def do_GET(self):  # noqa: N802 (http.server naming)
            name = self._artifact()
            if name is None:
                self.send_error(404, "not an atlas artifact")
                return
            self.path = "/" + name
            super().do_GET()

        def do_HEAD(self):  # noqa: N802
            name = self._artifact()
            if name is None:
                self.send_error(404, "not an atlas artifact")
                return
            self.path = "/" + name
            super().do_HEAD()

        def list_directory(self, path):  # never
            self.send_error(404, "not an atlas artifact")
            return None

        def log_message(self, *_args):  # keep stdio quiet (MCP speaks on stdout)
            pass

    return AtlasHandler


def serve_export(out_dir: Path, port: int = 0, bind: str = "127.0.0.1"):
    """Bind a server for ``out_dir``. Returns ``(httpd, url)``; the caller
    decides whether to ``serve_forever()`` in the foreground (CLI) or a
    daemon thread (MCP)."""
    httpd = ThreadingHTTPServer((bind, port), make_handler(out_dir))
    url = f"http://{bind}:{httpd.server_address[1]}/"
    return httpd, url


def serve_in_background(out_dir: Path, port: int = 0, bind: str = "127.0.0.1"):
    """``serve_export`` plus a daemon thread running it. Returns ``(httpd, url)``."""
    httpd, url = serve_export(out_dir, port, bind)
    threading.Thread(
        target=httpd.serve_forever, name=f"yantrikdb-atlas:{httpd.server_address[1]}", daemon=True
    ).start()
    return httpd, url
