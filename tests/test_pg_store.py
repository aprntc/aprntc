"""Postgres-backed TrajectoryStore — parity tests against SQLite.

Skipped unless ``APRNTC_TEST_PG_URL`` points at a live Postgres (e.g.
``postgresql://user:pwd@localhost:5432/aprntc_test``). The tests run against
fresh tables (creates if missing) and clean up after themselves. Local Postgres
isn't required for the project — these are integration tests for the multi-worker
deployment path.

The parity matters: callers MUST be able to swap SQLite → Postgres without code
changes. Every public-API behaviour the SQLite suite covers is re-asserted here.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aprntc.trajectory import (
    Collector,
    Episode,
    Label,
    LabelSource,
    Outcome,
    PiiStatus,
    Turn,
)
from aprntc.trajectory.store import make_trajectory_store

_PG_URL = os.environ.get("APRNTC_TEST_PG_URL")
pytestmark = pytest.mark.skipif(
    not _PG_URL,
    reason="Set APRNTC_TEST_PG_URL=postgresql://… to enable Postgres parity tests.",
)


def _ep(**kw) -> Episode:
    kw.setdefault("task_input", "do a thing")
    kw.setdefault("collector", Collector.SDK_WRAPPER)
    return Episode(**kw)


@pytest.fixture()
def store():
    """A fresh-schema PostgresTrajectoryStore. Drops + recreates so each test starts clean."""
    pytest.importorskip("psycopg")
    import psycopg

    # Drop existing tables (the schema is autocreated by the store constructor).
    with psycopg.connect(_PG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            for tbl in ("subject_episodes", "labels", "outcomes", "episodes"):
                cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    s = make_trajectory_store(_PG_URL)
    yield s
    s.close()


# -- parity of writes + reads --------------------------------------------------

def test_factory_dispatches_to_postgres_backend(store):
    """make_trajectory_store(postgresql://…) returns the Postgres class."""
    from aprntc.trajectory.pg_store import PostgresTrajectoryStore
    assert isinstance(store, PostgresTrajectoryStore)


def test_put_and_get_roundtrip(store):
    e = _ep(trace_id="t1", turns=[Turn(turn_index=0)])
    eid = store.put_episode(e, scrub=False)
    back = store.get_episode(eid)
    assert back.episode_id == eid and back.trace_id == "t1"


def test_get_missing_raises(store):
    with pytest.raises(KeyError):
        store.get_episode("ep_does_not_exist")


def test_put_scrubs_pii_at_ingest_by_default(store):
    e = _ep(task_input="contact me at foo@example.com")
    eid = store.put_episode(e)  # scrub=True default
    back = store.get_episode(eid)
    assert "foo@example.com" not in back.task_input
    assert back.pii_status is PiiStatus.SCRUBBED


def test_attach_label_append_only(store):
    eid = store.put_episode(_ep(turns=[Turn(turn_index=0)]), scrub=False)
    store.attach_label(eid, Label(source=LabelSource.OUTCOME, score=0.9, confidence=0.9))
    store.attach_label(eid, Label(source=LabelSource.JUDGE, score=0.6, confidence=0.5))
    labels = store.labels_for(eid)
    assert [l.source for l in labels] == [LabelSource.OUTCOME, LabelSource.JUDGE]


def test_attach_label_unknown_episode_raises(store):
    with pytest.raises(KeyError):
        store.attach_label("nope", Label(source=LabelSource.JUDGE, score=0.5))


def test_outcome_join_by_trace(store):
    eid = store.put_episode(_ep(trace_id="t1", turns=[Turn(turn_index=0)]), scrub=False)
    store.attach_outcome(Outcome(trace_id="t1", resolved=True, correct=True))
    outs = store.outcomes_for("t1")
    assert len(outs) == 1 and outs[0].resolved is True


def test_query_filters(store):
    a = _ep(generation_id="G0", collector=Collector.SDK_WRAPPER,
            turns=[Turn(turn_index=0)])
    b = _ep(generation_id="G1", collector=Collector.EGRESS_PROXY,
            turns=[Turn(turn_index=0)])
    store.put_episode(a, scrub=False)
    store.put_episode(b, scrub=False)
    assert len(store.query(generation_id="G0")) == 1
    assert len(store.query(collector="egress_proxy")) == 1
    assert store.count() == 2


def test_counts_by_collector(store):
    """A1 chip-source endpoint: parity with SQLite's GROUP BY collector."""
    store.put_episode(_ep(collector=Collector.SDK_WRAPPER, turns=[Turn(turn_index=0)]), scrub=False)
    store.put_episode(_ep(collector=Collector.SDK_WRAPPER, turns=[Turn(turn_index=0)]), scrub=False)
    store.put_episode(_ep(collector=Collector.EGRESS_PROXY, turns=[Turn(turn_index=0)]), scrub=False)
    assert store.counts_by_collector() == {"sdk_wrapper": 2, "egress_proxy": 1}


def test_fused_reward_outcome_outweighs_judge(store):
    eid = store.put_episode(_ep(turns=[Turn(turn_index=0)]), scrub=False)
    store.attach_label(eid, Label(source=LabelSource.OUTCOME, score=1.0, confidence=1.0))
    store.attach_label(eid, Label(source=LabelSource.JUDGE, score=0.0, confidence=0.9))
    reward, conf = store.fused_reward(eid)
    assert reward > 0.5  # OUTCOME weight 1.0 dominates JUDGE 0.4
    assert conf == 1.0   # anchored by OUTCOME


def test_delete_by_subject_cascades_labels(store):
    e = _ep(turns=[Turn(turn_index=0)])
    eid = store.put_episode(e, subject_ids=["user-42"], scrub=False)
    store.attach_label(eid, Label(source=LabelSource.JUDGE, score=0.5))
    n = store.delete_by_subject("user-42")
    assert n == 1
    with pytest.raises(KeyError):
        store.get_episode(eid)
    assert store.labels_for(eid) == []  # ON DELETE CASCADE


def test_purge_expired_ttl(store):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    store.put_episode(_ep(retention_until=past, turns=[Turn(turn_index=0)]), scrub=False)
    store.put_episode(_ep(retention_until=future, turns=[Turn(turn_index=0)]), scrub=False)
    store.put_episode(_ep(retention_until=None, turns=[Turn(turn_index=0)]), scrub=False)
    purged = store.purge_expired()
    assert purged == 1  # only the past one
    assert store.count() == 2


def test_upsert_replaces_episode_body(store):
    """ON CONFLICT DO UPDATE keeps the row id and replaces the body — like SQLite's
    INSERT OR REPLACE. Re-putting the same episode_id with different content updates it."""
    e1 = _ep(episode_id="ep_pin", task_input="v1", turns=[Turn(turn_index=0)])
    e2 = _ep(episode_id="ep_pin", task_input="v2", turns=[Turn(turn_index=0)])
    store.put_episode(e1, scrub=False)
    store.put_episode(e2, scrub=False)
    assert store.count() == 1
    assert store.get_episode("ep_pin").task_input == "v2"
