import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useTheme } from "./lib/theme";
import { useAsync } from "./lib/hooks";
import { api, AuthMe } from "./lib/api";
import {
  IconBolt,
  IconBook,
  IconBranch,
  IconGate,
  IconMoon,
  IconSun,
  IconTrace,
  IconUsers,
} from "./components/icons";
import { Spinner } from "./components/ui";
import ReviewScreen from "./screens/Review";
import LineageScreen from "./screens/Lineage";
import TrajectoriesScreen from "./screens/Trajectories";
import LessonsScreen from "./screens/Lessons";
import TryAgentScreen from "./screens/TryAgent";
import FleetScreen from "./screens/Fleet";
import ShadowScreen from "./screens/Shadow";
import Login from "./screens/Login";

const NAV = [
  { to: "/try", label: "Try an agent", icon: IconBolt },
  { to: "/review", label: "Promotion review", icon: IconGate },
  { to: "/lineage", label: "Lineage", icon: IconBranch },
  { to: "/trajectories", label: "Trajectories", icon: IconTrace },
  { to: "/lessons", label: "Lessons", icon: IconBook },
  { to: "/fleet", label: "Fleet", icon: IconUsers },
  { to: "/shadow", label: "Shadow & canary", icon: IconBolt },
];

function Sidebar({ me }: { me: AuthMe | null }) {
  const { theme, toggle } = useTheme();
  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-surface px-3 py-5">
      <div className="mb-7 flex items-center gap-2.5 px-2">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-accent-fg font-semibold">
          a
        </div>
        <div>
          <div className="text-sm font-semibold leading-tight">aprntc</div>
          <div className="text-[11px] text-faint leading-tight">apprentice console</div>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
                isActive
                  ? "bg-surface-2 font-medium text-fg"
                  : "text-muted hover:bg-surface-2 hover:text-fg"
              }`
            }
          >
            <Icon />
            {label}
          </NavLink>
        ))}
      </nav>

      <button
        onClick={toggle}
        className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted transition hover:bg-surface-2 hover:text-fg"
      >
        {theme === "dark" ? <IconSun /> : <IconMoon />}
        {theme === "dark" ? "Light mode" : "Dark mode"}
      </button>

      {me?.authenticated && (
        <div className="mt-2 border-t border-border pt-3">
          <div className="flex items-center gap-2 px-2">
            {me.picture ? (
              <img src={me.picture} alt="" className="h-7 w-7 rounded-full" />
            ) : (
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-surface-2 text-xs">
                {(me.name || me.email || "?")[0].toUpperCase()}
              </div>
            )}
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs font-medium text-fg">{me.name || me.email}</div>
              <div className="truncate text-[10px] text-faint">{me.tenant_id}</div>
            </div>
          </div>
          <button
            onClick={async () => { await api.logout(); location.href = "/"; }}
            className="mt-2 w-full rounded-lg px-3 py-1.5 text-left text-xs text-muted transition hover:bg-surface-2 hover:text-fg"
          >
            Sign out
          </button>
        </div>
      )}
    </aside>
  );
}

export default function App() {
  const { data: me, loading } = useAsync(() => api.me(), []);

  if (loading) {
    return <div className="flex h-full items-center justify-center"><Spinner /></div>;
  }
  // auth enabled + not signed in → gate the whole app behind Login
  if (me?.auth_enabled && !me.authenticated) {
    return <Login />;
  }

  return (
    <div className="flex h-full">
      <Sidebar me={me} />
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-8 py-8">
          <Routes>
            <Route path="/" element={<Navigate to="/try" replace />} />
            <Route path="/try" element={<TryAgentScreen />} />
            <Route path="/review" element={<ReviewScreen />} />
            <Route path="/lineage" element={<LineageScreen />} />
            <Route path="/trajectories" element={<TrajectoriesScreen />} />
            <Route path="/lessons" element={<LessonsScreen />} />
            <Route path="/fleet" element={<FleetScreen />} />
            <Route path="/shadow" element={<ShadowScreen />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
