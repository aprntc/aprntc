"""Web dashboard API tests — endpoints over gate/lineage/trajectories/lessons."""

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aprntc.web.app import AppState, create_app
from aprntc.trajectory import (
    Collector,
    Episode,
    Label,
    LabelSource,
    TrajectoryStore,
    Turn,
)


@pytest.fixture()
def store():
    s = TrajectoryStore(":memory:")
    yield s
    s.close()


def _client(tmp_path, store=None, memory_search=None, bundle=None, agent_run=None):
    if bundle is not None:
        (tmp_path / "bundle.json").write_text(json.dumps(bundle))
    state = AppState(
        lineage_path=str(tmp_path / "lineage.json"),
        bundle_path=str(tmp_path / "bundle.json"),
        auto_policy_path=str(tmp_path / "auto_policy.json"),
        store=store,
        memory_search=memory_search,
        agent_run=agent_run,
    )
    return TestClient(create_app(state))


# ─── health ─────────────────────────────────────────────────────────────────

def test_health(tmp_path):
    r = _client(tmp_path).get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ─── review / gate ──────────────────────────────────────────────────────────

def test_review_unavailable_without_bundle(tmp_path):
    r = _client(tmp_path).get("/api/review")
    assert r.status_code == 200 and r.json()["available"] is False


def test_review_returns_gate_and_diff(tmp_path):
    bundle = {
        "candidate_playbook_hash": "pb_abc",
        "gate": {"win_rate": 0.25, "ci_low": 0.05, "loss_rate": 0.75,
                 "win_rate_ok": False, "ci_ok": False, "loss_ok": False,
                 "regression_ok": True, "safety_ok": True, "passed": False},
        "diff": {"add_directives": ["cite docs"], "provenance": {"cite docs": ["les_1"]}},
    }
    r = _client(tmp_path, bundle=bundle).get("/api/review")
    body = r.json()
    assert body["available"] and body["passed"] is False
    assert body["candidate_playbook_hash"] == "pb_abc"
    assert body["diff"]["add_directives"] == ["cite docs"]


def test_review_computes_passed_when_missing(tmp_path):
    bundle = {"gate": {"win_rate_ok": True, "ci_ok": True, "loss_ok": True,
                       "regression_ok": True, "safety_ok": True}}
    r = _client(tmp_path, bundle=bundle).get("/api/review")
    assert r.json()["passed"] is True


# ─── lineage ────────────────────────────────────────────────────────────────

def test_lineage_promote_and_rollback_flow(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/lineage").json()["current"] is None

    # first promote registers the parent
    r1 = c.post("/api/lineage/promote", json={"playbook_hash": "pb_g0"})
    assert r1.json()["action"] == "registered_parent"

    r2 = c.post("/api/lineage/promote", json={"playbook_hash": "pb_g1",
                                              "gate_summary": "win 60%"})
    assert r2.json()["action"] == "promoted"
    assert r2.json()["current"]["generation"] == 1

    cur = c.get("/api/lineage").json()
    assert cur["current"]["generation"] == 1 and len(cur["generations"]) == 2

    r3 = c.post("/api/lineage/rollback")
    assert r3.json()["current"]["generation"] == 0


def test_promote_requires_hash(tmp_path):
    r = _client(tmp_path).post("/api/lineage/promote", json={})
    assert r.status_code == 400


def test_rollback_at_g0_conflicts(tmp_path):
    c = _client(tmp_path)
    c.post("/api/lineage/promote", json={"playbook_hash": "pb_g0"})
    r = c.post("/api/lineage/rollback")
    assert r.status_code == 409  # nothing to roll back to


# ─── trajectories ───────────────────────────────────────────────────────────

def test_list_and_get_trajectory(tmp_path, store):
    ep = Episode(task_input="refund?", collector=Collector.SDK_WRAPPER,
                 final_output="30 days", generation_id="G0", turns=[Turn(turn_index=0)])
    eid = store.put_episode(ep, scrub=False)
    store.attach_label(eid, Label(source=LabelSource.OUTCOME, score=0.9, confidence=0.9))
    c = _client(tmp_path, store=store)

    lst = c.get("/api/trajectories").json()
    assert lst["count"] == 1
    summary = lst["episodes"][0]
    assert summary["episode_id"] == eid and summary["reward"] is not None

    detail = c.get(f"/api/trajectories/{eid}").json()
    assert detail["task_input"] == "refund?"
    assert detail["fused_reward"]["reward"] == pytest.approx(0.9)
    assert len(detail["labels"]) == 1


def test_get_missing_trajectory_404(tmp_path, store):
    r = _client(tmp_path, store=store).get("/api/trajectories/ep_nope")
    assert r.status_code == 404


def test_auto_promotion_policy_default_off(tmp_path):
    """A4: GET /api/policy/auto-promote returns the default-off conservative policy."""
    c = _client(tmp_path)
    r = c.get("/api/policy/auto-promote").json()
    assert r["enabled"] is False
    # bar margins are above the gate bar (auto requires stricter)
    assert r["win_rate_min"] > 0.5 and r["loss_rate_max"] < 0.10


def test_auto_promotion_policy_partial_update_persists(tmp_path):
    """A4: POST /api/policy/auto-promote — partial updates persist; unset fields keep priors."""
    c = _client(tmp_path)
    r = c.post("/api/policy/auto-promote", json={"enabled": True}).json()
    assert r["enabled"] is True
    # round-trip — flip survives a fresh GET (file-backed)
    assert c.get("/api/policy/auto-promote").json()["enabled"] is True


def test_review_auto_decision_human_review(tmp_path):
    """A4: a passing gate but disabled policy + low trust → HUMAN_REVIEW with reasons."""
    bundle = {
        "candidate_playbook_hash": "h",
        "gate": {
            "n": 30, "wins": 24, "losses": 1, "ties": 5,
            "win_rate": 0.80, "ci_low": 0.62, "ci_high": 0.91,
            "loss_rate": 0.03,
            "regression_failures": 0, "safety_failures": 0,
            "win_rate_ok": True, "ci_ok": True, "loss_ok": True,
            "regression_ok": True, "safety_ok": True,
        },
        "diff": {"add_directives": ["always cite"], "add_exemplars": [], "add_watch_out": []},
    }
    c = _client(tmp_path, bundle=bundle)
    r = c.get("/api/review").json()
    auto = r["auto"]
    # policy default-off + no trust signal → HUMAN_REVIEW (never silent reject)
    assert auto["action"] == "human_review"
    assert auto["policy_enabled"] is False
    assert any("disabled" in reason for reason in auto["reasons"])


def test_review_auto_decision_reject_when_gate_fails(tmp_path):
    """A4: failed gate → REJECT (auto can't bypass hard gates)."""
    bundle = {
        "candidate_playbook_hash": "h",
        "gate": {
            "n": 30, "win_rate": 0.40, "ci_low": 0.20, "loss_rate": 0.30,
            "regression_failures": 0, "safety_failures": 0,
            "win_rate_ok": False, "ci_ok": False, "loss_ok": False,
            "regression_ok": True, "safety_ok": True,
        },
        "diff": {"add_directives": ["a"], "add_exemplars": [], "add_watch_out": []},
    }
    c = _client(tmp_path, bundle=bundle)
    r = c.get("/api/review").json()
    assert r["auto"]["action"] == "reject"


def test_review_no_bundle_no_auto(tmp_path):
    """A4: empty bundle (no candidate) → auto is null (nothing to decide)."""
    c = _client(tmp_path)
    assert c.get("/api/review").json()["auto"] is None


def test_trajectories_collector_filter_and_counts(tmp_path, store):
    """A1: /api/trajectories accepts ?collector= AND returns counts per collector."""
    sdk_ep = Episode(task_input="t1", collector=Collector.SDK_WRAPPER,
                     final_output="a", turns=[Turn(turn_index=0)])
    proxy_ep = Episode(task_input="t2", collector=Collector.EGRESS_PROXY,
                       final_output="b", turns=[Turn(turn_index=0)])
    store.put_episode(sdk_ep, scrub=False)
    store.put_episode(proxy_ep, scrub=False)
    c = _client(tmp_path, store=store)

    # by_collector groups episodes by collector for the UI filter chips.
    all_lst = c.get("/api/trajectories").json()
    assert all_lst["count"] == 2
    assert all_lst["by_collector"] == {"sdk_wrapper": 1, "egress_proxy": 1}
    assert len(all_lst["episodes"]) == 2

    # ?collector= narrows the list to just that collector, totals unchanged.
    only_proxy = c.get("/api/trajectories?collector=egress_proxy").json()
    assert only_proxy["count"] == 2  # global total
    assert len(only_proxy["episodes"]) == 1
    assert only_proxy["episodes"][0]["collector"] == "egress_proxy"


def test_trajectories_empty_without_store(tmp_path):
    r = _client(tmp_path).get("/api/trajectories")
    assert r.json() == {"episodes": [], "count": 0, "by_collector": {}}


# ─── lessons ────────────────────────────────────────────────────────────────

def test_lessons_search(tmp_path):
    def fake_search(q, k):
        return [{"lesson_id": "les_1", "content": "cite docs", "reward": 0.9, "score": 0.95}]
    c = _client(tmp_path, memory_search=fake_search)
    body = c.get("/api/lessons", params={"q": "citation", "k": 5}).json()
    assert body["available"] and len(body["lessons"]) == 1
    assert body["lessons"][0]["lesson_id"] == "les_1"


def test_lessons_unavailable_without_memory(tmp_path):
    body = _client(tmp_path).get("/api/lessons", params={"q": "x"}).json()
    assert body["available"] is False


def test_lessons_memory_error_is_surfaced_not_500(tmp_path):
    def boom(q, k):
        raise RuntimeError("vikingdb down")
    c = _client(tmp_path, memory_search=boom)
    body = c.get("/api/lessons", params={"q": "x"}).json()
    assert body["available"] is True and "vikingdb down" in body["error"]


# ─── try an agent ───────────────────────────────────────────────────────────

def test_list_agents(tmp_path):
    body = _client(tmp_path, agent_run=lambda a, t: {}).get("/api/agents").json()
    assert body["available"] is True
    ids = {a["id"] for a in body["agents"]}
    assert ids == {"byteplus", "support", "rag"}
    assert all(a["examples"] for a in body["agents"])


def test_list_agents_unavailable_without_runtime(tmp_path):
    assert _client(tmp_path).get("/api/agents").json()["available"] is False


def test_run_agent_invokes_runner(tmp_path):
    seen = {}
    def runner(agent_id, task):
        seen["call"] = (agent_id, task)
        return {"answer": "ok", "episode_id": "ep_1", "reward": 1.0, "steps": []}
    c = _client(tmp_path, agent_run=runner)
    r = c.post("/api/agents/run", json={"agent_id": "support", "task": "refund?"})
    assert r.status_code == 200 and r.json()["answer"] == "ok"
    assert seen["call"] == ("support", "refund?")


def test_list_agents_includes_byteplus(tmp_path):
    body = _client(tmp_path, agent_run=lambda a, t: {}).get("/api/agents").json()
    bp = next((a for a in body["agents"] if a["id"] == "byteplus"), None)
    assert bp is not None and "ModelArk" in bp["description"]
    assert bp["examples"]


def test_run_agent_validation(tmp_path):
    c = _client(tmp_path, agent_run=lambda a, t: {})
    assert c.post("/api/agents/run", json={"agent_id": "bad", "task": "x"}).status_code == 400
    assert c.post("/api/agents/run", json={"agent_id": "byteplus", "task": "  "}).status_code == 400
    # byteplus is now a valid id (would 503 only because no runtime, not 400)
    assert c.post("/api/agents/run", json={"agent_id": "byteplus", "task": "q"}).status_code == 200


# ─── user feedback (thumbs) ──────────────────────────────────────────────────

def test_feedback_attaches_explicit_label(tmp_path, store):
    ep = Episode(task_input="q", collector=Collector.SDK_WRAPPER, final_output="a")
    eid = store.put_episode(ep, scrub=False)
    c = _client(tmp_path, store=store)
    r = c.post("/api/feedback", json={"episode_id": eid, "vote": "up"})
    assert r.status_code == 200 and r.json()["ok"] is True
    labels = store.labels_for(eid)
    assert any(l.source is LabelSource.USER_EXPLICIT and l.score == 1.0 for l in labels)


def test_feedback_down_is_zero(tmp_path, store):
    ep = Episode(task_input="q", collector=Collector.SDK_WRAPPER, final_output="a")
    eid = store.put_episode(ep, scrub=False)
    c = _client(tmp_path, store=store)
    c.post("/api/feedback", json={"episode_id": eid, "vote": "down"})
    assert any(l.score == 0.0 for l in store.labels_for(eid))


def test_feedback_validation(tmp_path, store):
    c = _client(tmp_path, store=store)
    assert c.post("/api/feedback", json={"episode_id": "x", "vote": "maybe"}).status_code == 400
    assert c.post("/api/feedback", json={"episode_id": "nope", "vote": "up"}).status_code == 404


def test_run_agent_503_without_runtime(tmp_path):
    r = _client(tmp_path).post("/api/agents/run", json={"agent_id": "support", "task": "hi"})
    assert r.status_code == 503


# ─── from_env factory (persistent backends, graceful degradation) ───────────

def test_from_env_wires_persistent_store(tmp_path, monkeypatch):
    from aprntc.web.app import AppState
    # no VikingDB creds in env, and skip .env loading → memory_search stays None,
    # but a real persistent store is still created.
    for var in ("VIKINGDB_AK", "VIKINGDB_SK"):
        monkeypatch.delenv(var, raising=False)
    state = AppState.from_env(
        db_path=str(tmp_path / "aprntc.db"),
        lineage_path=str(tmp_path / "lineage.json"),
        bundle_path=str(tmp_path / "bundle.json"),
        load_dotenv=False,
    )
    assert state.store is not None              # real persistent store wired
    assert state.memory_search is None          # degraded cleanly (no creds)
    # the wired app answers over the store
    c = TestClient(create_app(state))
    assert c.get("/api/trajectories").json()["count"] == 0
    assert c.get("/api/lessons", params={"q": "x"}).json()["available"] is False
