// Typed client for the aprntc FastAPI backend. All paths are relative; the Vite
// dev server proxies /api → uvicorn (see vite.config.ts).

export type GateReport = {
  n?: number;
  win_rate?: number;
  ci_low?: number;
  loss_rate?: number;
  regression_failures?: number;
  safety_failures?: number;
  win_rate_ok?: boolean;
  ci_ok?: boolean;
  loss_ok?: boolean;
  regression_ok?: boolean;
  safety_ok?: boolean;
  passed?: boolean;
};

export type PlaybookDiff = {
  add_directives?: string[];
  add_exemplars?: string[];
  add_watch_out?: string[];
  provenance?: Record<string, string[]>;
};

export type AutoDecision = {
  action: "auto_promote" | "human_review" | "reject";
  reasons: string[];
  summary: string;
  policy_enabled: boolean;
};

export type AutoPolicy = {
  enabled: boolean;
  win_rate_min: number;
  ci_low_min: number;
  loss_rate_max: number;
  max_diff_items: number;
  allow_watch_out: boolean;
  min_trust: number;
};

export type Review = {
  available: boolean;
  candidate_playbook_hash?: string | null;
  gate: GateReport;
  passed: boolean;
  diff: PlaybookDiff;
  auto?: AutoDecision | null;
};

export type Generation = {
  generation: number;
  playbook_hash: string;
  parent_generation: number | null;
  gate_summary: string;
  note: string;
};

export type Lineage = { current: Generation | null; generations: Generation[] };

export type EpisodeSummary = {
  episode_id: string;
  task_input: string;
  final_output: string | null;
  collector: string;
  generation_id: string | null;
  ts_start: string;
  partial: boolean;
  n_turns: number;
  n_steps: number;
  reward: number | null;
};

export type Lesson = Record<string, unknown> & {
  lesson_id?: string;
  content?: string;
  reward?: number;
  score?: number;
  lesson_type?: string;
};

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error((detail as any).detail || `${r.status} ${r.statusText}`);
  }
  return r.json();
}

export const api = {
  health: () => get<{ status: string }>("/api/health"),
  review: () => get<Review>("/api/review"),
  lineage: () => get<Lineage>("/api/lineage"),
  promote: (playbook_hash: string, gate_summary = "") =>
    post<{ action: string; current: Generation }>("/api/lineage/promote", { playbook_hash, gate_summary }),
  rollback: () => post<{ action: string; current: Generation }>("/api/lineage/rollback"),
  trajectories: (limit = 50, collector?: string) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (collector) qs.set("collector", collector);
    return get<{
      count: number;
      episodes: EpisodeSummary[];
      by_collector?: Record<string, number>;
    }>(`/api/trajectories?${qs.toString()}`);
  },
  trajectory: (id: string) => get<Record<string, unknown>>(`/api/trajectories/${id}`),
  lessons: (q: string, k = 10) =>
    get<{ lessons: Lesson[]; available: boolean; error?: string }>(
      `/api/lessons?q=${encodeURIComponent(q)}&k=${k}`
    ),
  agents: () => get<{ available: boolean; agents: AgentInfo[] }>("/api/agents"),
  runAgent: (agent_id: string, task: string) =>
    post<AgentRunResult>("/api/agents/run", { agent_id, task }),
  feedback: (episode_id: string, vote: "up" | "down") =>
    post<{ ok: boolean; fused_reward: number | null }>("/api/feedback", { episode_id, vote }),
  autoPolicy: () => get<AutoPolicy>("/api/policy/auto-promote"),
  setAutoPolicy: (patch: Partial<AutoPolicy>) =>
    post<AutoPolicy>("/api/policy/auto-promote", patch),
  fleet: (domain?: string) => {
    const qs = domain ? `?domain=${encodeURIComponent(domain)}` : "";
    return get<{ agents: FleetAgent[] }>(`/api/fleet${qs}`);
  },
  registerAgent: (agent: { agent_id: string; domain?: string; name?: string; tags?: string[] }) =>
    post<FleetAgent>("/api/fleet/register", agent),
  shadow: () => get<ShadowStats>("/api/online/shadow"),
  canary: () => get<CanaryState>("/api/online/canary"),
  authConfig: () => get<{ enabled: boolean; provider: string | null }>("/api/auth/config"),
  me: () => get<AuthMe>("/api/auth/me"),
  logout: () => post<{ ok: boolean }>("/api/auth/logout"),
};

export type ShadowStats = {
  available: boolean;
  n?: number;
  wins?: number;
  losses?: number;
  ties?: number;
  errors?: number;
  win_rate?: number;
  loss_rate?: number;
  ci_low?: number;
  ci_high?: number;
  ready_to_promote?: boolean;
};

export type CanaryArm = { n: number; mean_reward: number };

export type CanaryState = {
  available: boolean;
  status?: "running" | "promoted" | "rolled_back";
  fraction?: number;
  stages?: number[];
  child?: CanaryArm;
  parent?: CanaryArm;
};

export type FleetAgent = {
  agent_id: string;
  domain: string;
  name: string;
  tags: string[];
  current_generation: number | null;
  playbook_hash: string | null;
};

export type AuthMe = {
  authenticated: boolean;
  auth_enabled: boolean;
  user_id?: string;
  tenant_id?: string;
  email?: string | null;
  name?: string | null;
  picture?: string | null;
};

export type AgentInfo = {
  id: string;
  name: string;
  description: string;
  examples: string[];
};

export type AgentStep = {
  type: string;
  tool_name: string | null;
  tool_args: unknown;
  duration_ms: number | null;
};

export type AgentRunResult = {
  answer: string;
  episode_id: string;
  agent_id: string;
  reward: number | null;
  reward_rationale: string | null;
  cited: string[];
  reasoning: string | null;
  steps: AgentStep[];
};
