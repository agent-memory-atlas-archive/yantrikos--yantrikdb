"""`yantrikdb atlas <store.db>`: the packaged Memory Atlas exporter behind
the CLI. The command must export ONLY the named store (never its sibling
.db files), run the exporter as a child process by script path, and write
the page next to the data.
"""
import json
import sqlite3
import sys
from pathlib import Path

from click.testing import CliRunner

from yantrikdb.cli import cli

_SCHEMA = ("CREATE TABLE memories (rid TEXT,namespace TEXT,type TEXT,text TEXT,"
           "importance REAL,created_at REAL,metadata TEXT,consolidation_status TEXT)")


def _store(path: Path, rows):
    c = sqlite3.connect(path)
    c.execute(_SCHEMA)
    c.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def _fixture(tmp_path: Path) -> Path:
    db = tmp_path / "mine.db"
    _store(db, [("a", "work", "semantic", "First memory", 0.7, 10, "{}", "active"),
                ("b", "work", "semantic", "Second memory", 0.4, 12, "{}", "active")])
    _store(tmp_path / "sibling.db",
           [("s", "work", "semantic", "Someone else's store", 0.5, 5, "{}", "active")])
    return db


def test_atlas_exports_only_the_named_store_and_writes_the_page(tmp_path):
    db = _fixture(tmp_path)
    out = tmp_path / "site"
    r = CliRunner().invoke(cli, ["atlas", str(db), "--out", str(out), "--no-serve", "--label", "Test"])
    assert r.exit_code == 0, r.output
    assert (out / "index.html").is_file() and (out / "export-report.json").is_file()
    d = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert [s["store"] for s in d["sources"]] == ["mine.db"], "sibling.db must not be swept in"
    assert len(d["memories"]) == 2
    assert d.get("label") == "Test" or "Test" in json.dumps(d)


def test_default_output_dir_sits_next_to_the_store(tmp_path):
    db = _fixture(tmp_path)
    r = CliRunner().invoke(cli, ["atlas", str(db), "--no-serve"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "mine.db.atlas" / "data.json").is_file()


def test_missing_store_is_a_usage_error_not_a_traceback(tmp_path):
    r = CliRunner().invoke(cli, ["atlas", str(tmp_path / "nope.db"), "--no-serve"])
    assert r.exit_code == 2
    assert "nope.db" in r.output


def test_exporter_failure_is_reported_with_its_message(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a database", encoding="utf-8")
    r = CliRunner().invoke(cli, ["atlas", str(bad), "--no-serve"])
    assert r.exit_code != 0
    assert "Traceback" not in r.output or "file is not a database" in r.output


def test_packaged_exporter_is_invoked_as_a_script(monkeypatch, tmp_path):
    """Never `-m yantrikdb.atlas...`: the child must not import the engine."""
    import subprocess as sp
    seen = {}
    real_run = sp.run

    def spy(cmd, **kw):
        seen["cmd"] = cmd
        return real_run(cmd, **kw)

    monkeypatch.setattr(sp, "run", spy)
    db = _fixture(tmp_path)
    r = CliRunner().invoke(cli, ["atlas", str(db), "--out", str(tmp_path / "s"), "--no-serve"])
    assert r.exit_code == 0, r.output
    cmd = seen["cmd"]
    assert cmd[0] == sys.executable and "-m" not in cmd
    assert cmd[1].endswith("export_atlas.py") and Path(cmd[1]).is_file()
    assert cmd[cmd.index("--stores") + 1] == str(db.resolve())


def test_out_dir_equal_to_the_store_directory_is_refused(tmp_path):
    """The server would otherwise serve the store and its neighbours."""
    db = _fixture(tmp_path)
    r = CliRunner().invoke(cli, ["atlas", str(db), "--out", str(tmp_path), "--no-serve"])
    assert r.exit_code != 0
    assert "store's own directory" in r.output
    assert not (tmp_path / "data.json").exists()


def test_existing_non_atlas_directory_is_refused(tmp_path):
    db = _fixture(tmp_path)
    other = tmp_path / "docs"
    other.mkdir()
    (other / "index.html").write_text("someone else's page", encoding="utf-8")
    r = CliRunner().invoke(cli, ["atlas", str(db), "--out", str(other), "--no-serve"])
    assert r.exit_code != 0 and "not an atlas export directory" in r.output
    assert (other / "index.html").read_text(encoding="utf-8") == "someone else's page"


def test_encrypted_store_is_refused_with_a_plain_message(tmp_path):
    from yantrikdb import YantrikDB

    enc = tmp_path / "secret.db"
    YantrikDB(str(enc), encryption_key=bytes(range(32))).close()
    r = CliRunner().invoke(cli, ["atlas", str(enc), "--out", str(tmp_path / "s"), "--no-serve"])
    assert r.exit_code != 0
    assert "encrypted" in r.output and "Traceback" not in r.output


# ── `yantrikdb tui` launcher ─────────────────────────────────────────


def test_tui_launcher_says_where_to_get_the_binary_when_missing(tmp_path, monkeypatch):
    import yantrikdb.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_find_tui_binary", lambda: None)
    db = _fixture(tmp_path)
    r = CliRunner().invoke(cli, ["tui", str(db)])
    assert r.exit_code != 0
    assert "yantrikdb-tui is not installed" in r.output
    assert cli_mod.TUI_RELEASES_URL in r.output and "cargo install" in r.output


def test_tui_launcher_runs_the_binary_on_the_resolved_store_path(tmp_path, monkeypatch):
    import subprocess as sp
    import yantrikdb.cli as cli_mod

    seen = {}
    monkeypatch.setattr(cli_mod, "_find_tui_binary", lambda: "/fake/bin/yantrikdb-tui")
    monkeypatch.setattr(sp, "call", lambda argv: seen.setdefault("argv", argv) and 0)
    db = _fixture(tmp_path)
    r = CliRunner().invoke(cli, ["tui", str(db)])
    assert r.exit_code == 0, r.output
    assert seen["argv"] == ["/fake/bin/yantrikdb-tui", str(db.resolve())]


def test_tui_launcher_propagates_the_binary_exit_code(tmp_path, monkeypatch):
    import subprocess as sp
    import yantrikdb.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_find_tui_binary", lambda: "/fake/bin/yantrikdb-tui")
    monkeypatch.setattr(sp, "call", lambda argv: 3)
    db = _fixture(tmp_path)
    r = CliRunner().invoke(cli, ["tui", str(db)])
    assert r.exit_code == 3
