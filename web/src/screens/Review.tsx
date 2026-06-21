import { useState } from "react";
import { api, AutoDecision } from "../lib/api";
import { useAsync, pct } from "../lib/hooks";
import { Badge, Button, Card, Empty, Metric, PageHeader, Spinner } from "../components/ui";
import {
  IconCheck,
  IconGate,
  IconPlus,
  IconRocket,
  IconShield,
  IconUndo,
  IconX,
} from "../components/icons";

export default function Review() {
  const { data, loading, error, refetch } = useAsync(() => api.review(), []);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  if (loading) return <Spinner />;
  if (error)
    return <Empty icon={<IconGate className="h-8 w-8" />} title="Couldn't reach the API" sub={error} />;
  if (!data?.available)
    return (
      <Empty
        icon={<IconGate className="h-8 w-8" />}
        title="No candidate to review"
        sub="Run an evaluation (scripts/demo_promotion.py) to produce a review bundle."
      />
    );

  const g = data.gate;
  const passed = data.passed;

  const promote = async () => {
    if (!data.candidate_playbook_hash) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.promote(data.candidate_playbook_hash, `win=${pct(g.win_rate)}`);
      setMsg(`Promoted to G${r.current.generation}. The child is now the live parent.`);
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setBusy(false);
      refetch();
    }
  };
  const rollback = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.rollback();
      setMsg(`Rolled back to G${r.current.generation}.`);
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="Promotion review"
        sub="Human-in-the-loop gate — review the candidate child, then promote or roll back."
        right={
          passed ? (
            <Badge tone="success">
              <IconCheck className="h-3.5 w-3.5" /> Passes acceptance bar
            </Badge>
          ) : (
            <Badge tone="danger">
              <IconX className="h-3.5 w-3.5" /> Rejected by gate
            </Badge>
          )
        }
      />

      {data.candidate_playbook_hash && (
        <div className="mb-5 text-sm text-muted">
          candidate{" "}
          <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-fg">
            {data.candidate_playbook_hash}
          </span>
        </div>
      )}

      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Metric label="Win-rate" value={pct(g.win_rate)} hint="need ≥ 55%" tone={g.win_rate_ok ? "success" : "danger"} />
        <Metric label="95% CI low" value={pct(g.ci_low)} hint="need > 50%" tone={g.ci_ok ? "success" : "danger"} />
        <Metric label="Loss-rate" value={pct(g.loss_rate)} hint="need < 10%" tone={g.loss_ok ? "success" : "danger"} />
        <Metric label="Eval set" value={`N=${g.n ?? "—"}`} hint="held-out" />
      </div>

      <div className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <HardGate ok={!!g.regression_ok} label="regression suite" fails={g.regression_failures} />
        <HardGate ok={!!g.safety_ok} label="safety probes" fails={g.safety_failures} />
      </div>

      <h2 className="mb-2 text-sm font-medium text-muted">Candidate playbook diff</h2>
      <Card className="mb-6 divide-y divide-border">
        <DiffList items={data.diff.add_directives} label="directive" tone="success" provenance={data.diff.provenance} />
        <DiffList items={data.diff.add_exemplars} label="exemplar" tone="accent" provenance={data.diff.provenance} />
        <DiffList items={data.diff.add_watch_out} label="watch-out" tone="warning" provenance={data.diff.provenance} />
        {!hasDiff(data.diff) && <div className="p-4 text-sm text-faint">No changes proposed.</div>}
      </Card>

      {/* A4: auto-promotion verdict + opt-in toggle. Always hidden when the gate
          already rejects (`auto` is null/REJECT — the gate banner above covers that). */}
      {data.auto && data.auto.action !== "reject" && (
        <AutoPromotion auto={data.auto} onPolicyChange={refetch} />
      )}

      {msg && (
        <div className="mb-4 rounded-lg border border-border bg-surface-2 px-4 py-3 text-sm text-fg">{msg}</div>
      )}

      <div className="flex gap-3">
        <Button variant="primary" onClick={promote} disabled={!passed || busy} className="flex-1">
          <IconRocket /> Promote candidate
        </Button>
        <Button onClick={rollback} disabled={busy} className="flex-1">
          <IconUndo /> Roll back
        </Button>
      </div>
      <p className="mt-3 text-center text-[11px] text-faint">
        Promote is locked until the acceptance bar passes · human approval required
      </p>
    </div>
  );
}

function HardGate({ ok, label, fails }: { ok: boolean; label: string; fails?: number }) {
  return (
    <div
      className={`flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm ${
        ok ? "bg-success/10 text-success" : "bg-danger/10 text-danger"
      }`}
    >
      <IconShield className="h-4 w-4" />
      {ok ? `0 ${label} failures` : `${fails ?? "?"} ${label} failure(s) — blocks promotion`}
    </div>
  );
}

function DiffList({
  items,
  label,
  tone,
  provenance,
}: {
  items?: string[];
  label: string;
  tone: "success" | "accent" | "warning";
  provenance?: Record<string, string[]>;
}) {
  if (!items?.length) return null;
  return (
    <>
      {items.map((text) => (
        <div key={text} className="flex items-start gap-3 p-3">
          <IconPlus className="mt-0.5 h-4 w-4 shrink-0 text-success" />
          <div className="flex-1 text-sm text-fg">
            {text}
            {provenance?.[text]?.length ? (
              <span className="ml-2 font-mono text-[11px] text-faint">{provenance[text].join(", ")}</span>
            ) : null}
          </div>
          <Badge tone={tone}>{label}</Badge>
        </div>
      ))}
    </>
  );
}

const hasDiff = (d: { add_directives?: string[]; add_exemplars?: string[]; add_watch_out?: string[] }) =>
  !!(d.add_directives?.length || d.add_exemplars?.length || d.add_watch_out?.length);


function TrustLine({ trust }: { trust: NonNullable<AutoDecision["trust"]> }) {
  // A3 → A4 signal: judge ↔ anchor agreement from the store's label history.
  // Until min_n is met, the policy treats trust as missing (HUMAN_REVIEW stays on).
  if (trust.value == null) {
    const threshold = trust.min_n ? ` (need ${trust.min_n})` : "";
    return (
      <div className="mt-2 text-[11px] text-faint">
        Judge trust: insufficient data — {trust.n} joint judge+anchor episodes{threshold}.
      </div>
    );
  }
  const tone = trust.value >= 0.8 ? "text-success" : trust.value >= 0.6 ? "text-warning" : "text-danger";
  return (
    <div className="mt-2 text-[11px]">
      <span className="text-faint">Judge trust: </span>
      <span className={tone}>{pct(trust.value)}</span>
      <span className="text-faint"> (n={trust.n} joint judge+anchor episodes)</span>
    </div>
  );
}

function AutoPromotion({ auto, onPolicyChange }: { auto: AutoDecision; onPolicyChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const isAuto = auto.action === "auto_promote";
  const togglePolicy = async () => {
    setBusy(true);
    try {
      await api.setAutoPolicy({ enabled: !auto.policy_enabled });
      onPolicyChange();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card
      className={`mb-6 border-l-4 p-4 ${
        isAuto ? "border-l-success bg-success/5" : "border-l-warning bg-warning/5"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 text-sm font-medium text-fg">
            {isAuto ? (
              <>
                <IconRocket className="h-4 w-4 text-success" />
                <span>Eligible for auto-promotion</span>
                {!auto.policy_enabled && (
                  <Badge tone="warning">policy off — won't fire</Badge>
                )}
              </>
            ) : (
              <>
                <IconShield className="h-4 w-4 text-warning" />
                <span>Needs human review</span>
              </>
            )}
          </div>
          {auto.reasons.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-muted">
              {auto.reasons.map((r) => (
                <li key={r}>· {r}</li>
              ))}
            </ul>
          )}
          {isAuto && auto.policy_enabled && (
            <div className="mt-2 text-xs text-success">
              All guardrails clear — auto-promotion would fire on this candidate.
            </div>
          )}
          {auto.trust && <TrustLine trust={auto.trust} />}
        </div>
        <label className="flex shrink-0 items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            disabled={busy}
            checked={auto.policy_enabled}
            onChange={togglePolicy}
            className="h-4 w-4"
          />
          <span>Enable auto-promotion</span>
        </label>
      </div>
    </Card>
  );
}
