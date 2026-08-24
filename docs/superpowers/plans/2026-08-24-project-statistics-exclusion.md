# 项目统计排除配置 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后台「设置」页可配置不参与统计的项目：名单落盘 `data/excluded_projects.json`，扫描器聚合层真排除，前端展示层防御性过滤。

**Architecture:** JSON 名单文件是唯一真源。Vite dev 中间件（Node 侧，绕开 workerd 不可写文件系统的限制）提供 GET/POST `/api/exclusions`；Python 扫描器每轮扫描读取名单并在物化 dashboard.json 时过滤；前端加载快照时应用同一名单并重算 KPI/daily。

**Tech Stack:** Python 3 (sqlite3/stdlib)、React 19 + vinext (Next 兼容层 on Vite/workerd)、Vite 插件中间件、node:test。

**设计文档:** `docs/superpowers/specs/2026-08-24-project-statistics-exclusion-design.md`

## Global Constraints

- 名单文件路径固定为 `<项目根>/data/excluded_projects.json`，格式 `{"version":1,"updated_at":"...","excluded":[...]}`。
- 按项目名精确匹配（`sessions.project` / `issues.project` 字段值）。
- 任何消费方读名单失败一律按空名单处理并继续运行（扫描器打 `[warn]`）。
- SQLite 证据库不做任何删除；排除只影响统计输出。
- UI 文案为中文；代码风格跟随各文件现状（Python 带类型标注、TSX 紧凑单行 JSX、CSS 单行紧凑规则）。
- Node >= 22.13.0（engines）；本机 Node v24 原生支持运行 TS 测试（type stripping 默认开启；若报 "Unknown file extension .ts" 则加 `NODE_OPTIONS=--experimental-strip-types` 重试）。
- Python 用 `python3` / `python -m unittest tests.test_analyzer`（README 口径）；在项目根目录执行所有命令。

---

### Task 1: 扫描器——排除名单加载

**Files:**
- Modify: `scripts/analyze_sessions.py:170-182` (`SessionAnalyzer.__init__`)
- Test: `tests/test_analyzer.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `SessionAnalyzer(db_path, output_dir, tz_name="Asia/Shanghai", excluded_file: Path | None = None)`；实例属性 `self.excluded_file: Path`、`self.excluded_projects: set[str]`；方法 `_load_excluded() -> set[str]`（Task 2 的 materialize 过滤依赖 `self.excluded_projects`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_analyzer.py` 的 `AnalyzerTests` 类中追加方法：

```python
    def test_load_excluded_tolerates_missing_invalid_and_trims(self):
        analyzer = SessionAnalyzer(self.db, self.output, excluded_file=self.root / "missing.json")
        try:
            self.assertEqual(analyzer._load_excluded(), set())

            config = self.root / "config.json"
            config.write_text(
                '{"version":1,"updated_at":"t","excluded":["demo"," demo ",""]}',
                encoding="utf-8",
            )
            analyzer.excluded_file = config
            self.assertEqual(analyzer._load_excluded(), {"demo"})

            config.write_text("{broken", encoding="utf-8")
            self.assertEqual(analyzer._load_excluded(), set())
        finally:
            analyzer.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_analyzer.AnalyzerTests.test_load_excluded_tolerates_missing_invalid_and_trims -v`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'excluded_file'`

- [ ] **Step 3: 最小实现**

`scripts/analyze_sessions.py` 修改 `__init__`（原 170-182 行），加入参数与属性初始化：

```python
    def __init__(self, db_path: Path, output_dir: Path, tz_name: str = "Asia/Shanghai",
                 excluded_file: Path | None = None) -> None:
        self.db_path = db_path
        self.output_dir = output_dir
        self.tz = ZoneInfo(tz_name)
        self.excluded_file = excluded_file or Path(__file__).resolve().parents[1] / "data" / "excluded_projects.json"
        self.excluded_projects: set[str] = set()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._init_schema()
```

在 `__init__` 之后新增方法（放在 `close()` 之前或之后均可，与现有布局一致即可）：

```python
    def _load_excluded(self) -> set[str]:
        try:
            payload = json.loads(self.excluded_file.read_text(encoding="utf-8"))
            entries = payload.get("excluded") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                return set()
            return {str(item).strip() for item in entries if str(item).strip()}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] 排除名单读取失败，按空名单处理: {exc}", file=sys.stderr)
            return set()
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_analyzer -v`
Expected: 全部 PASS（含既有 `test_incremental_cross_day_and_cost_attribution`）

- [ ] **Step 5: Commit**

```bash
git add scripts/analyze_sessions.py tests/test_analyzer.py
git commit -m "feat(scanner): 加载项目统计排除名单 data/excluded_projects.json"
```

---

### Task 2: 扫描器——materialize_dashboard 聚合层排除

**Files:**
- Modify: `scripts/analyze_sessions.py:334-393` (`scan`) 与 `scripts/analyze_sessions.py:746-831` (`materialize_dashboard`)
- Test: `tests/test_analyzer.py`

**Interfaces:**
- Consumes: Task 1 的 `self.excluded_projects` / `_load_excluded()`
- Produces: `materialize_dashboard()` 输出的 kpis/daily/projects/issues/sessions 及会话详情文件均不含被排除项目；新私有方法 `_exclude_clause() -> tuple[str, list[str]]`

- [ ] **Step 1: 写失败测试**

在 `tests/test_analyzer.py` 追加（复用 setUp 里已有的 session-1/demo fixture）：

```python
    def _write_other_session(self):
        lines = [
            event("2026-08-11T10:00:00Z", "session_meta", {"id": "session-2", "timestamp": "2026-08-11T10:00:00Z", "cwd": "D:\\projects\\other"}),
            event("2026-08-11T10:00:01Z", "event_msg", {"type": "task_started", "turn_id": "turn-o1"}),
            event("2026-08-11T10:00:02Z", "event_msg", {"type": "user_message", "message": "分析一下日志格式"}),
            event("2026-08-11T10:00:03Z", "event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 80, "cached_input_tokens": 0, "output_tokens": 15, "reasoning_output_tokens": 5, "total_tokens": 100}}}),
            event("2026-08-11T10:00:04Z", "event_msg", {"type": "agent_message", "phase": "final", "message": "建议将日志改为结构化输出。"}),
            event("2026-08-11T10:00:05Z", "event_msg", {"type": "task_complete", "turn_id": "turn-o1"}),
        ]
        (self.sessions / "rollout-other.jsonl").write_text("".join(lines), encoding="utf-8")

    def test_materialize_excludes_configured_projects_and_recovers(self):
        self._write_other_session()
        config = self.root / "data" / "excluded_projects.json"
        analyzer = SessionAnalyzer(self.db, self.output, excluded_file=config)
        try:
            analyzer.scan([self.root / "sessions"])
            baseline = json.loads((self.output / "dashboard.json").read_text(encoding="utf-8"))
            self.assertEqual({p["name"] for p in baseline["projects"]}, {"demo", "other"})
            baseline_daily = {d["date"]: d for d in baseline["daily"]}
            demo_before = next(p for p in baseline["projects"] if p["name"] == "demo")

            config.write_text(json.dumps({"version": 1, "updated_at": "t", "excluded": ["other"]}), encoding="utf-8")
            # 生产语义等价：scan 在有事件变更时会重读名单再物化；此处无新事件，
            # 直接重读名单并物化以验证同一行为。
            analyzer.excluded_projects = analyzer._load_excluded()
            analyzer.materialize_dashboard()
            excluded_dash = json.loads((self.output / "dashboard.json").read_text(encoding="utf-8"))
            self.assertEqual({p["name"] for p in excluded_dash["projects"]}, {"demo"})
            self.assertEqual(excluded_dash["kpis"]["issues"], baseline["kpis"]["issues"] - 1)
            self.assertEqual(excluded_dash["kpis"]["total_tokens"], baseline["kpis"]["total_tokens"] - 100)
            self.assertTrue(all(i["project"] != "other" for i in excluded_dash["issues"]))
            self.assertTrue(all(s["project"] != "other" for s in excluded_dash["sessions"]))
            daily = {d["date"]: d for d in excluded_dash["daily"]}
            self.assertEqual(set(daily), set(baseline_daily))
            self.assertEqual(daily["2026-08-11"]["tokens"], baseline_daily["2026-08-11"]["tokens"] - 100)
            demo_after = next(p for p in excluded_dash["projects"] if p["name"] == "demo")
            self.assertEqual(demo_before, demo_after)
            self.assertFalse((self.output / "sessions" / "session-2.json").exists())
            self.assertTrue((self.output / "sessions" / "session-1.json").exists())

            config.unlink()
            analyzer.excluded_projects = analyzer._load_excluded()
            analyzer.materialize_dashboard()
            restored = json.loads((self.output / "dashboard.json").read_text(encoding="utf-8"))
            self.assertEqual({p["name"] for p in restored["projects"]}, {"demo", "other"})
            self.assertEqual(restored["kpis"], baseline["kpis"])
        finally:
            analyzer.close()

    def test_materialize_with_empty_list_matches_baseline(self):
        self._write_other_session()
        config = self.root / "data" / "excluded_projects.json"
        analyzer = SessionAnalyzer(self.db, self.output, excluded_file=config)
        try:
            analyzer.scan([self.root / "sessions"])
            baseline = json.loads((self.output / "dashboard.json").read_text(encoding="utf-8"))
            config.write_text(json.dumps({"version": 1, "updated_at": "t", "excluded": []}), encoding="utf-8")
            analyzer.materialize_dashboard()
            again = json.loads((self.output / "dashboard.json").read_text(encoding="utf-8"))
            self.assertEqual(again["kpis"], baseline["kpis"])
            self.assertEqual(again["projects"], baseline["projects"])
            self.assertEqual(again["issues"], baseline["issues"])
            self.assertEqual(again["sessions"], baseline["sessions"])
        finally:
            analyzer.close()
```

注意：setUp 已创建 `self.db = self.root/"data"/"test.db"`，因此 `root/data/` 目录必然存在，测试可直接写该目录下的配置文件。

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_analyzer.AnalyzerTests.test_materialize_excludes_configured_projects_and_recovers -v`
Expected: FAIL —— 第一个断言即挂（当前实现不过滤，projects 集合仍含 "other"；且 `SessionAnalyzer(...)` 尚无 `excluded_file` 参数则先报 TypeError，说明 Task 1 未完成）

- [ ] **Step 3: 最小实现**

`scripts/analyze_sessions.py`：

(a) `scan()` 开头（`run_id = cursor.lastrowid` 之后一行）加入：

```python
        self.excluded_projects = self._load_excluded()
```

(b) 新增辅助方法（放在 `_load_excluded` 之后）：

```python
    def _exclude_clause(self) -> tuple[str, list[str]]:
        excluded = sorted(self.excluded_projects)
        if not excluded:
            return "", []
        placeholders = ",".join("?" for _ in excluded)
        return f" AND project NOT IN ({placeholders})", excluded
```

(c) `materialize_dashboard()` 开头两条查询替换为带过滤版本：

```python
        clause, params = self._exclude_clause()
        issues = [dict(row) for row in self.conn.execute("SELECT * FROM issues WHERE 1=1" + clause + " ORDER BY updated_at DESC", params).fetchall()]
        sessions = [dict(row) for row in self.conn.execute("SELECT * FROM sessions WHERE 1=1" + clause + " ORDER BY last_event_at DESC", params).fetchall()]
```

(d) daily 聚合查询追加子句（原 `WHERE local_date IS NOT NULL` 之后直接拼 `clause`）：

```python
        for row in self.conn.execute("SELECT local_date,session_id,total_tokens,wall_seconds,acceptance FROM issues WHERE local_date IS NOT NULL" + clause, params):
```

(e) projects 聚合查询同样追加：

```python
        for row in self.conn.execute("SELECT project,session_id,type,status,acceptance,total_tokens,wall_seconds,updated_at FROM issues WHERE 1=1" + clause, params):
```

kpis 由内存中的 `issues`/`sessions` 列表计算（现实现如此），自动生效；会话详情循环遍历过滤后的 `sessions`，被排除会话自然跳过，无需额外改动。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_analyzer -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/analyze_sessions.py tests/test_analyzer.py
git commit -m "feat(scanner): 物化看板时按排除名单过滤全部统计区块"
```

---

### Task 3: 前端过滤模块 filterDashboard（TDD）

**Files:**
- Create: `app/lib/exclusions.ts`
- Create: `tests/exclusions.test.ts`
- Modify: `package.json:12`（test 脚本）、`tsconfig.json`（allowImportingTsExtensions）

**Interfaces:**
- Consumes: 无
- Produces: `export type Dashboard`（字段同 page.tsx 现有定义）；`export function filterDashboard(data: Dashboard, excluded: string[], now?: Date): Dashboard`（excluded 为空时返回原引用）。Task 5/6 依赖这两个导出。

- [ ] **Step 1: 写失败测试**

创建 `tests/exclusions.test.ts`：

```ts
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
```

- [ ] **Step 2: 配置 node:test 直跑 TS 并确认失败**

`tsconfig.json` 的 `compilerOptions` 增加（`noEmit` 已为 true，满足该选项前提）：

```json
    "allowImportingTsExtensions": true,
```

`package.json` 的 `test` 脚本改为：

```json
    "test": "node --test tests/exclusions.test.ts && npm run build && node --test tests/rendered-html.test.mjs",
```

Run: `node --test tests/exclusions.test.ts`
Expected: FAIL —— 找不到模块 `../app/lib/exclusions.ts`

- [ ] **Step 3: 实现 app/lib/exclusions.ts**

```ts
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
```

- [ ] **Step 4: 运行确认通过**

Run: `node --test tests/exclusions.test.ts`
Expected: 2 个测试 PASS（若报 "Unknown file extension .ts"，改用 `NODE_OPTIONS=--experimental-strip-types node --test tests/exclusions.test.ts`）

- [ ] **Step 5: 类型检查**

Run: `npx tsc --noEmit`
Expected: 无错误退出（exit 0）

- [ ] **Step 6: Commit**

```bash
git add app/lib/exclusions.ts tests/exclusions.test.ts package.json tsconfig.json
git commit -m "feat(web): filterDashboard 展示层排除过滤与 KPI 重算"
```

---

### Task 4: Vite dev 中间件——配置读写 API

**Files:**
- Create: `build/exclusions-server.ts`
- Modify: `vite.config.ts:44-57`（plugins 数组）

**Interfaces:**
- Consumes: 名单文件契约（Global Constraints）
- Produces: HTTP 接口 `GET /api/exclusions -> {excluded: string[]}`、`POST /api/exclusions` body `{excluded: string[]}` -> `{ok: true, excluded}`（400 校验失败 / 405 方法不支持 / 500 内部错误）；Task 5/6 通过 fetch 使用

- [ ] **Step 1: 创建 build/exclusions-server.ts**

```ts
// Dev-only middleware: read/write the shared exclusion list from the Node side.
// The app runs inside workerd, whose node:fs can only touch ephemeral /tmp —
// so persistence must happen here, in the Vite dev server process.
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import type { ServerResponse } from "node:http";
import path from "node:path";
import type { Connect, Plugin } from "vite";

export const EXCLUSIONS_FILE = path.resolve(process.cwd(), "data", "excluded_projects.json");

async function readExclusions(): Promise<string[]> {
  try {
    const payload = JSON.parse(await readFile(EXCLUSIONS_FILE, "utf8"));
    const entries = payload?.excluded;
    return Array.isArray(entries) ? entries.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function sendJson(res: ServerResponse, body: unknown, status = 200): void {
  res.statusCode = status;
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(body));
}

async function handleExclusions(req: Connect.IncomingMessage, res: ServerResponse): Promise<void> {
  if (req.method === "GET") {
    sendJson(res, { excluded: await readExclusions() });
    return;
  }
  if (req.method === "POST") {
    let raw = "";
    req.on("data", (chunk: Buffer) => { raw += chunk; });
    await new Promise<void>((resolve, reject) => {
      req.on("end", resolve);
      req.on("error", reject);
    });
    let payload: any;
    try {
      payload = JSON.parse(raw || "null");
    } catch {
      payload = null;
    }
    const entries = payload?.excluded;
    if (!Array.isArray(entries) || entries.some((item: unknown) => typeof item !== "string" || !(item as string).trim())) {
      sendJson(res, { error: 'body 须为 {"excluded": string[]}，元素须为非空字符串' }, 400);
      return;
    }
    const excluded = [...new Set(entries.map((item: string) => item.trim()))];
    await mkdir(path.dirname(EXCLUSIONS_FILE), { recursive: true });
    const tmpPath = `${EXCLUSIONS_FILE}.tmp`;
    await writeFile(tmpPath, `${JSON.stringify({ version: 1, updated_at: new Date().toISOString(), excluded }, null, 2)}\n`, "utf8");
    await rename(tmpPath, EXCLUSIONS_FILE);
    sendJson(res, { ok: true, excluded });
    return;
  }
  sendJson(res, { error: "method not allowed" }, 405);
}

export function exclusionsServer(): Plugin {
  return {
    name: "exclusions-server",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/api/exclusions", (req, res) => {
        handleExclusions(req as Connect.IncomingMessage, res as ServerResponse).catch(() => sendJson(res as ServerResponse, { error: "internal error" }, 500));
      });
    },
  };
}
```

- [ ] **Step 2: 注册到 vite.config.ts**

顶部 import 区加入：

```ts
import { exclusionsServer } from "./build/exclusions-server";
```

plugins 数组（cloudflare 之前插入，保证拦截优先于 worker 代理）：

```ts
    plugins: [
      vinext(),
      sites(),
      exclusionsServer(),
      cloudflare({
        viteEnvironment: { name: "rsc", childEnvironments: ["ssr"] },
        config: localBindingConfig,
      }),
    ],
```

- [ ] **Step 3: 类型检查与 lint**

Run: `npx tsc --noEmit && npm run lint`
Expected: 均无错误退出

- [ ] **Step 4: 启动 dev server 验证端到端**

后台运行 `npm run dev`，从启动输出中取本地地址（形如 `Local: http://localhost:5173/`，下称 `$BASE`；vinext 可能使用 3000，以实际输出为准）。依次执行并核对：

```bash
curl -s $BASE/api/exclusions
```
Expected: `{"excluded":[]}`（或已存在名单内容）

```bash
curl -s -X POST -H 'content-type: application/json' -d '{"excluded":["demo"," demo ","demo"]}' $BASE/api/exclusions
```
Expected: `{"ok":true,"excluded":["demo"]}`（服务端 trim + 去重）

```bash
curl -s -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{"excluded":["demo",""]}' $BASE/api/exclusions
curl -s -X POST -H 'content-type: application/json' -d '{"excluded":[]}' $BASE/api/exclusions
```
Expected: 第一个返回 `400`（含空字符串元素，整体拒绝）；第二个返回 `{"ok":true,"excluded":[]}`（空数组=清空名单，合法）

随后检查文件内容：

Run: `cat data/excluded_projects.json`
Expected: 含 `"excluded": ["demo"]` 且带 `version`/`updated_at` 字段

```bash
curl -s -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{"excluded":[42]}' $BASE/api/exclusions
curl -s -X GET $BASE/api/exclusions
curl -s -o /dev/null -w '%{http_code}' -X PUT $BASE/api/exclusions
```
Expected: `400`；`{"excluded":["demo"]}`；`405`

- [ ] **Step 5: 清理验证残留并停止 server**

```bash
rm -f data/excluded_projects.json data/excluded_projects.json.tmp
```

停止后台 dev server 进程。

- [ ] **Step 6: Commit**

```bash
git add build/exclusions-server.ts vite.config.ts
git commit -m "feat(web): Vite dev 中间件提供排除名单读写 API"
```

---

### Task 5: 看板接入过滤数据流

**Files:**
- Modify: `app/page.tsx`

**Interfaces:**
- Consumes: Task 3 的 `filterDashboard` / `Dashboard`；Task 4 的 `GET /api/exclusions`
- Produces: 组件内状态 `excluded: string[]`、`saving: boolean`、`saveNotice: string`、`apiAvailable: boolean`；回调 `toggleExcluded(name: string): void`、`saveExclusions(): Promise<void>`；渲染数据源 `view`（过滤后 Dashboard）。Task 6 的 SettingsView 以这些为 props。

- [ ] **Step 1: 引入模块与状态**

`app/page.tsx` 头部：

```tsx
import { useEffect, useMemo, useState } from "react";
import { filterDashboard, type Dashboard } from "./lib/exclusions";
```

删除组件外原有的本地 `type Dashboard = {...}` 定义（第 5-12 行），统一从 lib 导入。

`Home` 组件内、现有 state 声明之后增加：

```tsx
  const [excluded, setExcluded] = useState<string[]>([]);
  const [apiAvailable, setApiAvailable] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveNotice, setSaveNotice] = useState("");
```

- [ ] **Step 2: load() 同时拉名单**

替换现有 `load` 函数：

```tsx
  const load = () => {
    fetch(`/data/dashboard.json?t=${Date.now()}`, { cache: "no-store" })
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
      .then(setData).catch((reason) => setError(String(reason)));
    fetch("/api/exclusions", { cache: "no-store" })
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
      .then((payload) => { setApiAvailable(true); setExcluded(Array.isArray(payload.excluded) ? payload.excluded : []); })
      .catch(() => setApiAvailable(false));
  };
```

- [ ] **Step 3: 计算过滤后的视图数据**

在 `filteredIssues` 的 `useMemo` 之前加：

```tsx
  const view = useMemo(() => (data ? filterDashboard(data, excluded) : null), [data, excluded]);
```

- [ ] **Step 4: 全部视图改用 view**

自 loading 判断之后（`if (!data && !error)...` 两处守卫保留判断 `data`），把后续所有 `data.` 引用改为 `view.`。涉及位置（逐处替换，勿遗漏）：
- `const projects = [...]`（FilterBar 下拉来源）
- `recentDays`/`maxTokens`/`topCost`
- overview 视图：`view.kpis.*`、`view.daily.slice(-14)`、`filteredIssues`（其 useMemo 依赖改为 `[view, query, project, acceptance]`，内部 `data.issues` 改 `view?.issues ?? []`，守卫 `if (!view) return [];` 开头）
- issues/projects/sessions/cost 各视图中所有 `data.*`

同时 `filteredIssues` useMemo 整体替换为：

```tsx
  const filteredIssues = useMemo(() => {
    if (!view) return [];
    const needle = query.trim().toLowerCase();
    return view.issues.filter((issue) =>
      (project === "全部项目" || issue.project === project) &&
      (acceptance === "全部状态" || issue.acceptance === acceptance) &&
      (!needle || `${issue.id} ${issue.title} ${issue.project} ${issue.prompt_preview} ${issue.solution_preview}`.toLowerCase().includes(needle))
    );
  }, [view, query, project, acceptance]);
```

- [ ] **Step 5: 导航加入「设置」**

`Icon` 组件 icons 对象加一项 `settings: "⚙"`。nav 数组改为：

```tsx
  const nav: Array<[View, string, string]> = [["overview", "overview", "总览"], ["issues", "issues", "问题台账"], ["projects", "projects", "项目"], ["sessions", "sessions", "会话"], ["cost", "cost", "成本分析"], ["settings", "settings", "设置"]];
```

`View` 类型改为：

```tsx
type View = "overview" | "issues" | "projects" | "sessions" | "cost" | "settings";
```

- [ ] **Step 6: 渲染分支占位（Task 6 填充）**

在 cost 分支之后、`</main>` 之前加入：

```tsx
        {view === "settings" && <section className="panel settings-panel"><div className="panel-title"><div><h2>统计排除配置</h2><p>加载中…</p></div></div></section>}
```

- [ ] **Step 7: 验证**

Run: `npx tsc --noEmit && npm run lint`
Expected: 均无错误

Run: `npm run dev`（后台）→ 打开首页确认总览数字与改造前一致（名单为空时 `filterDashboard` 直通）；Ctrl+C 停止。

- [ ] **Step 8: Commit**

```bash
git add app/page.tsx
git commit -m "feat(web): 看板全视图接入排除名单过滤数据流"
```

---

### Task 6: 设置视图 UI、保存流程、样式与文档

**Files:**
- Modify: `app/page.tsx`（替换 Task 5 Step 6 的占位分支，新增 SettingsView 组件与保存逻辑）
- Modify: `app/globals.css`（末尾追加样式）
- Modify: `README.md`（「已实现」清单加一条）

**Interfaces:**
- Consumes: Task 5 的全部状态/回调/props；Task 4 的 POST API
- Produces: 完整可用的「设置」页（开关列表、保存、提示、API 不可用降级文案）

- [ ] **Step 1: Home 内新增保存逻辑**

在 `openSession` 函数之后加入：

```tsx
  const toggleExcluded = (name: string) => setExcluded((list) => list.includes(name) ? list.filter((item) => item !== name) : [...list, name]);
  const saveExclusions = async () => {
    setSaving(true);
    setSaveNotice("");
    try {
      const response = await fetch("/api/exclusions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ excluded }) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      const saved = Array.isArray(payload.excluded) ? payload.excluded : excluded;
      setExcluded(saved);
      setSaveNotice(`已保存 ${saved.length} 项`);
    } catch (reason) {
      setSaveNotice(`保存失败：${reason}`);
    } finally {
      setSaving(false);
    }
  };
```

- [ ] **Step 2: 替换占位分支为完整设置视图**

```tsx
        {view === "settings" && <SettingsView knownProjects={data.projects} excluded={excluded} apiAvailable={apiAvailable} saving={saving} saveNotice={saveNotice} onToggle={toggleExcluded} onSave={saveExclusions}/>}
```

注意：这里传原始 `data.projects` 而非过滤后的 `view.projects`——被排除的项目也要展示其会话数 / Token 摘要，否则会误显示为“历史条目”。

- [ ] **Step 3: 文件末尾新增 SettingsView 组件**

```tsx
function SettingsView({ knownProjects, excluded, apiAvailable, saving, saveNotice, onToggle, onSave }: {
  knownProjects: Dashboard["projects"];
  excluded: string[];
  apiAvailable: boolean;
  saving: boolean;
  saveNotice: string;
  onToggle: (name: string) => void;
  onSave: () => void;
}) {
  const meta = new Map(knownProjects.map((item) => [item.name, item]));
  const names = [...new Set([...knownProjects.map((item) => item.name), ...excluded])];
  return <section className="panel settings-panel">
    <div className="panel-title"><div><h2>统计排除配置</h2><p>勾选以排除该项目：不进入任何统计，原始证据完整保留，可随时恢复；聚合层数字在下次扫描后完全同步。</p></div></div>
    {!apiAvailable && <p className="empty">配置接口不可用：仅在 npm run dev 模式下可读写名单。</p>}
    <div className="settings-list">{names.map((name) => {
      const item = meta.get(name);
      const isExcluded = excluded.includes(name);
      return <label className="settings-row" key={name}>
        <input type="checkbox" checked={isExcluded} onChange={() => onToggle(name)}/>
        <span className="settings-main"><b>{name}</b><small>{item ? `${item.sessions} 个会话 · ${formatNumber(item.tokens)} Token` : "当前数据中无此项目（历史条目）"}</small></span>
        <span className="badge">{isExcluded ? "已排除" : "统计中"}</span>
      </label>;
    })}{!names.length && <div className="empty">暂无项目</div>}</div>
    <div className="settings-foot"><button onClick={onSave} disabled={saving}>{saving ? "保存中…" : "保存配置"}</button>{saveNotice && <small>{saveNotice}</small>}</div>
  </section>;
}
```

- [ ] **Step 4: globals.css 末尾追加样式（贴合现有单行紧凑风格）**

```css
.settings-panel { margin-top:0; padding:22px; }.settings-list { margin-top:14px; display:grid; gap:8px; }.settings-row { display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:14px; padding:13px 14px; background:#f6f7f4; border-radius:8px; cursor:pointer; transition:.15s; }.settings-row:hover { background:#eef2ef; }.settings-row input { width:16px; height:16px; accent-color:var(--teal); }.settings-main b { display:block; font-size:13px; color:#23302c; }.settings-main small { display:block; margin-top:4px; color:#8c9692; font-size:9px; }.settings-foot { margin-top:18px; display:flex; align-items:center; gap:14px; }.settings-foot button { border:0; border-radius:8px; padding:10px 20px; background:var(--teal); color:#fff; font-weight:600; cursor:pointer; }.settings-foot button:disabled { opacity:.55; cursor:default; }.settings-foot small { color:#7b8782; font-size:11px; }
```

- [ ] **Step 5: README 更新**

`README.md` 「已实现」清单末尾加一条：

```markdown
- 项目统计排除：后台「设置」页可配置不参与统计的项目；名单存 `data/excluded_projects.json`，扫描器与看板共同遵循，证据库不受影响、可随时恢复。
```

- [ ] **Step 6: 验证**

Run: `npx tsc --noEmit && npm run lint && node --test tests/exclusions.test.ts`
Expected: 均通过

手动冒烟（后台起 `npm run dev`，浏览器或 curl）：
1. 打开 `/` → 侧边栏出现「设置 ⚙」→ 进入后列出快照中的全部项目
2. 勾选某项目 → 保存 → 提示「已保存 N 项」→ 返回总览 KPI 立即变化（该项目消失）
3. `cat data/excluded_projects.json` 内容与勾选一致
4. 取消勾选 → 保存 → 总览恢复

验证后清理：`rm -f data/excluded_projects.json`，停掉 dev server。

- [ ] **Step 7: Commit**

```bash
git add app/page.tsx app/globals.css README.md
git commit -m "feat(web): 设置页管理项目统计排除名单"
```

---

### Task 7: 全量回归验证

**Files:** 无新改动（只验证）

**Interfaces:**
- Consumes: 前 6 个任务的全部产物
- Produces: 验证结论（全部绿才算完成）

- [ ] **Step 1: Python 全量**

Run: `python3 -m unittest tests.test_analyzer -v`
Expected: 全部 PASS

- [ ] **Step 2: Web 全量（含构建与冒烟）**

Run: `npm test`
Expected: exclusions.test.ts 通过 → 构建成功 → rendered-html 冒烟通过

- [ ] **Step 3: 类型与 lint**

Run: `npx tsc --noEmit && npm run lint`
Expected: 均无错误

- [ ] **Step 4: 端到端一致性抽查（可选但推荐）**

后台 `npm run dev` → 手写一份名单 `{"version":1,"updated_at":"t","excluded":["<真实存在的某项目>"]}` 到 `data/excluded_projects.json` → 刷新首页确认该项目从所有视图消失且 KPI 下降 → 直接跑一轮扫描器（Mac 本机）：

```bash
python3 scripts/analyze_sessions.py --db data/codex_insights.db --output public/data
```

等待完成后再刷新首页：前端显示的 KPI 应与快照一致（聚合层已生效）。验证后删除测试名单文件并停掉 dev server。

注意：此步会用 Mac 本机 `~/.codex/sessions` 数据做一次真实增量扫描写入 `data/codex_insights.db`（系统设计的正常操作，增量安全；如不想动真实库，可改用临时目录参数 `--db "$TMPDIR/t.db" --output "$TMPDIR/out"` 并配 `CODEX_SESSIONS_DIR` 指向构造样例）。

- [ ] **Step 5: 收尾提交（如 Step 4 有清理遗漏）**

```bash
git status   # 确认工作区干净（data/*.db 与名单文件均不入库或按需忽略）
```

---

## Self-Review 记录

1. **Spec 覆盖**：名单契约（Task 1/4）、扫描器四处查询+详情跳过（Task 2）、配置 API GET/POST+原子写（Task 4）、管理页 UI+降级提示（Task 5/6）、前端过滤+KPI/daily 重算（Task 3/5）、边界情况（各任务测试覆盖：损坏名单/空名单/历史条目/405/400）、测试策略（Python/Node/手动验收 = Task 1-7）——全覆盖。
2. **占位符扫描**：无 TBD/TODO；所有代码步骤含完整代码。
3. **类型一致性**：`filterDashboard(data, excluded, now?)`、`_load_excluded()`、`_exclude_clause()`、SettingsView props 与消费方签名逐一核对一致；Task 4 Step 4 的 trim/去重预期结果已在步骤内自我纠正说明。
