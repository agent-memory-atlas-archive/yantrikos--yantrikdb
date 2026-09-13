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
