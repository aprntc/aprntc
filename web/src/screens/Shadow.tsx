import { useEffect, useState } from "react";
import { api, CanaryState, ShadowStats } from "../lib/api";
import { pct } from "../lib/hooks";
import { Badge, Card, Empty, Metric, PageHeader } from "../components/ui";
import { IconBolt } from "../components/icons";

const REFRESH_MS = 5_000;

export default function Shadow() {
  const [shadow, setShadow] = useState<ShadowStats | null>(null);
  const [canary, setCanary] = useState<CanaryState | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Light polling — the live runtime accumulates samples continuously; the UI
  // shows the running tally without needing to be opened twice.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const [s, c] = await Promise.all([api.shadow(), api.canary()]);
        if (alive) { setShadow(s); setCanary(c); setErr(null); }
      } catch (e: any) {
        if (alive) setErr(e?.message ?? "failed");
      }
    };
    tick();
    const id = setInterval(tick, REFRESH_MS);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (err && !shadow && !canary)
    return <Empty icon={<IconBolt className="h-8 w-8" />} title="Couldn't reach the API" sub={err} />;

  const anyWired = shadow?.available || canary?.available;
  if (shadow && canary && !anyWired) {
    return (
      <div>
        <PageHeader
          title="Shadow & canary"
          sub="Online evaluation against live traffic — runs alongside the parent."
        />
        <Empty
          icon={<IconBolt className="h-8 w-8" />}
          title="Not wired in this environment"
          sub="ShadowRunner + CanaryController are deploy-time singletons. Set
            `AppState.shadow` and `AppState.canary` in your production wiring; this
            screen will then surface live win-rate, CI, and canary stage."
        />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="Shadow & canary"
        sub="Online evaluation against live traffic — auto-refreshes every 5s."
      />
      {shadow?.available && <ShadowPanel s={shadow} />}
      {canary?.available && <CanaryPanel c={canary} />}
    </div>
  );
}

function ShadowPanel({ s }: { s: ShadowStats }) {
  return (
    <section className="mb-6">
      <h2 className="mb-2 text-sm font-medium text-muted">
        Shadow runner
        {s.ready_to_promote && <Badge tone="success" >ready to promote</Badge>}
      </h2>
      <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Metric label="N" value={`${s.n ?? 0}`} hint="live samples" />
        <Metric
          label="Win-rate"
          value={pct(s.win_rate ?? 0)}
          hint={`vs parent · ${s.wins ?? 0} wins / ${s.losses ?? 0} losses`}
          tone={(s.win_rate ?? 0) >= 0.55 ? "success" : "default"}
        />
        <Metric
          label="95% CI"
          value={`${pct(s.ci_low ?? 0)} – ${pct(s.ci_high ?? 0)}`}
          hint="Wilson"
        />
        <Metric
          label="Loss-rate"
          value={pct(s.loss_rate ?? 0)}
          hint={`${s.errors ?? 0} errors`}
          tone={(s.loss_rate ?? 0) < 0.10 ? "success" : "danger"}
        />
      </div>
      {s.trust && <ShadowTrustLine trust={s.trust} />}
    </section>
  );
}

function ShadowTrustLine({ trust }: { trust: NonNullable<ShadowStats["trust"]> }) {
  // A3 → shadow gate: the judge that drives live win-rate must itself have
  // earned reliability against the anchor before ready_to_promote can clear.
  if (trust.value == null) {
    const threshold = trust.min_n ? ` (need ${trust.min_n})` : "";
    return (
      <div className="text-[11px] text-faint">
        Judge trust: insufficient data — {trust.n} joint judge+anchor episodes{threshold}.
        Promotion is blocked until trust is established.
      </div>
    );
  }
  const tone = trust.value >= 0.8 ? "text-success" : trust.value >= 0.6 ? "text-warning" : "text-danger";
  return (
    <div className="text-[11px]">
      <span className="text-faint">Judge trust: </span>
      <span className={tone}>{pct(trust.value)}</span>
      <span className="text-faint"> (n={trust.n} joint judge+anchor episodes)</span>
    </div>
  );
}

function CanaryPanel({ c }: { c: CanaryState }) {
  const stages = c.stages ?? [0.05, 0.25, 0.5, 1.0];
  const currentStageIdx = stages.findIndex((s) => s >= (c.fraction ?? 0));
  const tone = c.status === "rolled_back" ? "danger" : c.status === "promoted" ? "success" : "accent";

  return (
    <section>
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-medium text-muted">Canary rollout</h2>
        <Badge tone={tone as any}>{c.status}</Badge>
        <span className="text-xs text-faint">
          {pct(c.fraction ?? 0)} traffic to child
        </span>
      </div>

      {/* Stage strip: each segment is one configured stage; current is highlighted. */}
      <Card className="mb-3 p-4">
        <div className="mb-3 flex gap-1.5">
          {stages.map((s, i) => (
            <div
              key={s}
              className={`h-2 flex-1 rounded-full ${
                i <= currentStageIdx
                  ? c.status === "rolled_back"
                    ? "bg-danger/40"
                    : "bg-accent"
                  : "bg-surface-2"
              }`}
              title={`${pct(s)}`}
            />
          ))}
        </div>
        <div className="flex justify-between text-[11px] text-faint">
          {stages.map((s) => <span key={s}>{pct(s)}</span>)}
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <ArmCard label="Child" arm={c.child} />
        <ArmCard label="Parent (baseline)" arm={c.parent} />
      </div>
    </section>
  );
}

function ArmCard({ label, arm }: { label: string; arm?: { n: number; mean_reward: number } }) {
  return (
    <Card className="p-4">
      <div className="mb-1 text-xs text-muted">{label}</div>
      <div className="text-2xl font-semibold tabular-nums text-fg">
        {arm ? pct(arm.mean_reward) : "—"}
      </div>
      <div className="mt-0.5 text-[11px] text-faint">mean reward · n={arm?.n ?? 0}</div>
    </Card>
  );
}
