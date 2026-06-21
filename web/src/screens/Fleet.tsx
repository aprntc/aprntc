import { useState } from "react";
import { api, FleetAgent } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { Badge, Button, Card, Empty, PageHeader, Spinner } from "../components/ui";
import { IconPlus, IconUsers } from "../components/icons";

export default function Fleet() {
  const { data, loading, error, refetch } = useAsync(() => api.fleet(), []);
  const [showForm, setShowForm] = useState(false);

  if (loading) return <Spinner />;
  if (error)
    return <Empty icon={<IconUsers className="h-8 w-8" />} title="Couldn't reach the API" sub={error} />;

  const agents = data?.agents ?? [];

  return (
    <div>
      <PageHeader
        title="Fleet"
        sub="Many parent agents under one apprentice — each with its own lineage."
        right={
          <Button variant="primary" onClick={() => setShowForm((s) => !s)}>
            <IconPlus /> Register agent
          </Button>
        }
      />

      {showForm && <RegisterAgentForm onDone={() => { setShowForm(false); refetch(); }} />}

      {agents.length === 0 ? (
        <Empty
          icon={<IconUsers className="h-8 w-8" />}
          title="No agents registered"
          sub="Click 'Register agent' to add the first one — each gets its own lineage."
        />
      ) : (
        <div className="space-y-2">
          {agents.map((a) => <AgentCard key={a.agent_id} a={a} />)}
        </div>
      )}
    </div>
  );
}

function AgentCard({ a }: { a: FleetAgent }) {
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium text-fg">{a.name || a.agent_id}</span>
            <Badge tone="accent">{a.domain}</Badge>
            {a.tags.map((t) => <Badge key={t}>{t}</Badge>)}
          </div>
          <div className="mt-1 font-mono text-[11px] text-faint">{a.agent_id}</div>
        </div>
        <div className="text-right">
          {a.current_generation == null ? (
            <span className="text-xs text-faint">no parent yet</span>
          ) : (
            <>
              <div className="text-sm font-semibold text-fg">G{a.current_generation}</div>
              {a.playbook_hash && (
                <div className="mt-0.5 font-mono text-[10px] text-faint">
                  {a.playbook_hash.slice(0, 14)}…
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </Card>
  );
}

function RegisterAgentForm({ onDone }: { onDone: () => void }) {
  const [agentId, setAgentId] = useState("");
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("generic");
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    if (!agentId.trim()) {
      setErr("agent_id is required");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.registerAgent({
        agent_id: agentId.trim(),
        name: name.trim() || undefined,
        domain: domain.trim() || "generic",
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      onDone();
    } catch (e: any) {
      setErr(e?.message ?? "failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="mb-4 p-4">
      <h3 className="mb-3 text-sm font-medium text-fg">Register a new agent</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="agent_id (required)" value={agentId} onChange={setAgentId} placeholder="byteplus-support" />
        <Field label="display name" value={name} onChange={setName} placeholder="BytePlus support" />
        <Field label="domain" value={domain} onChange={setDomain} placeholder="generic / support / rag …" />
        <Field label="tags (comma-separated)" value={tags} onChange={setTags} placeholder="prod, kb" />
      </div>
      {err && <div className="mt-3 text-xs text-danger">{err}</div>}
      <div className="mt-3 flex gap-2">
        <Button variant="primary" onClick={submit} disabled={busy}>
          {busy ? "Registering…" : "Register"}
        </Button>
        <Button onClick={onDone} disabled={busy}>Cancel</Button>
      </div>
    </Card>
  );
}

function Field({
  label, value, onChange, placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block text-xs">
      <span className="mb-1 block text-muted">{label}</span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-md border border-border bg-surface-2 px-3 py-2 text-sm text-fg focus:border-accent focus:outline-none"
      />
    </label>
  );
}
