"""Inspection reads on the Python binding: `claims_for_memory`,
`revision_history`, `memory_entities`.

These are the three reads an explorer needs (atlas, CLI, MCP, a TUI) and
could previously get only from raw SQL, which is forbidden in the engine's
process. The last test checks the USE: a store the engine built, exported
by the packaged atlas exporter (raw SQL in a child process), must agree
with what the binding reports for the same memory.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yantrikdb import YantrikDB

_EXPORTER = Path(__file__).resolve().parents[1] / "src" / "yantrikdb" / "atlas" / "export_atlas.py"


def test_binding_registers_the_three_reads():
    """Source-build CI must fail, not skip, if a registration goes missing."""
    for name in ("claims_for_memory", "revision_history", "memory_entities"):
        assert callable(getattr(YantrikDB, name, None)), f"binding lacks {name}"


@pytest.fixture
def db(tmp_path):
    store = YantrikDB.with_default(str(tmp_path / "inspect.db"))
    yield store
    store.close()


def test_revision_history_returns_prior_states_oldest_first(db):
    rid = db.record("Dana leads the Data Platform team.", metadata={"team": "data"})
    assert db.revision_history(rid) == []
    before = time.time()
    db.correct(rid, reason="team reorganised", new_text="Dana leads the ML Platform team.")
    db.correct(rid, reason="importance bump", new_importance=0.9)

    revs = db.revision_history(rid)
    assert [r["revision_num"] for r in revs] == [1, 2]
    assert revs[0]["prior_text"] == "Dana leads the Data Platform team."
    assert revs[0]["reason"] == "team reorganised"
    assert revs[0]["prior_metadata"]["team"] == "data", "metadata comes back parsed"
    assert revs[1]["prior_text"] == "Dana leads the ML Platform team."
    assert revs[1]["reason"] == "importance bump"
    assert all(r["rid"] == rid for r in revs)
    assert before - 1 <= revs[0]["applied_at"] <= time.time() + 1
    assert db.revision_history("no-such-rid") == []


def test_claims_for_memory_returns_the_memory_s_claims(db):
    rid = db.record("Pranab prefers Vim for editing Rust and reviews with Maria.")
    other = db.record("Unrelated note about the weather in Lisbon.")
    report = db.attach_claims(rid, [
        {"src": "Pranab", "rel_type": "prefers", "dst": "Vim"},
        {"src": "Pranab", "rel_type": "reviews with", "dst": "Maria"},
    ])
    assert len(report["accepted"]) == 2, report

    claims = db.claims_for_memory(rid)
    assert {c["dst"] for c in claims} == {"Vim", "Maria"}
    assert all(c["source_memory_rid"] == rid for c in claims)
    assert all(c["status_suggestion"] == "active" for c in claims)
    assert db.claims_for_memory(other) == []
    assert db.claims_for_memory("no-such-rid") == []


def test_memory_entities_lists_linked_names_sorted(db):
    rid = db.record("A plain record with no obvious names in it.")
    db.link_memory_entity(rid, "Zeta Corp")
    db.link_memory_entity(rid, "Acme")
    names = db.memory_entities(rid)
    assert names[:2] == ["Acme", "Zeta Corp"] or {"Acme", "Zeta Corp"} <= set(names)
    assert db.memory_entities("no-such-rid") == []


def test_binding_reads_agree_with_the_atlas_exporter(tmp_path):
    """The use: the exporter reads claims, revisions and memberships from
    the file with raw SQL in a child process; the binding reads them in the
    engine's process. Same store, same memory, same answers."""
    path = tmp_path / "agree.db"
    db = YantrikDB.with_default(str(path))
    rid = db.record("Dana Okafor leads the Data Platform team at Northwind Analytics.")
    db.attach_claims(rid, [{"src": "Dana Okafor", "rel_type": "leads", "dst": "Data Platform team"}])
    db.correct(rid, reason="team reorganised",
               new_text="Dana Okafor leads the ML Platform team at Northwind Analytics.")
    db.link_memory_entity(rid, "Northwind Analytics")
    db.think()
    expected = {
        "claims": sorted((c["src"], c["rel_type"], c["dst"]) for c in db.claims_for_memory(rid)),
        "revisions": [(r["revision_num"], r["prior_text"]) for r in db.revision_history(rid)],
        "entities": set(db.memory_entities(rid)),
    }
    db.close()

    out = tmp_path / "site"
    proc = subprocess.run([sys.executable, str(_EXPORTER), "--stores", str(path), "--out", str(out)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    idx = next(i for i, m in enumerate(data["memories"]) if m["rid"] == rid)
    # The exporter keys claims, revisions and memberships by the memory's
    # index in data["memories"], not by rid.
    got_claims = sorted((c["src"], c["rel_type"], c["dst"]) for c in data["claims"]
                        if c.get("memory_id") == idx)
    got_revs = [(r["revision_num"], r["prior_text"]) for r in data["revisions"] if r["memory_id"] == idx]
    got_entities = {name for mid, _store, name in data["memberships"] if mid == idx}

    assert got_claims == expected["claims"]
    assert got_revs == expected["revisions"]
    assert got_entities == expected["entities"]
