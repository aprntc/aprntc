import { useState } from "react";
import { api, EpisodeSummary } from "../lib/api";
import { useAsync, pct } from "../lib/hooks";
import { Badge, Card, Empty, PageHeader, Spinner } from "../components/ui";
import { IconThumbDown, IconThumbUp, IconTrace } from "../components/icons";

// A1: collector → tone (visual cue for which collector recorded each episode).
const COLLECTOR_TONE: Record<string, "accent" | "success" | "warning" | "default"> = {
  sdk_wrapper: "accent",
  egress_proxy: "success",
  mcp: "warning",
  otel: "default",
  a2a: "default",
};

export default function Trajectories() {
  const [collector, setCollector] = useState<string | null>(null);
  const { data, loading, error } = useAsync(
    () => api.trajectories(100, collector ?? undefined),
    [collector],
  );
  const [open, setOpen] = useState<string | null>(null);

  if (loading && !data)
    return <Spinner />;
  if (error)
    return <Empty icon={<IconTrace className="h-8 w-8" />} title="Couldn't reach the API" sub={error} />;

  const byCollector = data?.by_collector ?? {};
  const total = data?.count ?? 0;
  const collectorEntries = Object.entries(byCollector).sort((a, b) => b[1] - a[1]);

  return (
    <div>
      <PageHeader title="Trajectories" sub={`${total} captured episodes`} />

      {/* A1: collector filter chips — one per collector that has captured at least one episode. */}
      {collectorEntries.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          <FilterChip
            label="All"
            count={total}
            active={collector === null}
            onClick={() => setCollector(null)}
            tone="default"
          />
          {collectorEntries.map(([name, n]) => (
            <FilterChip
              key={name}
              label={name}
              count={n}
              active={collector === name}
              onClick={() => setCollector(name)}
              tone={COLLECTOR_TONE[name] ?? "default"}
            />
          ))}
        </div>
      )}

      {!data?.episodes.length ? (
        <Empty
          icon={<IconTrace className="h-8 w-8" />}
          title={collector ? `No episodes for ${collector}` : "No trajectories captured"}
          sub={collector
            ? "Try a different collector — or run an agent that emits via this one."
            : "Run an agent (scripts/demo_agents.py) to populate the trajectory store."}
        />
      ) : (
        <div className="space-y-2">
          {data.episodes.map((e) => (
            <EpisodeRow key={e.episode_id} e={e} open={open === e.episode_id} onToggle={() => setOpen(open === e.episode_id ? null : e.episode_id)} />
          ))}
        </div>
      )}
    </div>
  );
}

function FilterChip({
  label, count, active, onClick, tone,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  tone: "accent" | "success" | "warning" | "default";
}) {
  const activeCls: Record<typeof tone, string> = {
    accent: "border-accent bg-accent/10 text-accent",
    success: "border-success bg-success/10 text-success",
    warning: "border-warning bg-warning/10 text-warning",
    default: "border-fg/40 bg-surface-2 text-fg",
  };
  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition ${
        active ? activeCls[tone] : "border-border text-muted hover:bg-surface-2"
      }`}
    >
      <span>{label}</span>
      <span className="rounded-full bg-fg/10 px-1.5 py-0.5 text-[10px] font-medium">{count}</span>
    </button>
  );
}

function rewardTone(r: number | null): "success" | "warning" | "danger" | "default" {
  if (r == null) return "default";
  if (r >= 0.7) return "success";
  if (r >= 0.4) return "warning";
  return "danger";
}

function EpisodeRow({ e, open, onToggle }: { e: EpisodeSummary; open: boolean; onToggle: () => void }) {
  const detail = useAsync(() => api.trajectory(e.episode_id), [open ? e.episode_id : ""]);
  return (
    <Card>
      <button onClick={onToggle} className="flex w-full items-center gap-3 px-4 py-3 text-left">
        <div className="flex-1 truncate">
          <div className="truncate text-sm font-medium text-fg">{e.task_input}</div>
          <div className="mt-0.5 flex items-center gap-2 text-[11px] text-faint">
            <span className="font-mono">{e.episode_id.slice(0, 14)}…</span>
            <Badge tone={COLLECTOR_TONE[e.collector] ?? "default"}>{e.collector}</Badge>
            <span>· {e.n_turns} turns · {e.n_steps} steps</span>
            {e.partial && <Badge tone="warning">partial</Badge>}
          </div>
        </div>
        {e.reward != null && <Badge tone={rewardTone(e.reward)}>reward {pct(e.reward)}</Badge>}
      </button>
      {open && (
        <div className="border-t border-border px-4 py-3 text-sm">
          {detail.loading ? (
            <span className="text-muted">Loading…</span>
          ) : detail.data ? (
            <TrajectoryDetail data={detail.data as any} episodeId={e.episode_id} onVoted={detail.refetch} />
          ) : (
            <span className="text-danger">Failed to load</span>
          )}
        </div>
      )}
    </Card>
  );
}

function TrajectoryDetail({ data, episodeId, onVoted }: { data: any; episodeId: string; onVoted: () => void }) {
  return (
    <div className="space-y-3">
      {data.final_output && (
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs text-muted">Final answer</span>
            <FeedbackButtons episodeId={episodeId} labels={data.labels ?? []} onVoted={onVoted} />
          </div>
          <div className="rounded-lg bg-surface-2 px-3 py-2 text-sm">{data.final_output}</div>
        </div>
      )}
      {(data.turns ?? []).map((t: any) => (
        <div key={t.turn_index}>
          {(t.steps ?? []).length > 0 && (
            <div className="space-y-1">
              {t.steps.map((s: any, i: number) => (
                <div key={i} className="flex items-center gap-2 text-xs">
                  <Badge>{s.type}</Badge>
                  {s.tool_name && <span className="font-mono text-muted">{s.tool_name}</span>}
                  {s.source_fidelity && s.source_fidelity !== "full" && (
                    <span className="text-faint">({s.source_fidelity})</span>
                  )}
                  {s.error && <span className="text-danger">{s.error}</span>}
                </div>
              ))}
            </div>
          )}
          {t.reasoning_content && (
            <div className="mt-2 rounded-lg border border-border px-3 py-2 text-xs text-muted">
              <span className="text-faint">reasoning · </span>
              {t.reasoning_content}
            </div>
          )}
        </div>
      ))}
      {data.labels?.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {data.labels.map((l: any, i: number) => (
            <Badge key={i}>
              {l.source}: {pct(l.score)}
            </Badge>
          ))}
          {data.fused_reward && <Badge tone="accent">fused {pct(data.fused_reward.reward)}</Badge>}
        </div>
      )}
    </div>
  );
}

function FeedbackButtons({
  episodeId,
  labels,
  onVoted,
}: {
  episodeId: string;
  labels: any[];
  onVoted: () => void;
}) {
  // reflect an existing explicit vote (so reopening shows the prior thumbs)
  const prior = labels.find((l) => l.source === "user_explicit");
  const initial = prior ? (prior.score >= 0.5 ? "up" : "down") : null;
  const [vote, setVote] = useState<"up" | "down" | null>(initial);
  const [sending, setSending] = useState(false);

  const send = async (v: "up" | "down") => {
    if (sending || vote === v) return;
    setSending(true);
    try {
      await api.feedback(episodeId, v);
      setVote(v);
      onVoted(); // refetch so the new label + fused reward show
    } catch {
      /* best-effort */
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="flex items-center gap-1.5">
      <button
        onClick={() => send("up")}
        disabled={sending}
        aria-label="thumbs up"
        className={`rounded-md border p-1 transition disabled:opacity-50 ${
          vote === "up"
            ? "border-success bg-success/10 text-success"
            : "border-border text-muted hover:bg-surface-2 hover:text-fg"
        }`}
      >
        <IconThumbUp className="h-3.5 w-3.5" />
      </button>
      <button
        onClick={() => send("down")}
        disabled={sending}
        aria-label="thumbs down"
        className={`rounded-md border p-1 transition disabled:opacity-50 ${
          vote === "down"
            ? "border-danger bg-danger/10 text-danger"
            : "border-border text-muted hover:bg-surface-2 hover:text-fg"
        }`}
      >
        <IconThumbDown className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
