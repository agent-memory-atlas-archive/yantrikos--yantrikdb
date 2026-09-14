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

from yantrikdb.atlas.serve import ARTIFACTS, atlas_out_dir, serve_in_background, write_artifact


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


def _symlink(src: Path, dst: Path):
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")


def test_symlinked_artifact_is_refused_same_root_and_outside_root(tmp_path):
    """`index.html -> unrelated.txt` passed a parent check and served the
    target (review finding); a link to a file outside the directory would
    serve that. Both must be 404, by name and by the refused artifact."""
    out = tmp_path / "export"
    out.mkdir()
    (out / "unrelated.txt").write_text("must never be served", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    _symlink(out / "unrelated.txt", out / "index.html")
    _symlink(outside, out / "data.json")
    (out / "export-report.json").write_text("{}", encoding="utf-8")
    httpd, url = serve_in_background(out)
    try:
        for path in ("", "index.html", "data.json"):
            status, body = _get(url + path)
            assert status == 404, path
            assert b"must never be served" not in body and b"outside secret" not in body
        assert _get(url + "export-report.json")[0] == 200, "the regular artifact still serves"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_out_dir_with_a_symlinked_artifact_is_not_atlas_owned(tmp_path):
    store = tmp_path / "memory.db"
    store.write_bytes(b"x")
    out = tmp_path / "export"
    out.mkdir()
    (out / "export-report.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    _symlink(target, out / "data.json")
    with pytest.raises(ValueError, match="symlink or not a regular file"):
        atlas_out_dir(out, store)
    # A symlinked REPORT means nothing here is ours either.
    out2 = tmp_path / "export2"
    out2.mkdir()
    _symlink(target, out2 / "export-report.json")
    (out2 / "note.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not an atlas export directory"):
        atlas_out_dir(out2, store)


# ── write_artifact ───────────────────────────────────────────────────


def test_write_never_uses_a_predictable_temp_name(tmp_path):
    """A hard link planted at the old predictable temp path
    (`.<name>.tmp-<pid>`) must not receive the bytes (review finding)."""
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("must stay", encoding="utf-8")
    planted = tmp_path / f".data.json.tmp-{os.getpid()}"
    try:
        os.link(unrelated, planted)
    except (OSError, NotImplementedError):
        planted.write_text("must stay", encoding="utf-8")  # a plain file works for the assertion too
    write_artifact(tmp_path / "data.json", b'{"fresh": true}')
    assert unrelated.read_text(encoding="utf-8") == "must stay"
    assert planted.read_text(encoding="utf-8") == "must stay"
    assert (tmp_path / "data.json").read_bytes() == b'{"fresh": true}'
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == [], "no temp files left"


def test_write_failure_leaves_no_temp_file(tmp_path):
    target = tmp_path / "data.json"
    target.mkdir()  # os.replace onto a directory fails
    with pytest.raises(OSError):
        write_artifact(target, b"x")
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_repeated_writes_in_one_process_leave_no_leftovers(tmp_path):
    for i in range(3):
        write_artifact(tmp_path / "export-report.json", f'{{"n": {i}}}'.encode())
    assert (tmp_path / "export-report.json").read_bytes() == b'{"n": 2}'
    assert [p.name for p in tmp_path.iterdir()] == ["export-report.json"]
