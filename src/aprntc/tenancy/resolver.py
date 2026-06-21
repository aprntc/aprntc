"""TenantResolver — auth a request + hand back that tenant's ISOLATED data paths.

Each tenant gets its own namespace under a data root: ``{root}/{tenant_id}/...``.
The resolver authenticates the API key, then builds a :class:`TenantContext` whose
store / playbook-registry / lineage / fleet all point at the tenant's own files.
Tenant A literally cannot read tenant B's data — different paths, different DBs.

Per-tenant backends are cached so repeated requests reuse the same store handle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aprntc.tenancy.auth import Tenant, TenantStore


@dataclass
class TenantContext:
    """Everything a request needs, scoped to one authenticated tenant."""

    tenant: Tenant
    db_path: str
    playbooks_path: str
    lineage_path: str
    bundle_path: str
    fleet_root: str

    @property
    def tenant_id(self) -> str:
        return self.tenant.tenant_id


class TenantResolver:
    """Authenticates API keys and yields per-tenant, isolated storage contexts."""

    def __init__(self, tenants: TenantStore, *, data_root: str = "tenant_data") -> None:
        self._tenants = tenants
        self._root = Path(data_root)
        self._ctx_cache: dict[str, TenantContext] = {}

    def resolve(self, api_key: str | None) -> TenantContext | None:
        """Return the isolated context for a valid API key, or None if unauthorized."""
        tenant = self._tenants.authenticate(api_key)
        if tenant is None:
            return None
        return self._context_for(tenant)

    def resolve_tenant_id(self, tenant_id: str | None) -> TenantContext | None:
        """Return the context for a tenant_id without re-authenticating a key.

        Used by the dashboard session path: a human signs in via Google → the
        session cookie already proves who they are → we map their tenant_id to
        its isolated context. Returns None if the tenant is unknown or inactive.
        """
        if not tenant_id:
            return None
        try:
            tenant = self._tenants.get(tenant_id)
        except KeyError:
            return None
        if not tenant.active:
            return None
        return self._context_for(tenant)

    def _context_for(self, tenant: Tenant) -> TenantContext:
        tid = tenant.tenant_id
        if tid not in self._ctx_cache:
            base = self._root / tid
            base.mkdir(parents=True, exist_ok=True)
            self._ctx_cache[tid] = TenantContext(
                tenant=tenant,
                db_path=str(base / "aprntc.db"),
                playbooks_path=str(base / "playbooks.json"),
                lineage_path=str(base / "lineage.json"),
                bundle_path=str(base / "review_bundle.json"),
                fleet_root=str(base / "fleet"),
            )
        return self._ctx_cache[tid]
