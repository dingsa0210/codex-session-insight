# Codex Session Insight

面向项目管理人员的 Codex 会话分析系统。后台增量读取本机 `.codex/sessions/**/*.jsonl`，将跨日会话、问题、Bug、方案、采纳判断和成本归档到 SQLite；前台提供只读 Web 看板。

## 已实现

- 会话生命周期：按事件时间而非文件创建日统计；持续监测已有文件的追加内容，正确处理跨日 session。
- 完整证据：保存 user/assistant 对话、turn、token 样本、工具调用、文件变更、工作目录和 Git 元数据。
- 问题台账：每个用户 turn 形成问题单元，抽取类型、标题、方案、推进状态和关联项目；短跟进可链接到上一问题根节点。
- 采纳状态：`accepted` / `rejected` 来自后续用户反馈规则；`implemented_unverified` 只表示检测到实施动作，不冒充业务验收。所有判断都有置信度和依据。
- 成本口径：问题级 input/cache/output/reasoning token、总 token、墙钟耗时、活跃耗时、工具调用和变更文件。
- 管理看板：总览、问题台账、项目、会话生命周期、成本分析；支持搜索和筛选，前台没有写操作。
- 项目统计排除：后台「设置」页可配置不参与统计的项目；名单存 `data/excluded_projects.json`，扫描器与看板共同遵循，证据库不受影响、可随时恢复。

## 快速开始

在 PowerShell 中运行：

```powershell
cd D:\projects\codex-session-insight
powershell -ExecutionPolicy Bypass -File scripts\update-dashboard.ps1
npm run dev
```

打开 `http://localhost:3000`。看板每 60 秒读取一次最新快照。

持续监听模式：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update-dashboard.ps1 -WatchSeconds 60
```

安装 Windows 后台计划任务（默认每 10 分钟）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-scheduled-task.ps1 -IntervalMinutes 10
```

该命令会变更 Windows 计划任务；当前项目不会自动替用户安装。

## 数据流

```text
~/.codex/sessions/**/*.jsonl
          │  文件 size + offset + head hash
          ▼
scripts/analyze_sessions.py
          ├── data/codex_insights.db       证据与分析模型
          └── public/data/dashboard.json   只读 Web 快照
                          │
                          ▼
                 Codex Insight Web UI
```

首次全量扫描取决于历史数据量；当前约 977 MB 的本机数据首扫约 103 秒。之后只读取文件新增字节；日常增量通常在数秒内完成。

## 表与口径

- `source_files`：每个 JSONL 的 offset、size、mtime、头部指纹和当前 turn，用于可靠增量。
- `sessions`：会话首末活动、项目、仓库、分支和 Codex 版本。
- `turns`：单次用户请求到 Codex 完成/中断的成本与结果。
- `events`：紧凑事件索引；原始证据通过 `source_path + byte_offset` 可追溯，避免复制近 1 GB JSONL。
- `messages`：完整 user/assistant 文本。
- `token_samples`：采用 `last_token_usage`，按 turn 累加，避免把 session 累计值重复计算。
- `issues`：面向项目管理的派生问题台账。

墙钟耗时为 `task_started → task_complete/turn_aborted`。活跃耗时将相邻事件间隔最多计入 5 分钟，用于降低离席时间偏差。一个 turn 可能包含多次上下文输入，因此 token 是模型实际处理量，不等价于计费金额；后续可按模型价格表增加货币成本。

## 检查

```powershell
python -m unittest tests.test_analyzer
npm run build
```

分析数据库和会话内容属于敏感本地数据。项目默认只在本机运行，没有部署到公网，也没有上传历史会话。
