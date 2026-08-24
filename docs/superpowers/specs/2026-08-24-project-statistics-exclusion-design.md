# 项目统计排除配置 · 设计文档

日期：2026-08-24
状态：待用户审阅

## 背景与目标

看板（Codex Insight）目前统计 `sessions` 表中出现的全部项目，包括临时目录产生的噪音项目（如 `new-chat`、`words`）。目标：提供后台配置能力，让指定项目**不参与统计**。

"不作统计"分两层生效：

1. **聚合层（真排除）**：扫描器生成 `public/data/dashboard.json` 时跳过被排除项目——KPI、每日趋势、项目卡片、问题台账、会话列表、会话详情 JSON 全部不含它们。
2. **展示层（防御性过滤）**：前端加载快照时应用同一名单并重算 KPI，消除"名单已改、快照未重新物化"窗口期内的数字不一致。

关键性质：**排除只影响统计输出，SQLite 证据库完整保留**。取消排除后无需重扫，下次物化即恢复全部数字。

## 非目标

- 不删除、不修改任何原始证据数据（events/messages/issues 等表不动）。
- 不做线上部署形态的适配（已确认仅本机运行；若将来部署需重新评估存储方案）。
- 不引入鉴权（与现状一致：仅监听本机、README 声明不部署公网）。

## 已确认的关键约束

- 站点经 vinext + @cloudflare/vite-plugin 运行在 **workerd(miniflare)** 中。官方文档确认 workerd 的 `node:fs` 只能写临时 `/tmp` 目录且请求结束即销毁，项目目录不可写 → **API 路由内写文件不可行**。
- 扫描器为独立 Python 进程（`scripts/analyze_sessions.py`），与站点无共享运行时 → 名单必须通过**双方都能读的文件**传递。

## 方案选择

**采用：方案 A —— JSON 名单文件为唯一真源 + Vite dev 服务器中间件提供读写接口。**

弃用方案及原因：

- 方案 B（存 D1）：Python 扫描器须直接读 miniflare 内部 sqlite（路径靠 glob 推测、WAL 并发、双运行时共享单库），契约脆弱；名单不可手改。仅在将来线上部署时才有必要，届时再评估。
- 方案 C（纯 localStorage 浏览器过滤）：扫描器无法感知名单，无法真排除；换浏览器丢配置。

## 详细设计

### 1. 名单文件契约（唯一真源）

路径：`data/excluded_projects.json`（与 SQLite 库同目录；`.gitignore` 当前忽略 `data/*.db`，此 JSON 可入库同步）。

```json
{
  "version": 1,
  "updated_at": "2026-08-24T12:00:00Z",
  "excluded": ["new-chat", "words"]
}
```

- **按项目名精确匹配**（`sessions.project` / `issues.project` 字段值）。这是看板所有聚合的原生粒度，同名多目录在快照中本就合并为一个项目。
- 文件不存在、损坏或字段缺失 → 一律视为空名单（行为与现状完全一致），绝不中断消费方。
- 「未关联项目」视作普通项目名，可被排除。

### 2. 扫描器改动（scripts/analyze_sessions.py，约 30 行）

1. `SessionAnalyzer.__init__` 新增可选参数 `excluded_file: Path | None = None`；缺省解析为 `<项目根>/data/excluded_projects.json`（测试传临时路径）。
2. 新增 `_load_excluded() -> set[str]`：每轮 `scan()` 开始时重新读取（watch 模式下名单变更 ≤ 一个扫描周期生效）；任何异常打 `[warn]` 并返回空集合。
3. `materialize_dashboard()`：issues / sessions / daily / projects 四处 SQL 查询追加 `WHERE project NOT IN (...)`；`kpis` 由过滤后的列表计算，自动生效。
4. 会话详情 JSON：被排除项目的 session 跳过，不生成文件。
5. 无新增 CLI 参数（YAGNI；测试经构造函数注入路径）。

### 3. 配置 API（新增 build/exclusions-server.ts，约 60 行）

Vite 插件（`apply: "serve"`，仅 dev 生效），注册于 `vite.config.ts` 的 cloudflare 插件**之前**；`configureServer` 中间件拦截：

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/exclusions` | 读名单文件返回 `{ excluded: [...] }`；缺失/损坏按空名单 |
| POST | `/api/exclusions` | 校验 body 为字符串数组（允许空数组=清空排除；每个元素须为非空字符串）→ trim、去重 → 临时文件 + rename 原子写入（含 `version`/`updated_at`）→ 返回 `{ ok, excluded }` |

每次请求读盘（手改文件即时可见）；校验失败返回 400，读盘失败返回 500，均为 JSON 错误信息。纯 Node fs，不受 workerd 限制。

### 4. 管理页 UI（app/page.tsx 新增「设置」视图）

- 侧边导航增加第六项「设置」（图标 ⚙）。
- 页面：说明文案（"排除的项目不进入任何统计；原始证据保留，可随时恢复"）→ 项目开关列表 → 保存按钮与状态反馈（保存中/已保存/失败）。
- 列表项 = 当前快照项目 ∪ 名单中的历史条目（项目消失后仍可取消排除）；每行显示会话数 / Token 摘要辅助决策。
- 保存成功 → 新名单立即应用于当前页面，并提示"聚合层数字在下次扫描后完全同步"。
- 配置接口不可用（如以 `vinext start` 运行）→ 设置页显示提示，其余功能不受影响。

### 5. 前端防御性过滤（新增 app/lib/exclusions.ts）

`filterDashboard(data, excluded): Dashboard` 纯函数：

- 过滤 issues / sessions / projects 三组明细数据；
- `daily` 与全部 kpis 由过滤后的 issues/sessions **重新聚合**（每日行是多项目聚合值，不可按行剔除），口径与扫描器一致——sessions 按当日去重会话集合计数，时区换算使用 `meta.timezone` 与 Intl；
- 名单为空时原样返回（零开销）。

## 边界情况

| 场景 | 行为 |
|---|---|
| 名单含数据中不存在的项目名 | 设置页仍显示、可移除；统计无影响 |
| 扫描器读取名单失败 | `[warn]` 日志 + 按空名单继续扫描 |
| dashboard.json 尚未生成 | 维持现有错误页不变 |
| 以非 dev 模式运行站点 | GET 失败按空名单处理；设置页提示 |
| 并发保存 | 单人本机场景 + 原子写已足够 |
| 手改名单文件 | 三方（中间件/扫描器/前端轮询）均周期性重读，无需重启 |

## 测试策略

- **Python**（扩展 tests/test_analyzer.py）：
  - 双项目 fixture → 排除其一 → 断言 dashboard.json 五个区块均不含该项目、另一项目数字不变、session 详情文件跳过被排除会话；
  - 空/缺失名单文件输出与现状一致；
  - 取消排除后再次物化，数字完整恢复。
- **Node**（新增 tests/exclusions.test.ts，Node 24 原生 TS 类型剥离运行 node --test）：
  - filterDashboard 的 KPI 重算数值断言、空名单直通；
  - 现有 rendered-html 冒烟保持通过（`npm test` 全绿）。
- **手动验收**：dev 启动 → 设置页排除某项目 → KPI 立即变化 → 手动跑一轮扫描器 → 快照与前端数字一致且快照中无该项目 → 取消排除 → 数字恢复。

## 验收标准

1. `python -m unittest tests.test_analyzer` 通过（含新用例）。
2. `npm test` 通过（含 filterDashboard 单测与既有冒烟）。
3. 手动验收清单全流程走通。
4. 未配置名单文件时，系统行为与本设计落地前完全一致。
