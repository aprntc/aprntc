"""Append-only audit log for auto-promotion decisions."""

import datetime as dt
import json

import pytest

from aprntc.promote.audit import AuditRecord, append_audit, read_audit


def _fixed_now(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


def test_append_writes_jsonl_line(tmp_path):
    p = tmp_path / "audit.jsonl"
    rec = append_audit(
        p,
        candidate_playbook_hash="abc",
        action="human_review",
        policy_enabled=False,
        reasons=["no trust signal available"],
        trust_value=None,
        trust_n=0,
        gate_summary="n=30 win=80%",
        now=_fixed_now("2026-06-21T12:00:00"),
    )
    assert rec is not None and rec.action == "human_review"
    line = p.read_text(encoding="utf-8").strip()
    d = json.loads(line)
    assert d["candidate_playbook_hash"] == "abc"
    assert d["action"] == "human_review"
    assert d["reasons"] == ["no trust signal available"]
    assert d["ts"].startswith("2026-06-21T12:00:00")


def test_consecutive_identical_decisions_are_deduped(tmp_path):
    """The dashboard polls /api/review; the audit log must NOT grow per poll."""
    p = tmp_path / "audit.jsonl"
    kwargs = dict(candidate_playbook_hash="abc", action="human_review",
                  policy_enabled=False, reasons=["disabled"], trust_value=None, trust_n=0)
    r1 = append_audit(p, **kwargs, now=_fixed_now("2026-06-21T12:00:00"))
    r2 = append_audit(p, **kwargs, now=_fixed_now("2026-06-21T12:00:05"))
    r3 = append_audit(p, **kwargs, now=_fixed_now("2026-06-21T12:00:10"))
    assert r1 is not None and r2 is None and r3 is None
    # Only one line on disk.
    assert len([ln for ln in p.read_text().splitlines() if ln.strip()]) == 1


def test_change_in_any_field_creates_new_record(tmp_path):
    """Real changes — action flips, trust crosses threshold, candidate hash updates — each line up."""
    p = tmp_path / "audit.jsonl"
    base = dict(candidate_playbook_hash="abc", action="human_review",
                policy_enabled=False, reasons=["disabled"], trust_value=None, trust_n=0)
    append_audit(p, **base, now=_fixed_now("2026-06-21T12:00:00"))

    # Operator toggles policy on → reasons change → new record.
    append_audit(p, **{**base, "policy_enabled": True, "reasons": ["no trust signal"]},
                 now=_fixed_now("2026-06-21T12:01:00"))
    # Trust threshold met → action flips to auto_promote → new record.
    append_audit(p, **{**base, "policy_enabled": True, "action": "auto_promote",
                       "reasons": [], "trust_value": 0.92, "trust_n": 18},
                 now=_fixed_now("2026-06-21T12:02:00"))
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3
    # Final record reflects the AUTO_PROMOTE state.
    last = json.loads(lines[-1])
    assert last["action"] == "auto_promote" and last["trust_value"] == 0.92


def test_read_returns_newest_first_with_limit(tmp_path):
    p = tmp_path / "audit.jsonl"
    for i in range(5):
        append_audit(
            p, candidate_playbook_hash=f"h{i}", action="human_review",
            policy_enabled=False, reasons=[f"r{i}"], trust_value=None, trust_n=0,
            now=_fixed_now(f"2026-06-21T12:0{i}:00"),
        )
    recs = read_audit(p, limit=3)
    assert len(recs) == 3
    # Newest first — h4 written last, so recs[0] is h4.
    assert [r.candidate_playbook_hash for r in recs] == ["h4", "h3", "h2"]


def test_read_missing_path_returns_empty(tmp_path):
    assert read_audit(tmp_path / "no-such.jsonl") == []


def test_read_skips_corrupted_lines(tmp_path):
    """A torn last line (process killed mid-write) doesn't break the reader."""
    p = tmp_path / "audit.jsonl"
    p.write_text(
        '{"ts":"2026-06-21T12:00:00","candidate_playbook_hash":"a","action":"human_review",'
        '"policy_enabled":false,"reasons":["x"],"trust_value":null,"trust_n":0,"gate_summary":null}\n'
        '{partially-written corrupt line that fails json.loads\n',
        encoding="utf-8",
    )
    recs = read_audit(p)
    assert len(recs) == 1 and recs[0].candidate_playbook_hash == "a"


# ─── API integration ─────────────────────────────────────────────────────────

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aprntc.trajectory import (  # noqa: E402
    Collector,
    Episode,
    Label,
    LabelSource,
    TrajectoryStore,
    Turn,
)
from aprntc.web.app import AppState, create_app  # noqa: E402


def _bundle(passing: bool = True) -> dict:
    gate = {
        "n": 30, "wins": 24, "losses": 1, "ties": 5,
        "win_rate": 0.80, "ci_low": 0.62, "ci_high": 0.91,
        "loss_rate": 0.03,
        "regression_failures": 0, "safety_failures": 0,
        "win_rate_ok": passing, "ci_ok": passing, "loss_ok": passing,
        "regression_ok": True, "safety_ok": True,
    }
    return {
        "candidate_playbook_hash": "h1",
        "gate": gate,
        "diff": {"add_directives": ["always cite"], "add_exemplars": [], "add_watch_out": []},
    }


def _client(tmp_path, *, bundle, store=None):
    (tmp_path / "bundle.json").write_text(json.dumps(bundle))
    return TestClient(create_app(AppState(
        lineage_path=str(tmp_path / "lineage.json"),
        bundle_path=str(tmp_path / "bundle.json"),
        auto_policy_path=str(tmp_path / "auto_policy.json"),
        auto_audit_path=str(tmp_path / "audit.jsonl"),
        store=store,
    )))


def test_review_appends_audit_record(tmp_path):
    """Calling /api/review writes an audit record; the audit endpoint surfaces it."""
    c = _client(tmp_path, bundle=_bundle())
    # First call writes the audit line.
    review = c.get("/api/review").json()
    assert review["auto"] is not None
    log = c.get("/api/policy/auto-promote/audit").json()
    assert log["count"] == 1
    rec = log["records"][0]
    assert rec["candidate_playbook_hash"] == "h1"
    assert rec["action"] == "human_review"  # default-off policy → human_review
    assert "no trust signal available" in rec["reasons"]


def test_repeated_review_polls_do_not_dup_audit(tmp_path):
    """Hammering /api/review (dashboard auto-refresh) must not bloat the log."""
    c = _client(tmp_path, bundle=_bundle())
    for _ in range(5):
        c.get("/api/review")
    assert c.get("/api/policy/auto-promote/audit").json()["count"] == 1


def test_policy_toggle_creates_new_audit_record(tmp_path):
    """Enabling the policy changes the reasons → new audit line."""
    c = _client(tmp_path, bundle=_bundle())
    c.get("/api/review")
    c.post("/api/policy/auto-promote", json={"enabled": True})
    c.get("/api/review")
    log = c.get("/api/policy/auto-promote/audit").json()
    assert log["count"] == 2
    # Newest first: the second entry has policy_enabled=True.
    assert log["records"][0]["policy_enabled"] is True
    assert log["records"][1]["policy_enabled"] is False


def test_trust_climbing_creates_new_audit_record(tmp_path):
    """Trust crossing min_n flips the decision; the audit captures the transition."""
    store = TrajectoryStore(":memory:")
    c = _client(tmp_path, bundle=_bundle(), store=store)
    c.get("/api/review")  # baseline: trust None
    # Seed 10 matching judge+outcome → trust 1.0.
    for i in range(10):
        ep = Episode(task_input=f"q{i}", collector=Collector.SDK_WRAPPER,
                     final_output="ok", turns=[Turn(turn_index=0)])
        eid = store.put_episode(ep, scrub=False)
        store.attach_label(eid, Label(source=LabelSource.OUTCOME, score=1.0, confidence=1.0))
        store.attach_label(eid, Label(source=LabelSource.JUDGE, score=1.0, confidence=0.9))
    c.get("/api/review")
    log = c.get("/api/policy/auto-promote/audit").json()
    assert log["count"] == 2
    # The newer record has trust populated; the older one does not.
    assert log["records"][0]["trust_value"] == 1.0 and log["records"][0]["trust_n"] == 10
    assert log["records"][1]["trust_value"] is None
    store.close()
