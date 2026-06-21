"""B2 auth + multi-tenancy — key auth (hashed), per-tenant isolation, API enforcement."""

import pytest

from aprntc.tenancy import (
    TenantResolver,
    TenantStore,
    hash_key,
    new_api_key,
)


# ─── auth / key handling ─────────────────────────────────────────────────────

def test_keys_are_unique_and_prefixed():
    a, b = new_api_key(), new_api_key()
    assert a != b and a.startswith("aprntc_sk_")


def test_create_tenant_issues_key_and_stores_only_hash(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    tenant, key = ts.create_tenant("acme", name="Acme")
    assert tenant.tenant_id == "acme"
    # plaintext key is NOT stored — only its hash
    assert key not in str(tenant.to_dict())
    assert hash_key(key) in tenant.key_hashes


def test_authenticate_valid_and_invalid(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    _, key = ts.create_tenant("acme")
    assert ts.authenticate(key).tenant_id == "acme"
    assert ts.authenticate("wrong") is None
    assert ts.authenticate(None) is None


def test_key_rotation_both_keys_work(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    _, k1 = ts.create_tenant("acme")
    k2 = ts.issue_key("acme")
    assert ts.authenticate(k1).tenant_id == "acme"
    assert ts.authenticate(k2).tenant_id == "acme"


def test_revoke_key(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    _, key = ts.create_tenant("acme")
    assert ts.revoke_key(key) is True
    assert ts.authenticate(key) is None


def test_deactivated_tenant_cannot_auth(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    _, key = ts.create_tenant("acme")
    ts.deactivate("acme")
    assert ts.authenticate(key) is None


def test_duplicate_tenant_rejected(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    ts.create_tenant("acme")
    with pytest.raises(ValueError):
        ts.create_tenant("acme")


def test_tenant_store_persists(tmp_path):
    path = tmp_path / "t.json"
    _, key = TenantStore(path).create_tenant("acme")
    assert TenantStore(path).authenticate(key).tenant_id == "acme"  # reloaded


# ─── resolver isolation ──────────────────────────────────────────────────────

def test_resolver_gives_isolated_paths(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    _, ka = ts.create_tenant("a")
    _, kb = ts.create_tenant("b")
    r = TenantResolver(ts, data_root=str(tmp_path / "data"))
    ca, cb = r.resolve(ka), r.resolve(kb)
    assert ca.tenant_id == "a" and cb.tenant_id == "b"
    # every storage path is namespaced per tenant -> no shared files
    assert ca.db_path != cb.db_path and ca.playbooks_path != cb.playbooks_path
    assert "/a/" in ca.db_path and "/b/" in cb.db_path


def test_resolver_rejects_bad_key(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    r = TenantResolver(ts, data_root=str(tmp_path / "data"))
    assert r.resolve("nope") is None


# ─── API enforcement + isolation ─────────────────────────────────────────────

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aprntc.web.app import AppState, create_app  # noqa: E402


def _multitenant_client(tmp_path):
    ts = TenantStore(tmp_path / "t.json")
    _, ka = ts.create_tenant("tenant-a")
    _, kb = ts.create_tenant("tenant-b")
    resolver = TenantResolver(ts, data_root=str(tmp_path / "data"))
    app = create_app(AppState(
        lineage_path=str(tmp_path / "l.json"),
        bundle_path=str(tmp_path / "b.json"),
        tenant_resolver=resolver,
    ))
    return TestClient(app), ka, kb


def test_api_requires_key_when_multitenant(tmp_path):
    c, ka, kb = _multitenant_client(tmp_path)
    # no key -> 401
    assert c.get("/api/playbooks/agent/active").status_code == 401
    # bad key -> 401
    assert c.get("/api/playbooks/agent/active",
                 headers={"X-API-Key": "bad"}).status_code == 401


def test_api_tenants_are_isolated(tmp_path):
    c, ka, kb = _multitenant_client(tmp_path)
    # tenant A registers an agent playbook
    r = c.post("/api/playbooks/shared-id/register",
               json={"system_prompt": "Tenant A prompt"},
               headers={"X-API-Key": ka})
    assert r.status_code == 200

    # tenant A can read it
    ra = c.get("/api/playbooks/shared-id/active", headers={"X-API-Key": ka})
    assert ra.status_code == 200 and "Tenant A" in ra.json()["playbook"]["system_prompt"]

    # tenant B (same agent_id!) does NOT see tenant A's data -> 404, isolated
    rb = c.get("/api/playbooks/shared-id/active", headers={"X-API-Key": kb})
    assert rb.status_code == 404


def test_bearer_token_also_accepted(tmp_path):
    c, ka, kb = _multitenant_client(tmp_path)
    c.post("/api/playbooks/x/register", json={"system_prompt": "P"},
           headers={"Authorization": f"Bearer {ka}"})
    r = c.get("/api/playbooks/x/active", headers={"Authorization": f"Bearer {ka}"})
    assert r.status_code == 200


def test_single_tenant_mode_unaffected(tmp_path):
    # no resolver -> dev/single-tenant mode, no key required (existing behavior)
    from aprntc.serving import PlaybookRegistry
    app = create_app(AppState(playbooks=PlaybookRegistry(tmp_path / "pb.json")))
    c = TestClient(app)
    c.post("/api/playbooks/x/register", json={"system_prompt": "P"})
    assert c.get("/api/playbooks/x/active").status_code == 200  # works with no key


# ─── Data endpoints: tenant isolation (extended from playbook-only B2) ───────

from aprntc.trajectory import (  # noqa: E402
    Collector,
    Episode,
    Label,
    LabelSource,
    Turn,
)


def test_trajectories_require_key_in_tenant_mode(tmp_path):
    c, ka, kb = _multitenant_client(tmp_path)
    # no credentials -> 401 (data endpoints now gate the same as playbook ones)
    assert c.get("/api/trajectories").status_code == 401
    assert c.get("/api/trajectories/ep_anything").status_code == 401
    assert c.get("/api/trajectories", headers={"X-API-Key": "bad"}).status_code == 401


def test_trajectories_are_isolated_per_tenant(tmp_path):
    c, ka, kb = _multitenant_client(tmp_path)
    # Tenant A's runner writes into A's own store; this simulates that by going
    # through the resolver path manually.
    from aprntc.tenancy import TenantStore as _TS
    from aprntc.tenancy.resolver import TenantResolver as _TR
    ts = _TS(tmp_path / "t.json")
    resolver = _TR(ts, data_root=str(tmp_path / "data"))
    ctx_a = resolver.resolve(ka)
    from aprntc.trajectory import TrajectoryStore
    store_a = TrajectoryStore(ctx_a.db_path)
    ep = Episode(task_input="A's refund?", collector=Collector.SDK_WRAPPER,
                 final_output="30 days", generation_id="G0", turns=[Turn(turn_index=0)])
    eid = store_a.put_episode(ep, scrub=False)
    store_a.close()

    # Tenant A sees it.
    la = c.get("/api/trajectories", headers={"X-API-Key": ka}).json()
    assert la["count"] == 1 and la["episodes"][0]["episode_id"] == eid

    # Tenant B is fully empty (same agent, same machine — different store on disk).
    lb = c.get("/api/trajectories", headers={"X-API-Key": kb}).json()
    assert lb["count"] == 0 and lb["episodes"] == []

    # Tenant B trying to fetch tenant A's episode id explicitly -> 404 (isolated, not 403).
    rb = c.get(f"/api/trajectories/{eid}", headers={"X-API-Key": kb})
    assert rb.status_code == 404


def test_lineage_is_isolated_per_tenant(tmp_path):
    c, ka, kb = _multitenant_client(tmp_path)
    # A promotes a parent (G0); B's lineage stays empty.
    r = c.post("/api/lineage/promote", json={"playbook_hash": "abc123"},
               headers={"X-API-Key": ka})
    assert r.status_code == 200

    la = c.get("/api/lineage", headers={"X-API-Key": ka}).json()
    assert la["current"] is not None and la["current"]["playbook_hash"] == "abc123"

    lb = c.get("/api/lineage", headers={"X-API-Key": kb}).json()
    assert lb["current"] is None and lb["generations"] == []


def test_feedback_writes_to_tenant_store(tmp_path):
    c, ka, kb = _multitenant_client(tmp_path)
    # Seed tenant A's store with one episode (without going through agents/run).
    from aprntc.tenancy import TenantStore as _TS
    from aprntc.tenancy.resolver import TenantResolver as _TR
    from aprntc.trajectory import TrajectoryStore
    ts = _TS(tmp_path / "t.json")
    resolver = _TR(ts, data_root=str(tmp_path / "data"))
    ctx_a = resolver.resolve(ka)
    store_a = TrajectoryStore(ctx_a.db_path)
    ep = Episode(task_input="q?", collector=Collector.SDK_WRAPPER,
                 final_output="answer", generation_id="G0", turns=[Turn(turn_index=0)])
    eid = store_a.put_episode(ep, scrub=False)
    store_a.close()

    # A's thumbs-up persists to A's store.
    r = c.post("/api/feedback", json={"episode_id": eid, "vote": "up"},
               headers={"X-API-Key": ka})
    assert r.status_code == 200 and r.json()["fused_reward"] == 1.0

    # B can't 👍 A's episode — it's not in B's store -> 404.
    r2 = c.post("/api/feedback", json={"episode_id": eid, "vote": "up"},
                headers={"X-API-Key": kb})
    assert r2.status_code == 404


def test_review_bundle_is_per_tenant(tmp_path):
    # Write tenant A a bundle directly into A's isolated bundle_path.
    from aprntc.tenancy import TenantStore as _TS
    from aprntc.tenancy.resolver import TenantResolver as _TR
    ts = _TS(tmp_path / "t.json")
    _, ka = ts.create_tenant("tenant-a")
    _, kb = ts.create_tenant("tenant-b")
    resolver = _TR(ts, data_root=str(tmp_path / "data"))
    ctx_a = resolver.resolve(ka)
    Path = __import__("pathlib").Path
    json_mod = __import__("json")
    Path(ctx_a.bundle_path).write_text(
        json_mod.dumps({"candidate_playbook_hash": "h-A", "gate": {"passed": True}}),
        encoding="utf-8")

    app = create_app(AppState(tenant_resolver=resolver))
    c = TestClient(app)

    ra = c.get("/api/review", headers={"X-API-Key": ka}).json()
    assert ra["available"] and ra["candidate_playbook_hash"] == "h-A"

    rb = c.get("/api/review", headers={"X-API-Key": kb}).json()
    assert rb["available"] is False  # B has no bundle


def test_lessons_endpoint_requires_auth_in_tenant_mode(tmp_path):
    c, ka, kb = _multitenant_client(tmp_path)
    assert c.get("/api/lessons?q=refund").status_code == 401
    # With a valid key the auth gate lets the request through. Whether VikingDB
    # is available depends on local config — we only verify the gate passed (200).
    r = c.get("/api/lessons?q=refund", headers={"X-API-Key": ka})
    assert r.status_code == 200 and "available" in r.json()


def test_fleet_is_isolated_per_tenant(tmp_path):
    """A6: fleet/{tenant_a} cannot see agents registered to fleet/{tenant_b}."""
    c, ka, kb = _multitenant_client(tmp_path)
    # Tenant A registers a support agent.
    r = c.post("/api/fleet/register",
               json={"agent_id": "shared-id", "domain": "support"},
               headers={"X-API-Key": ka})
    assert r.status_code == 200

    la = c.get("/api/fleet", headers={"X-API-Key": ka}).json()["agents"]
    assert len(la) == 1 and la[0]["agent_id"] == "shared-id"

    # Tenant B's fleet stays empty even though the agent_id collides.
    lb = c.get("/api/fleet", headers={"X-API-Key": kb}).json()["agents"]
    assert lb == []


def test_session_cookie_resolves_tenant(tmp_path):
    """Dashboard humans authenticate via session cookie (not API key)."""
    from aprntc.auth import SessionSigner
    from aprntc.tenancy import TenantStore
    from aprntc.tenancy.resolver import TenantResolver

    ts = TenantStore(tmp_path / "t.json")
    ts.create_tenant("tenant-a")
    ts.create_tenant("tenant-b")
    resolver = TenantResolver(ts, data_root=str(tmp_path / "data"))
    signer = SessionSigner("test-secret-at-least-sixteen-chars")

    # Seed an episode in tenant A.
    from aprntc.trajectory import TrajectoryStore
    ctx_a = resolver.resolve_tenant_id("tenant-a")
    store_a = TrajectoryStore(ctx_a.db_path)
    ep = Episode(task_input="hi", collector=Collector.SDK_WRAPPER,
                 final_output="hello", generation_id="G0", turns=[Turn(turn_index=0)])
    store_a.put_episode(ep, scrub=False)
    store_a.close()

    app = create_app(AppState(
        tenant_resolver=resolver,
        session_signer=signer,
    ))
    c = TestClient(app)

    # Forge a valid session for a tenant-a user.
    token = signer.issue(user_id="u1", tenant_id="tenant-a")
    c.cookies.set("aprntc_session", token)
    r = c.get("/api/trajectories")
    assert r.status_code == 200 and r.json()["count"] == 1

    # A tampered/invalid session -> 401 (no API key fallback either).
    c.cookies.set("aprntc_session", "not-a-real-token")
    assert c.get("/api/trajectories").status_code == 401
