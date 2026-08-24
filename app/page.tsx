"use client";

import { useEffect, useMemo, useState } from "react";
import { filterDashboard, type Dashboard } from "./lib/exclusions";

type View = "overview" | "issues" | "projects" | "sessions" | "cost" | "settings";

const labels: Record<string, string> = {
  bug: "缺陷", feature: "需求", optimization: "优化", analysis: "分析", task: "任务",
  open: "待处理", proposed: "有方案", verification: "待验收", resolved: "已解决",
  needs_followup: "需跟进", blocked: "受阻", accepted: "已采纳", rejected: "未采纳",
  pending: "待判断", proposed_acceptance: "已提出", implemented_unverified: "已实施·待验收", proposed_value: "已提出",
};

const formatNumber = (value = 0) => new Intl.NumberFormat("zh-CN", { notation: value >= 100000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value);
const formatDate = (value?: string) => value ? new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value)) : "—";
const formatDuration = (seconds = 0) => seconds < 60 ? `${Math.round(seconds)}秒` : seconds < 3600 ? `${Math.round(seconds / 60)}分钟` : `${(seconds / 3600).toFixed(1)}小时`;

function Icon({ name }: { name: string }) {
  const icons: Record<string, string> = {
    overview: "⌂", issues: "◎", projects: "◇", sessions: "◫", cost: "↗", search: "⌕", sync: "↻", chevron: "›", settings: "⚙",
  };
  return <span aria-hidden="true" className="icon">{icons[name] || "·"}</span>;
}

function StatusBadge({ value, type = "status" }: { value: string; type?: string }) {
  const text = value === "proposed" && type === "acceptance" ? "已提出" : labels[value] || value;
  return <span className={`badge badge-${value}`}>{text}</span>;
}

function Metric({ label, value, hint, tone }: { label: string; value: string; hint: string; tone?: string }) {
  return <article className={`metric ${tone || ""}`}><div className="metric-head"><span>{label}</span><span className="metric-mark">↗</span></div><strong>{value}</strong><small>{hint}</small></article>;
}

function Skeleton() {
  return <main className="loading"><div className="loader-orbit"/><h1>正在读取 Codex 分析数据</h1><p>首次扫描会保留完整会话证据，请稍候。</p></main>;
}

export default function Home() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState("");
  const [view, setView] = useState<View>("overview");
  const [query, setQuery] = useState("");
  const [project, setProject] = useState("全部项目");
  const [acceptance, setAcceptance] = useState("全部状态");
  const [selectedIssue, setSelectedIssue] = useState<Record<string, any> | null>(null);
  const [sessionDetail, setSessionDetail] = useState<Record<string, any> | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [excluded, setExcluded] = useState<string[]>([]);
  const [apiAvailable, setApiAvailable] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveNotice, setSaveNotice] = useState("");

  const load = () => {
    fetch(`/data/dashboard.json?t=${Date.now()}`, { cache: "no-store" })
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
      .then(setData).catch((reason) => setError(String(reason)));
    fetch("/api/exclusions", { cache: "no-store" })
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
      .then((payload) => { setApiAvailable(true); setExcluded(Array.isArray(payload.excluded) ? payload.excluded : []); })
      .catch(() => setApiAvailable(false));
  };
  useEffect(() => { load(); const timer = setInterval(load, 60_000); return () => clearInterval(timer); }, []);

  const dashboard = useMemo(() => (data ? filterDashboard(data, excluded) : null), [data, excluded]);

  const filteredIssues = useMemo(() => {
    if (!dashboard) return [];
    const needle = query.trim().toLowerCase();
    return dashboard.issues.filter((issue) =>
      (project === "全部项目" || issue.project === project) &&
      (acceptance === "全部状态" || issue.acceptance === acceptance) &&
      (!needle || `${issue.id} ${issue.title} ${issue.project} ${issue.prompt_preview} ${issue.solution_preview}`.toLowerCase().includes(needle))
    );
  }, [dashboard, query, project, acceptance]);

  if (!data && !error) return <Skeleton />;
  if (!data || !dashboard) return <main className="loading"><h1>暂无分析数据</h1><p>请先运行 scripts/update-dashboard.ps1。</p><code>{error}</code></main>;

  const projects = ["全部项目", ...dashboard.projects.map((item) => item.name)];
  const recentDays = dashboard.daily.slice(-14);
  const maxTokens = Math.max(...recentDays.map((day) => day.tokens), 1);
  const topCost = [...dashboard.issues].sort((a, b) => b.total_tokens - a.total_tokens).slice(0, 8);
  const nav: Array<[View, string, string]> = [["overview", "overview", "总览"], ["issues", "issues", "问题台账"], ["projects", "projects", "项目"], ["sessions", "sessions", "会话"], ["cost", "cost", "成本分析"], ["settings", "settings", "设置"]];
  const openSession = async (session: Record<string, any>) => {
    setSessionLoading(true);
    try {
      const response = await fetch(`/data/sessions/${session.id}.json`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setSessionDetail(await response.json());
    } finally {
      setSessionLoading(false);
    }
  };
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

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-symbol">C</div><div><b>Codex Insight</b><span>研发效能看板</span></div></div>
      <nav>{nav.map(([id, icon, text]) => <button key={id} className={view === id ? "active" : ""} onClick={() => setView(id)}><Icon name={icon}/><span>{text}</span>{id === "issues" && <em>{dashboard.kpis.open_issues}</em>}</button>)}</nav>
      <div className="sidebar-foot"><span className="pulse-dot"/><div><b>后台自动更新</b><small>最近 {formatDate(dashboard.meta.generated_at)}</small></div></div>
    </aside>

    <div className="workspace">
      <header className="topbar">
        <div><h1>{nav.find(([id]) => id === view)?.[2]}</h1><p>{view === "overview" ? "Codex 协作全景与研发投入" : "基于会话证据的只读分析"}</p></div>
        <div className="top-actions"><label className="search"><Icon name="search"/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索问题、会话或项目"/></label><button className="sync" onClick={load} aria-label="立即刷新"><Icon name="sync"/></button><div className="avatar">PM</div></div>
      </header>

      <main className="content">
        {view === "overview" && <>
          <section className="metrics-grid">
            <Metric label="今日活跃会话" value={formatNumber(dashboard.kpis.today_sessions)} hint={`累计 ${dashboard.kpis.sessions} 个会话`} />
            <Metric label="问题与任务" value={formatNumber(dashboard.kpis.issues)} hint={`${dashboard.kpis.open_issues} 项仍需推进`} tone="amber" />
            <Metric label="Token 投入" value={formatNumber(dashboard.kpis.total_tokens)} hint={`跨日会话 ${dashboard.kpis.cross_day_sessions} 个`} tone="blue" />
            <Metric label="方案采纳率" value={`${dashboard.kpis.acceptance_rate}%`} hint={`${dashboard.kpis.accepted} 项有用户确认`} tone="green" />
          </section>
          <section className="overview-grid">
            <article className="panel trend-panel"><div className="panel-title"><div><h2>每日研发投入</h2><p>Token 与问题量趋势 · 最近 14 个活跃日</p></div><span className="legend"><i/>Token</span></div><div className="bars">{recentDays.map((day) => <div className="bar-col" key={day.date}><div className="bar-tip">{formatNumber(day.tokens)}<small>{day.issues} 项</small></div><div className="bar" style={{ height: `${Math.max(8, day.tokens / maxTokens * 100)}%` }}/><span>{day.date.slice(5)}</span></div>)}</div></article>
            <article className="panel attention"><div className="panel-title"><div><h2>管理关注</h2><p>基于会话行为自动聚合</p></div></div><div className="attention-list"><div><strong>{dashboard.kpis.open_issues}</strong><span>待推进问题</span><small>开放、受阻或仅有方案</small></div><div><strong>{dashboard.kpis.bugs}</strong><span>缺陷类问题</span><small>由问题语义规则识别</small></div><div><strong>{formatDuration(dashboard.kpis.wall_seconds)}</strong><span>协作墙钟耗时</span><small>包含等待与工具执行</small></div></div><button onClick={() => setView("issues")}>查看问题台账 <Icon name="chevron"/></button></article>
          </section>
          <section className="panel issue-panel"><div className="panel-title"><div><h2>近期问题</h2><p>方案、实施与验收状态一览</p></div><button className="text-button" onClick={() => setView("issues")}>全部问题 <Icon name="chevron"/></button></div><IssueTable issues={filteredIssues.slice(0, 7)} onSelect={setSelectedIssue}/></section>
        </>}

        {view === "issues" && <section className="panel issue-panel full"><FilterBar projects={projects} project={project} setProject={setProject} acceptance={acceptance} setAcceptance={setAcceptance}/><div className="panel-title compact"><div><h2>问题与决策台账</h2><p>共 {filteredIssues.length} 项；采纳状态均显示推断依据</p></div></div><IssueTable issues={filteredIssues} onSelect={setSelectedIssue}/></section>}

        {view === "projects" && <section className="project-grid">{dashboard.projects.map((item) => <article className="project-card" key={item.name}><div className="project-icon">{item.name.slice(0, 2).toUpperCase()}</div><div className="project-heading"><h2>{item.name}</h2><p>最后活动 {formatDate(item.last_activity)}</p></div><div className="project-stats"><span><b>{item.issues}</b>问题</span><span><b>{item.bugs}</b>缺陷</span><span><b>{item.resolved}</b>已推进</span><span><b>{formatNumber(item.tokens)}</b>Token</span></div><div className="progress"><i style={{width: `${item.issues ? item.resolved / item.issues * 100 : 0}%`}}/></div><small>{item.sessions} 个会话 · {formatDuration(item.wall_seconds)}</small></article>)}</section>}

        {view === "sessions" && <section className="panel session-panel"><div className="panel-title"><div><h2>会话生命周期</h2><p>会话按最后活动更新，跨日会话不会在创建日提前关闭；点击查看完整对话</p></div>{sessionLoading && <span className="inline-loading">正在读取…</span>}</div><div className="session-list">{dashboard.sessions.map((session) => <button className="session-row" key={session.id} onClick={() => openSession(session)}><div className="session-state"><i className={session.spans_days ? "spanning" : ""}/></div><div className="session-main"><h3>{session.title || "未命名会话"}</h3><p>{session.project} · {session.git_branch || "无 Git 分支"}</p><code>{session.id}</code></div><div className="session-meta"><span>{session.issue_count} 项问题</span><span>{formatNumber(session.tokens)} Token</span><span>{session.created_day} → {session.last_day}</span>{session.spans_days && <b>跨日</b>}<Icon name="chevron"/></div></button>)}</div></section>}

        {view === "cost" && <section className="cost-layout"><article className="panel"><div className="panel-title"><div><h2>高成本问题 Top 8</h2><p>问题级 Token、墙钟耗时与活跃耗时</p></div></div><div className="cost-list">{topCost.map((item, index) => <button key={item.id} onClick={() => setSelectedIssue(item)}><em>{String(index + 1).padStart(2, "0")}</em><div><b>{item.title}</b><span>{item.project} · {item.id}</span></div><strong>{formatNumber(item.total_tokens)}</strong><small>{formatDuration(item.wall_seconds)}</small></button>)}</div></article><article className="panel cost-note"><h2>成本口径</h2><dl><dt>Token</dt><dd>Codex token_count 的 last_token_usage 按 turn 累加，包含缓存输入。</dd><dt>墙钟耗时</dt><dd>从 task_started 到 task_complete/turn_aborted；开放 turn 截止最后消息。</dd><dt>活跃耗时</dt><dd>相邻事件间隔最多计 5 分钟，降低长时间离席造成的偏差。</dd><dt>问题归属</dt><dd>每个用户 turn 为一个问题单元；短跟进可关联到上一问题根节点。</dd></dl></article></section>}
        {view === "settings" && <SettingsView knownProjects={data.projects} excluded={excluded} apiAvailable={apiAvailable} saving={saving} saveNotice={saveNotice} onToggle={toggleExcluded} onSave={saveExclusions}/>}
      </main>
    </div>

    {selectedIssue && <IssueDrawer issue={selectedIssue} onClose={() => setSelectedIssue(null)}/>} 
    {sessionDetail && <SessionDrawer detail={sessionDetail} onClose={() => setSessionDetail(null)}/>} 
  </div>;
}

function FilterBar({ projects, project, setProject, acceptance, setAcceptance }: { projects: string[]; project: string; setProject: (v: string) => void; acceptance: string; setAcceptance: (v: string) => void }) {
  return <div className="filters"><label>项目<select value={project} onChange={(e) => setProject(e.target.value)}>{projects.map((item) => <option key={item}>{item}</option>)}</select></label><label>采纳状态<select value={acceptance} onChange={(e) => setAcceptance(e.target.value)}><option value="全部状态">全部状态</option><option value="accepted">已采纳</option><option value="implemented_unverified">已实施·待验收</option><option value="proposed">已提出</option><option value="rejected">未采纳</option><option value="pending">待判断</option></select></label></div>;
}

function IssueTable({ issues, onSelect }: { issues: Array<Record<string, any>>; onSelect: (issue: Record<string, any>) => void }) {
  return <div className="table-wrap"><table><thead><tr><th>问题</th><th>项目</th><th>类型</th><th>推进状态</th><th>方案采纳</th><th className="number">Token</th><th className="number">耗时</th><th/></tr></thead><tbody>{issues.map((issue) => <tr key={issue.id} onClick={() => onSelect(issue)}><td><b>{issue.title}</b><small>{issue.id} · {formatDate(issue.updated_at)}</small></td><td>{issue.project}</td><td><StatusBadge value={issue.type}/></td><td><StatusBadge value={issue.status}/></td><td><StatusBadge value={issue.acceptance} type="acceptance"/></td><td className="number">{formatNumber(issue.total_tokens)}</td><td className="number">{formatDuration(issue.wall_seconds)}</td><td><Icon name="chevron"/></td></tr>)}</tbody></table>{!issues.length && <div className="empty">没有符合当前筛选条件的问题</div>}</div>;
}

function IssueDrawer({ issue, onClose }: { issue: Record<string, any>; onClose: () => void }) {
  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="drawer" onMouseDown={(e) => e.stopPropagation()}><button className="drawer-close" onClick={onClose}>×</button><div className="drawer-kicker">{issue.id} · {issue.project}</div><h2>{issue.title}</h2><div className="drawer-badges"><StatusBadge value={issue.type}/><StatusBadge value={issue.status}/><StatusBadge value={issue.acceptance} type="acceptance"/></div><section><h3>用户问题</h3><p>{issue.prompt_preview || "无摘要"}</p></section><section><h3>Codex 方案 / 结果</h3><p>{issue.solution_preview || "尚无最终方案"}</p></section><section className="evidence"><h3>采纳判断依据</h3><p>{issue.evidence}</p><span>置信度 {Math.round((issue.acceptance_confidence || 0) * 100)}%</span></section><div className="drawer-cost"><div><span>Token</span><b>{formatNumber(issue.total_tokens)}</b></div><div><span>墙钟耗时</span><b>{formatDuration(issue.wall_seconds)}</b></div><div><span>活跃耗时</span><b>{formatDuration(issue.active_seconds)}</b></div><div><span>工具调用</span><b>{issue.tool_calls}</b></div></div>{issue.changed_files?.length > 0 && <section><h3>修改文件</h3><ul className="file-list">{issue.changed_files.map((file: string) => <li key={file}>{file}</li>)}</ul></section>}</aside></div>;
}

function SessionDrawer({ detail, onClose }: { detail: Record<string, any>; onClose: () => void }) {
  const session = detail.session || {};
  const messages = detail.messages || [];
  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="drawer session-drawer" onMouseDown={(e) => e.stopPropagation()}><button className="drawer-close" onClick={onClose}>×</button><div className="drawer-kicker">会话证据 · {session.project}</div><h2>{session.title || "未命名会话"}</h2><div className="session-facts"><span>{messages.length} 条消息</span><span>{formatDate(session.first_event_at)} → {formatDate(session.last_event_at)}</span><code>{session.id}</code></div><div className="transcript">{messages.map((message: Record<string, any>, index: number) => <article className={`message message-${message.role}`} key={`${message.timestamp}-${index}`}><header><b>{message.role === "user" ? "用户" : "Codex"}</b><time>{formatDate(message.timestamp)}</time>{message.phase && <span>{message.phase}</span>}</header><pre>{message.content}</pre></article>)}</div></aside></div>;
}

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
