"""`yantrikdb.atlas.serve`: the export directory rules and the allowlist
server shared by the CLI and the MCP tool.

The promise under test: the local server hands out the three export
artifacts and nothing else, and an export never lands where it would
overwrite something unrelated or expose the store.
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from yantrikdb.atlas.serve import ARTIFACTS, atlas_out_dir, serve_in_background


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


# ── atlas_out_dir ────────────────────────────────────────────────────


def test_store_directory_is_refused(tmp_path):
    store = tmp_path / "memory.db"
    store.write_bytes(b"x")
    with pytest.raises(ValueError, match="store's own directory"):
        atlas_out_dir(tmp_path, store)


def test_missing_and_empty_directories_are_accepted(tmp_path):
    store = tmp_path / "memory.db"
    store.write_bytes(b"x")
    assert atlas_out_dir(tmp_path / "new", store) == (tmp_path / "new").resolve()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert atlas_out_dir(empty, store) == empty.resolve()


def test_existing_atlas_directory_is_accepted_and_foreign_one_refused(tmp_path):
    store = tmp_path / "memory.db"
    store.write_bytes(b"x")
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "export-report.json").write_text("{}", encoding="utf-8")
    (mine / "data.json").write_text("{}", encoding="utf-8")
    assert atlas_out_dir(mine, store) == mine.resolve()

    foreign = tmp_path / "docs"
    foreign.mkdir()
    (foreign / "index.html").write_text("not ours", encoding="utf-8")
    with pytest.raises(ValueError, match="not an atlas export directory"):
        atlas_out_dir(foreign, store)

    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        atlas_out_dir(a_file, store)


def test_symlink_to_the_store_directory_is_refused(tmp_path):
    store = tmp_path / "memory.db"
    store.write_bytes(b"x")
    link = tmp_path / "link"
    try:
        os.symlink(tmp_path, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    with pytest.raises(ValueError, match="store's own directory"):
        atlas_out_dir(link, store)


# ── the server ───────────────────────────────────────────────────────


@pytest.fixture
def served(tmp_path):
    out = tmp_path / "export"
    out.mkdir()
    (out / "index.html").write_text("<title>YantrikDB · Memory Atlas</title>", encoding="utf-8")
    (out / "data.json").write_text(json.dumps({"format_version": 2}), encoding="utf-8")
    (out / "export-report.json").write_text(json.dumps({"memories": 0}), encoding="utf-8")
    (out / "secret.txt").write_text("must never be served", encoding="utf-8")
    (out / "sub").mkdir()
    (out / "sub" / "index.html").write_text("nested", encoding="utf-8")
    (tmp_path / "memory.db").write_bytes(b"SQLite format 3\x00")
    httpd, url = serve_in_background(out)
    yield url
    httpd.shutdown()
    httpd.server_close()


def test_serves_exactly_the_artifacts(served):
    status, body = _get(served)
    assert status == 200 and b"Memory Atlas" in body, "/ is the page"
    for name in ARTIFACTS:
        status, _ = _get(served + name)
        assert status == 200, name
    assert json.loads(_get(served + "data.json")[1])["format_version"] == 2


def test_refuses_everything_else(served):
    for path in ("secret.txt", "sub/", "sub/index.html", "memory.db", "../memory.db",
                 "index.html/../secret.txt", "%2e%2e/memory.db"):
        status, body = _get(served + path)
        assert status == 404, path
        assert b"must never be served" not in body and b"SQLite" not in body
    # A query string never changes which file is served: this is data.json.
    status, body = _get(served + "data.json?x=1/../secret.txt")
    assert status == 200 and b"format_version" in body and b"must never be served" not in body


def test_directory_listing_is_never_produced(served):
    # A plain SimpleHTTPRequestHandler would list `sub/`; ours must not.
    status, body = _get(served + "sub/")
    assert status == 404 and b"Directory listing" not in body
