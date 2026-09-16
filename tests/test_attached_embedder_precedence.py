"""An embedder the caller attached must actually be the one that runs.

Reported as yantrikdb-hermes-plugin#84 ("external embedders produce a
single-session store"). The refusal-on-reopen in that report is a *symptom*.
The bug underneath is quieter and worse: `embed_text` tried the Rust-native
embedder first and only fell back to the attached Python object, so whenever
the engine had already resolved a native embedder for the store's dimension —
which it does at every bundled dimension — `set_embedder(obj)` was accepted,
probed, identity-checked, stored... and then never called.

Measured on 0.23.0 before the fix, at dim 256: the attached embedder's
`encode()` ran **zero** times across a `record_text`, every vector came from
the bundled `potion-base-8M`, and the store recorded that model as its
identity. Truthfully, which is the trap — the stamp was right, the *user's
choice* was the thing that vanished. Someone selecting a multilingual model to
hold non-English memories got English-centric vectors and no error, ever.

Both directions are asserted here on purpose. "The attached embedder runs" is
worthless without "the native embedder still runs when nothing was attached",
or the fix could simply have broken the default path instead.
"""

import math
import tempfile
from pathlib import Path

from yantrikdb import YantrikDB

DIM = 64  # a bundled dimension: the engine auto-attaches a native embedder here,
# which is precisely the condition that used to discard the attached object.


class CountingEmbedder:
    """Valid, unfingerprinted, and deliberately unlike any real model's space."""

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, text):
        if not isinstance(text, str):
            return [self.encode(t) for t in text]
        self.calls += 1
        h = abs(hash(text)) % 100_000
        v = [math.sin(h * (i + 1) * 0.001) for i in range(DIM)]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


def _store(tmp: Path, name: str = "s.db") -> str:
    return str(tmp / name)


def test_attached_embedder_is_the_one_that_runs(tmp_path):
    db = YantrikDB(_store(tmp_path), embedding_dim=DIM)
    assert db.has_embedder(), (
        "precondition: this dimension must auto-attach a native embedder, "
        "otherwise the test cannot observe the precedence it is about"
    )

    emb = CountingEmbedder()
    db.set_embedder(emb)
    emb.calls = 0  # set_embedder probes once; count only the write
    db.record_text(text="Alice Moreau works at Fennwick Labs.")

    assert emb.calls >= 1, (
        "the attached embedder never ran — the engine's own default silently "
        "encoded this record instead (hermes-plugin#84)"
    )
    db.close()


def test_no_identity_is_stamped_for_an_unfingerprinted_attached_embedder(tmp_path):
    """The store must not claim a model that did not build its vectors.

    This is what made the plugin store single-session: the native default
    encoded the records and stamped itself, so reopening and attaching the
    user's own (unfingerprinted) embedder was then correctly refused forever.
    """
    path = _store(tmp_path)
    db = YantrikDB(path, embedding_dim=DIM)
    db.set_embedder(CountingEmbedder())
    db.record_text(text="Dana Okafor leads the ML Platform team.")
    assert db.embedder_identity() is None, (
        "vectors built by an unfingerprinted embedder must leave the store's "
        f"identity unset, got {db.embedder_identity()}"
    )
    db.close()

    # ...and the store therefore reopens, which is the user-visible bug.
    db2 = YantrikDB(path, embedding_dim=DIM)
    db2.set_embedder(CountingEmbedder())
    db2.record_text(text="a second session writes fine")
    db2.close()


def test_native_embedder_still_runs_when_nothing_was_attached(tmp_path):
    """The other direction: don't fix precedence by breaking the default."""
    db = YantrikDB(_store(tmp_path), embedding_dim=DIM)
    assert db.has_embedder()
    rid = db.record_text(text="the bundled embedder handles this one")
    assert rid, "record_text must still work with no Python embedder attached"
    assert db.embedder_identity() is not None, (
        "a fingerprinted native embedder should still stamp the store"
    )
    db.close()


def test_recall_uses_the_attached_embedder_too(tmp_path):
    """Write and query must share a space, or recall is meaningless."""
    db = YantrikDB(_store(tmp_path), embedding_dim=DIM)
    emb = CountingEmbedder()
    db.set_embedder(emb)
    db.record_text(text="Priya Natarajan is the head of analytics at Corvid Labs.")
    emb.calls = 0
    hits = db.recall_text("who leads analytics at Corvid Labs", top_k=3)
    assert emb.calls >= 1, "the query was encoded by something other than the attached embedder"
    assert hits, "a store written and queried in one space should return its only record"
    db.close()


def test_a_fresh_temp_store_is_not_required(tmp_path):
    """Guard against the test passing only because tmp_path is pristine."""
    path = _store(tmp_path, "reused.db")
    with tempfile.TemporaryDirectory():
        db = YantrikDB(path, embedding_dim=DIM)
        db.set_embedder(CountingEmbedder())
        db.record_text(text="first")
        db.close()
    db = YantrikDB(path, embedding_dim=DIM)
    emb = CountingEmbedder()
    db.set_embedder(emb)
    emb.calls = 0
    db.record_text(text="second")
    assert emb.calls >= 1
    db.close()
