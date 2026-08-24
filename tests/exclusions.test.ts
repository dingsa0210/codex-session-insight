import assert from "node:assert/strict";
import test from "node:test";
import { filterDashboard, type Dashboard } from "../app/lib/exclusions.ts";

const base: Dashboard = {
  meta: { generated_at: "2026-08-24T04:00:00Z", timezone: "Asia/Shanghai", acceptance_note: "" },
  kpis: {},
  daily: [],
  projects: [
    { name: "demo", sessions: 1, issues: 2, bugs: 1, resolved: 1, accepted: 1, tokens: 300, wall_seconds: 90, last_activity: "2026-08-12T02:00:03Z" },
    { name: "noise", sessions: 1, issues: 1, bugs: 0, resolved: 0, accepted: 0, tokens: 100, wall_seconds: 30, last_activity: "2026-08-11T16:00:02Z" },
  ],
  issues: [
    { id: "i1", project: "demo", session_id: "s1", local_date: "2026-08-11", type: "bug", status: "resolved", acceptance: "accepted", total_tokens: 200, wall_seconds: 60 },
    { id: "i2", project: "demo", session_id: "s1", local_date: "2026-08-12", type: "task", status: "open", acceptance: "pending", total_tokens: 100, wall_seconds: 30 },
    { id: "i3", project: "noise", session_id: "s2", local_date: "2026-08-11", type: "task", status: "open", acceptance: "rejected", total_tokens: 100, wall_seconds: 30 },
  ],
  sessions: [
    { id: "s1", project: "demo", first_event_at: "2026-08-11T15:59:50Z", last_event_at: "2026-08-12T02:00:03Z" },
    { id: "s2", project: "noise", first_event_at: "2026-08-11T15:00:00Z", last_event_at: "2026-08-11T16:00:00Z" },
  ],
};

const fixedNow = new Date("2026-08-12T00:00:00Z"); // 上海本地日 2026-08-12

test("empty exclusion list returns the original object", () => {
  assert.equal(filterDashboard(base, [], fixedNow), base);
});

test("excluding a project filters all sections and recomputes aggregates", () => {
  const view = filterDashboard(base, ["noise"], fixedNow);
  assert.deepEqual(view.projects.map((p) => p.name), ["demo"]);
  assert.deepEqual(view.issues.map((i) => i.id), ["i1", "i2"]);
  assert.deepEqual(view.sessions.map((s) => s.id), ["s1"]);
  assert.deepEqual(view.kpis, {
    sessions: 1,
    today_sessions: 1,
    issues: 2,
    open_issues: 1,
    bugs: 1,
    total_tokens: 300,
    wall_seconds: 90,
    accepted: 1,
    acceptance_rate: 100,
    cross_day_sessions: 1,
  });
  assert.deepEqual(view.daily, [
    { date: "2026-08-11", sessions: 1, issues: 1, tokens: 200, wall_seconds: 60, accepted: 1 },
    { date: "2026-08-12", sessions: 1, issues: 1, tokens: 100, wall_seconds: 30, accepted: 0 },
  ]);
});
