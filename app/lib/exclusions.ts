// 展示层防御性过滤：按排除名单过滤看板数据并按扫描器口径重算聚合值。
export type Dashboard = {
  meta: { generated_at: string; timezone: string; acceptance_note: string };
  kpis: Record<string, number>;
  daily: Array<{ date: string; sessions: number; issues: number; tokens: number; wall_seconds: number; accepted: number }>;
  projects: Array<{ name: string; sessions: number; issues: number; bugs: number; resolved: number; accepted: number; tokens: number; wall_seconds: number; last_activity: string }>;
  issues: Array<Record<string, any>>;
  sessions: Array<Record<string, any>>;
};

const dayFormatterCache = new Map<string, Intl.DateTimeFormat>();

function dayInTz(value: unknown, timeZone: string): string | null {
  if (typeof value !== "string" || !value) return null;
  const parsed = new Date(value);
  if (!Number.isNaN(parsed.getTime())) {
    let formatter = dayFormatterCache.get(timeZone);
    if (!formatter) {
      formatter = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" });
      dayFormatterCache.set(timeZone, formatter);
    }
    return formatter.format(parsed);
  }
  return value.slice(0, 10);
}

export function filterDashboard(data: Dashboard, excluded: string[], now: Date = new Date()): Dashboard {
  if (!excluded.length) return data;
  const blocked = new Set(excluded);
  const issues = data.issues.filter((issue) => !blocked.has(issue.project));
  const sessions = data.sessions.filter((session) => !blocked.has(session.project));
  const projects = data.projects.filter((project) => !blocked.has(project.name));

  const timeZone = data.meta.timezone || "UTC";
  const byDay = new Map<string, { sessionIds: Set<string>; issues: number; tokens: number; wallSeconds: number; accepted: number }>();
  for (const issue of issues) {
    if (typeof issue.local_date !== "string" || !issue.local_date) continue;
    const day = byDay.get(issue.local_date) ?? { sessionIds: new Set<string>(), issues: 0, tokens: 0, wallSeconds: 0, accepted: 0 };
    day.sessionIds.add(issue.session_id);
    day.issues += 1;
    day.tokens += Number(issue.total_tokens) || 0;
    day.wallSeconds += Number(issue.wall_seconds) || 0;
    if (issue.acceptance === "accepted") day.accepted += 1;
    byDay.set(issue.local_date, day);
  }
  const daily = [...byDay.keys()].sort().map((date) => {
    const day = byDay.get(date)!;
    return { date, sessions: day.sessionIds.size, issues: day.issues, tokens: day.tokens, wall_seconds: Math.round(day.wallSeconds * 10) / 10, accepted: day.accepted };
  });

  const today = dayInTz(now.toISOString(), timeZone);
  const acceptedCount = issues.filter((issue) => issue.acceptance === "accepted").length;
  const decided = issues.filter((issue) => issue.acceptance === "accepted" || issue.acceptance === "rejected").length;
  return {
    ...data,
    kpis: {
      sessions: sessions.length,
      today_sessions: new Set(issues.filter((issue) => issue.local_date === today).map((issue) => issue.session_id)).size,
      issues: issues.length,
      open_issues: issues.filter((issue) => ["open", "proposed", "needs_followup", "blocked"].includes(issue.status)).length,
      bugs: issues.filter((issue) => issue.type === "bug").length,
      total_tokens: issues.reduce((sum, issue) => sum + (Number(issue.total_tokens) || 0), 0),
      wall_seconds: Math.round(issues.reduce((sum, issue) => sum + (Number(issue.wall_seconds) || 0), 0) * 10) / 10,
      accepted: acceptedCount,
      acceptance_rate: decided ? Math.round((acceptedCount / decided) * 1000) / 10 : 0,
      cross_day_sessions: sessions.filter((session) => dayInTz(session.first_event_at, timeZone) !== dayInTz(session.last_event_at, timeZone)).length,
    },
    daily,
    projects,
    issues,
    sessions,
  };
}
