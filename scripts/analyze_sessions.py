#!/usr/bin/env python3
"""Incrementally index Codex JSONL sessions and build a read-only PM dashboard.

The source JSONL files remain untouched. SQLite is the evidence store; the web
application consumes materialized JSON snapshots from ``public/data``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
POSITIVE_RE = re.compile(
    r"(^|[，。,.!！\s])(可以|好了|已解决|解决了|确认|通过|没问题|正确|不错|继续|就这样|yes|works?|fixed|approved)([，。,.!！\s]|$)",
    re.I,
)
NEGATIVE_RE = re.compile(
    r"(还没|没有解决|仍然|还是不行|不对|错误|失败|有问题|重做|重新|回滚|doesn.?t work|not fixed|wrong|failed)",
    re.I,
)
BUG_RE = re.compile(r"(bug|错误|异常|失败|崩溃|报错|不工作|不生效|为什么|问题|故障|error|exception|crash|failed)", re.I)
FEATURE_RE = re.compile(r"(新增|添加|开发|实现|创建|支持|接入|build|create|implement|add )", re.I)
OPTIMIZE_RE = re.compile(r"(优化|改进|性能|提速|重构|精简|内存|耗时|optimi[sz]|performance|refactor)", re.I)
ANALYSIS_RE = re.compile(r"(分析|审计|调研|评估|检查|诊断|为什么|review|audit|investigate|analy[sz])", re.I)
FOLLOWUP_RE = re.compile(r"^(继续|再|还|请继续|按这个|就这样|可以|好|不对|不是|重新|修复|完善|补充|然后|yes|ok|retry|continue|fix)\b", re.I)
DONE_RE = re.compile(r"(已完成|已实现|已修复|已解决|测试通过|构建通过|完成了|implemented|fixed|resolved|tests? pass|build succeeded)", re.I)
PROPOSAL_RE = re.compile(r"(建议|方案|可以通过|应当|需要|根因|做法|recommend|solution|approach|root cause)", re.I)
FILE_PATCH_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_day(value: str | None, tz: ZoneInfo) -> str | None:
    parsed = parse_ts(value)
    if not parsed:
        return value[:10] if value else None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).date().isoformat()


def seconds_between(start: str | None, end: str | None) -> float:
    a, b = parse_ts(start), parse_ts(end)
    if not a or not b:
        return 0.0
    return max(0.0, (b - a).total_seconds())


def compact_text(text: str | None, limit: int = 160) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def title_from(text: str | None) -> str:
    clean = compact_text(text, 120)
    clean = re.sub(r"^/(goal|plan)\s*", "", clean, flags=re.I)
    return clean or "未命名任务"


def project_name(cwd: str | None, repo_url: str | None) -> str:
    if repo_url:
        name = repo_url.rstrip("/").rsplit("/", 1)[-1]
        return re.sub(r"\.git$", "", name) or "未关联项目"
    if not cwd:
        return "未关联项目"
    if re.match(r"^[A-Za-z]:\\", cwd):
        parts = PureWindowsPath(cwd).parts
    else:
        parts = Path(cwd).parts
    lowered = [part.lower() for part in parts]
    if "projects" in lowered:
        index = lowered.index("projects")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1] if parts else "未关联项目"


def issue_type(text: str) -> str:
    if BUG_RE.search(text):
        return "bug"
    if OPTIMIZE_RE.search(text):
        return "optimization"
    if FEATURE_RE.search(text):
        return "feature"
    if ANALYSIS_RE.search(text):
        return "analysis"
    return "task"


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def event_evidence(payload: dict[str, Any], payload_type: str) -> str:
    """Keep a compact event summary; raw evidence is addressed by file+offset."""
    keys_by_type = {
        "task_started": {"turn_id", "started_at", "model_context_window"},
        "task_complete": {"turn_id", "last_agent_message"},
        "turn_aborted": {"turn_id", "reason"},
        "token_count": set(),
        "patch_apply_end": {"call_id", "success", "changes"},
    }
    allowed = keys_by_type.get(payload_type, {"turn_id", "call_id", "name", "role", "phase", "status"})
    summary = {key: value for key, value in payload.items() if key in allowed}
    text = json_dump(summary)
    return text[:12000]


def payload_turn_id(payload: dict[str, Any]) -> str | None:
    direct = payload.get("turn_id")
    if direct:
        return str(direct)
    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
    if isinstance(metadata, dict) and metadata.get("turn_id"):
        return str(metadata["turn_id"])
    return None


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    output: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "output_text", "text"} and item.get("text"):
            output.append(str(item["text"]))
    return "\n".join(output)


@dataclass
class ScanResult:
    files_seen: int = 0
    files_changed: int = 0
    events_added: int = 0
    bytes_read: int = 0
    errors: int = 0
    changed_turns: set[str] = field(default_factory=set)
    changed_sessions: set[str] = field(default_factory=set)


class SessionAnalyzer:
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

    def close(self) -> None:
        self.conn.close()

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

    def _exclude_clause(self) -> tuple[str, list[str]]:
        excluded = sorted(self.excluded_projects)
        if not excluded:
            return "", []
        placeholders = ",".join("?" for _ in excluded)
        return f" AND project NOT IN ({placeholders})", excluded

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_files (
              path TEXT PRIMARY KEY,
              size INTEGER NOT NULL DEFAULT 0,
              mtime_ns INTEGER NOT NULL DEFAULT 0,
              offset INTEGER NOT NULL DEFAULT 0,
              head_hash TEXT,
              session_id TEXT,
              current_turn_id TEXT,
              last_scan_at TEXT,
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY,
              source_path TEXT,
              created_at TEXT,
              first_event_at TEXT,
              last_event_at TEXT,
              cwd TEXT,
              project TEXT,
              repository_url TEXT,
              git_branch TEXT,
              git_commit TEXT,
              originator TEXT,
              cli_version TEXT,
              model_provider TEXT,
              title TEXT,
              updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS turns (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
              started_at TEXT,
              ended_at TEXT,
              status TEXT NOT NULL DEFAULT 'running',
              model TEXT,
              effort TEXT,
              user_prompt TEXT,
              final_answer TEXT,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              cached_input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              reasoning_tokens INTEGER NOT NULL DEFAULT 0,
              total_tokens INTEGER NOT NULL DEFAULT 0,
              wall_seconds REAL NOT NULL DEFAULT 0,
              active_seconds REAL NOT NULL DEFAULT 0,
              tool_calls INTEGER NOT NULL DEFAULT 0,
              patch_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_key TEXT NOT NULL UNIQUE,
              source_path TEXT NOT NULL,
              byte_offset INTEGER NOT NULL,
              session_id TEXT,
              turn_id TEXT,
              timestamp TEXT,
              top_type TEXT NOT NULL,
              payload_type TEXT,
              payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_session_time_idx ON events(session_id, timestamp);
            CREATE INDEX IF NOT EXISTS events_turn_time_idx ON events(turn_id, timestamp);
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_key TEXT NOT NULL UNIQUE,
              session_id TEXT NOT NULL,
              turn_id TEXT,
              timestamp TEXT,
              role TEXT NOT NULL,
              phase TEXT,
              content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_session_time_idx ON messages(session_id, timestamp);
            CREATE TABLE IF NOT EXISTS token_samples (
              event_key TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              turn_id TEXT,
              timestamp TEXT,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              cached_input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              reasoning_tokens INTEGER NOT NULL DEFAULT 0,
              total_tokens INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tools (
              call_id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              turn_id TEXT,
              timestamp TEXT,
              name TEXT,
              input_text TEXT,
              output_text TEXT,
              status TEXT,
              changed_files TEXT
            );
            CREATE TABLE IF NOT EXISTS issues (
              id TEXT PRIMARY KEY,
              root_id TEXT NOT NULL,
              parent_id TEXT,
              session_id TEXT NOT NULL,
              turn_id TEXT NOT NULL,
              project TEXT NOT NULL,
              created_at TEXT,
              updated_at TEXT,
              local_date TEXT,
              type TEXT NOT NULL,
              title TEXT NOT NULL,
              prompt TEXT NOT NULL,
              solution TEXT,
              status TEXT NOT NULL,
              acceptance TEXT NOT NULL,
              acceptance_confidence REAL NOT NULL DEFAULT 0,
              evidence TEXT,
              total_tokens INTEGER NOT NULL DEFAULT 0,
              wall_seconds REAL NOT NULL DEFAULT 0,
              active_seconds REAL NOT NULL DEFAULT 0,
              tool_calls INTEGER NOT NULL DEFAULT 0,
              changed_files TEXT
            );
            CREATE INDEX IF NOT EXISTS issues_date_idx ON issues(local_date, updated_at);
            CREATE TABLE IF NOT EXISTS scan_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              source_root TEXT,
              files_seen INTEGER NOT NULL DEFAULT 0,
              files_changed INTEGER NOT NULL DEFAULT 0,
              events_added INTEGER NOT NULL DEFAULT 0,
              bytes_read INTEGER NOT NULL DEFAULT 0,
              errors INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self.conn.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def scan(self, source_roots: Iterable[Path], full: bool = False) -> ScanResult:
        started = utc_now()
        root_label = os.pathsep.join(str(p) for p in source_roots)
        cursor = self.conn.execute(
            "INSERT INTO scan_runs(started_at, source_root) VALUES(?, ?)", (started, root_label)
        )
        run_id = cursor.lastrowid
        self.excluded_projects = self._load_excluded()
        result = ScanResult()
        paths: list[Path] = []
        for root in source_roots:
            if root.exists():
                paths.extend(root.rglob("*.jsonl"))
        known_rows = {
            row["path"]: row
            for row in self.conn.execute("SELECT path,size,offset,mtime_ns FROM source_files").fetchall()
        }
        for path in sorted(set(paths)):
            result.files_seen += 1
            try:
                key = str(path.resolve())
                stat = path.stat()
                known = known_rows.get(key)
                if known and not full and stat.st_size == known["offset"]:
                    if stat.st_mtime_ns != known["mtime_ns"]:
                        self.conn.execute(
                            "UPDATE source_files SET size=?,mtime_ns=?,last_scan_at=?,error=NULL WHERE path=?",
                            (stat.st_size, stat.st_mtime_ns, utc_now(), key),
                        )
                    continue
                changed, added, read_bytes, turn_ids, session_ids = self._scan_file(path, full=full)
                result.files_changed += int(changed)
                result.events_added += added
                result.bytes_read += read_bytes
                result.changed_turns.update(turn_ids)
                result.changed_sessions.update(session_ids)
            except Exception as exc:  # keep the monitor progressing if one live file is transiently locked
                result.errors += 1
                self.conn.execute(
                    "INSERT INTO source_files(path, error, last_scan_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET error=excluded.error, last_scan_at=excluded.last_scan_at",
                    (str(path), f"{type(exc).__name__}: {exc}", utc_now()),
                )
                print(f"[warn] {path}: {exc}", file=sys.stderr)
        # Live sessions append token/tool chatter continuously. Rebuilding every
        # model for each tiny append is expensive, so only changed turns are
        # recomputed and the dashboard snapshot is refreshed from materialized
        # rows.
        needs_rebuild = bool(result.events_added or full or not (self.output_dir / "dashboard.json").exists())
        if needs_rebuild:
            changed_turns = sorted(result.changed_turns) if result.events_added and not full else None
            self._rebuild_models(changed_turns)
            self.materialize_dashboard(write_session_ids=result.changed_sessions if changed_turns is not None else None)
        finished = utc_now()
        self.conn.execute(
            """UPDATE scan_runs SET finished_at=?, files_seen=?, files_changed=?, events_added=?,
               bytes_read=?, errors=? WHERE id=?""",
            (finished, result.files_seen, result.files_changed, result.events_added, result.bytes_read, result.errors, run_id),
        )
        self.conn.commit()
        return result

    def _scan_file(self, path: Path, full: bool = False) -> tuple[bool, int, int, set[str], set[str]]:
        stat = path.stat()
        key = str(path.resolve())
        row = self.conn.execute("SELECT * FROM source_files WHERE path=?", (key,)).fetchone()
        offset = int(row["offset"]) if row and not full else 0
        current_turn = row["current_turn_id"] if row and offset else None
        session_id = row["session_id"] if row and offset else None
        if row and not full and stat.st_size == offset:
            if stat.st_mtime_ns != row["mtime_ns"]:
                self.conn.execute(
                    "UPDATE source_files SET size=?,mtime_ns=?,last_scan_at=?,error=NULL WHERE path=?",
                    (stat.st_size, stat.st_mtime_ns, utc_now(), key),
                )
            return False, 0, 0, set(), set()
        with path.open("rb") as handle:
            head_hash = hashlib.sha256(handle.read(4096)).hexdigest()
        replaced = bool(row and (stat.st_size < offset or (row["head_hash"] and row["head_hash"] != head_hash)))
        if replaced or full:
            self._delete_source_events(key)
            offset, current_turn, session_id = 0, None, None
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read()
        complete_length = len(raw)
        if raw and not raw.endswith((b"\n", b"\r")):
            last_newline = max(raw.rfind(b"\n"), raw.rfind(b"\r"))
            complete_length = last_newline + 1 if last_newline >= 0 else 0
        complete = raw[:complete_length]
        added = 0
        changed_turns: set[str] = set()
        changed_sessions: set[str] = set()
        byte_position = offset
        for raw_line in complete.splitlines(keepends=True):
            line_start = byte_position
            byte_position += len(raw_line)
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            top_type = str(obj.get("type") or "unknown")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            payload_type = str(payload.get("type") or "")
            if top_type == "session_meta":
                session_id = str(payload.get("id") or payload.get("session_id") or session_id or "")
            event_turn = payload_turn_id(payload) or current_turn
            if top_type == "event_msg" and payload_type == "task_started":
                current_turn = str(payload.get("turn_id") or current_turn or "") or None
                event_turn = current_turn
            if top_type == "event_msg" and payload_type in {"task_complete", "turn_aborted"}:
                event_turn = str(payload.get("turn_id") or current_turn or "") or None
            event_key = hashlib.sha256((key + "\x1f" + str(line_start)).encode("utf-8")).hexdigest()
            inserted = self.conn.execute(
                """INSERT OR IGNORE INTO events(event_key, source_path, byte_offset, session_id, turn_id,
                   timestamp, top_type, payload_type, payload_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (event_key, key, line_start, session_id, event_turn, obj.get("timestamp"), top_type, payload_type, event_evidence(payload, payload_type)),
            ).rowcount
            if inserted:
                added += 1
                if event_turn:
                    changed_turns.add(event_turn)
                if session_id:
                    changed_sessions.add(session_id)
                self._index_event(event_key, key, session_id, event_turn, obj.get("timestamp"), top_type, payload_type, payload)
            if top_type == "event_msg" and payload_type in {"task_complete", "turn_aborted"}:
                current_turn = None

        new_offset = offset + complete_length
        self.conn.execute(
            """INSERT INTO source_files(path,size,mtime_ns,offset,head_hash,session_id,current_turn_id,last_scan_at,error)
               VALUES(?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
               offset=excluded.offset,head_hash=excluded.head_hash,session_id=excluded.session_id,
               current_turn_id=excluded.current_turn_id,last_scan_at=excluded.last_scan_at,error=NULL""",
            (key, stat.st_size, stat.st_mtime_ns, new_offset, head_hash, session_id, current_turn, utc_now()),
        )
        return True, added, complete_length, changed_turns, changed_sessions

    def _delete_source_events(self, source_path: str) -> None:
        keys = [r[0] for r in self.conn.execute("SELECT event_key FROM events WHERE source_path=?", (source_path,))]
        if keys:
            placeholders = ",".join("?" for _ in keys)
            self.conn.execute(f"DELETE FROM messages WHERE event_key IN ({placeholders})", keys)
            self.conn.execute(f"DELETE FROM token_samples WHERE event_key IN ({placeholders})", keys)
        self.conn.execute("DELETE FROM events WHERE source_path=?", (source_path,))

    def _ensure_session(self, session_id: str | None, source_path: str) -> None:
        if not session_id:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions(id, source_path, project, updated_at) VALUES(?,?,?,?)",
            (session_id, source_path, "未关联项目", utc_now()),
        )

    def _index_event(
        self,
        event_key: str,
        source_path: str,
        session_id: str | None,
        turn_id: str | None,
        timestamp: str | None,
        top_type: str,
        payload_type: str,
        payload: dict[str, Any],
    ) -> None:
        if top_type == "session_meta":
            session_id = str(payload.get("id") or payload.get("session_id") or session_id or "")
            if not session_id:
                return
            git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
            cwd = payload.get("cwd")
            repo = git.get("repository_url")
            self.conn.execute(
                """INSERT INTO sessions(id,source_path,created_at,first_event_at,last_event_at,cwd,project,
                   repository_url,git_branch,git_commit,originator,cli_version,model_provider,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET source_path=excluded.source_path,
                   created_at=COALESCE(sessions.created_at,excluded.created_at),cwd=COALESCE(excluded.cwd,sessions.cwd),
                   project=excluded.project,repository_url=COALESCE(excluded.repository_url,sessions.repository_url),
                   git_branch=COALESCE(excluded.git_branch,sessions.git_branch),git_commit=COALESCE(excluded.git_commit,sessions.git_commit),
                   originator=COALESCE(excluded.originator,sessions.originator),cli_version=COALESCE(excluded.cli_version,sessions.cli_version),
                   model_provider=COALESCE(excluded.model_provider,sessions.model_provider),updated_at=excluded.updated_at""",
                (
                    session_id, source_path, payload.get("timestamp") or timestamp, timestamp, timestamp, cwd,
                    project_name(cwd, repo), repo, git.get("branch"), git.get("commit_hash"), payload.get("originator"),
                    payload.get("cli_version"), payload.get("model_provider"), utc_now(),
                ),
            )
            return
        self._ensure_session(session_id, source_path)
        if not session_id:
            return
        self.conn.execute(
            """UPDATE sessions SET first_event_at=CASE WHEN first_event_at IS NULL OR first_event_at>? THEN ? ELSE first_event_at END,
               last_event_at=CASE WHEN last_event_at IS NULL OR last_event_at<? THEN ? ELSE last_event_at END, updated_at=? WHERE id=?""",
            (timestamp, timestamp, timestamp, timestamp, utc_now(), session_id),
        )
        if top_type == "turn_context":
            turn_id = str(payload.get("turn_id") or turn_id or "") or None
            if turn_id:
                self.conn.execute(
                    """INSERT INTO turns(id,session_id,model,effort) VALUES(?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET model=COALESCE(excluded.model,turns.model), effort=COALESCE(excluded.effort,turns.effort)""",
                    (turn_id, session_id, payload.get("model"), payload.get("effort")),
                )
            return
        if top_type == "event_msg":
            if payload_type == "task_started" and turn_id:
                self.conn.execute(
                    """INSERT INTO turns(id,session_id,started_at,status) VALUES(?,?,?,'running')
                       ON CONFLICT(id) DO UPDATE SET started_at=COALESCE(turns.started_at,excluded.started_at), status='running'""",
                    (turn_id, session_id, timestamp),
                )
            elif payload_type in {"task_complete", "turn_aborted"} and turn_id:
                status = "completed" if payload_type == "task_complete" else "aborted"
                self.conn.execute(
                    """INSERT INTO turns(id,session_id,ended_at,status) VALUES(?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at,status=excluded.status""",
                    (turn_id, session_id, timestamp, status),
                )
            elif payload_type in {"user_message", "agent_message"}:
                role = "user" if payload_type == "user_message" else "assistant"
                content = str(payload.get("message") or "")
                self.conn.execute(
                    "INSERT OR IGNORE INTO messages(event_key,session_id,turn_id,timestamp,role,phase,content) VALUES(?,?,?,?,?,?,?)",
                    (event_key, session_id, turn_id, timestamp, role, payload.get("phase"), content),
                )
            elif payload_type == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                self.conn.execute(
                    """INSERT OR IGNORE INTO token_samples(event_key,session_id,turn_id,timestamp,input_tokens,cached_input_tokens,
                       output_tokens,reasoning_tokens,total_tokens) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        event_key, session_id, turn_id, timestamp, int(usage.get("input_tokens") or 0),
                        int(usage.get("cached_input_tokens") or 0), int(usage.get("output_tokens") or 0),
                        int(usage.get("reasoning_output_tokens") or 0), int(usage.get("total_tokens") or 0),
                    ),
                )
            elif payload_type == "patch_apply_end" and turn_id:
                self.conn.execute("UPDATE turns SET patch_count=patch_count+1 WHERE id=?", (turn_id,))
            return
        if top_type == "response_item":
            if payload_type == "message" and payload.get("role") in {"user", "assistant"}:
                # event_msg is the canonical user/assistant stream; response_item is only a fallback.
                return
            if payload_type in {"function_call", "custom_tool_call"}:
                call_id = str(payload.get("call_id") or payload.get("id") or event_key)
                raw_input = payload.get("arguments") if "arguments" in payload else payload.get("input")
                input_text = raw_input if isinstance(raw_input, str) else json_dump(raw_input)
                input_text = (input_text or "")[:20000]
                files = FILE_PATCH_RE.findall(input_text or "")
                self.conn.execute(
                    """INSERT INTO tools(call_id,session_id,turn_id,timestamp,name,input_text,status,changed_files)
                       VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET
                       turn_id=COALESCE(excluded.turn_id,tools.turn_id),name=COALESCE(excluded.name,tools.name),
                       input_text=COALESCE(excluded.input_text,tools.input_text),status=COALESCE(excluded.status,tools.status),
                       changed_files=COALESCE(excluded.changed_files,tools.changed_files)""",
                    (call_id, session_id, turn_id, timestamp, payload.get("name"), input_text, payload.get("status") or "called", json_dump(files)),
                )
            elif payload_type in {"function_call_output", "custom_tool_call_output"}:
                call_id = str(payload.get("call_id") or "")
                if call_id:
                    output = payload.get("output")
                    output_text = output if isinstance(output, str) else json_dump(output)
                    output_text = (output_text or "")[:8000]
                    self.conn.execute(
                        "UPDATE tools SET output_text=?,status='completed' WHERE call_id=?", (output_text, call_id)
                    )

    def _rebuild_models(self, changed_turns: list[str] | None = None) -> None:
        # Canonical prompts and final answers per turn.
        turn_filter = ""
        turn_params: tuple[Any, ...] = ()
        if changed_turns:
            turn_filter = " AND turn_id IN (" + ",".join("?" for _ in changed_turns) + ")"
            turn_params = tuple(changed_turns)
        messages_by_turn: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in self.conn.execute("SELECT turn_id,content,timestamp,role,phase FROM messages WHERE turn_id IS NOT NULL" + turn_filter + " ORDER BY timestamp", turn_params):
            messages_by_turn[row["turn_id"]].append(row)
        tokens_by_turn = {
            row["turn_id"]: row for row in self.conn.execute(
                """SELECT turn_id,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(cached_input_tokens),0) cached,
                COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(reasoning_tokens),0) reasoning,
                COALESCE(SUM(total_tokens),0) total FROM token_samples WHERE turn_id IS NOT NULL""" + turn_filter + " GROUP BY turn_id",
                turn_params,
            )
        }
        tools_by_turn = {row["turn_id"]: row["c"] for row in self.conn.execute("SELECT turn_id,COUNT(*) c FROM tools WHERE turn_id IS NOT NULL" + turn_filter + " GROUP BY turn_id", turn_params)}
        times_by_turn: dict[str, list[str]] = defaultdict(list)
        for row in self.conn.execute("SELECT turn_id,timestamp FROM events WHERE turn_id IS NOT NULL AND timestamp IS NOT NULL" + turn_filter + " ORDER BY turn_id,timestamp", turn_params):
            times_by_turn[row["turn_id"]].append(row["timestamp"])
        empty_tokens = {"input_tokens": 0, "cached": 0, "output_tokens": 0, "reasoning": 0, "total": 0}
        turn_query = "SELECT id,session_id,started_at,ended_at,status FROM turns"
        params: tuple[Any, ...] = ()
        if changed_turns:
            turn_query += " WHERE id IN (" + ",".join("?" for _ in changed_turns) + ")"
            params = tuple(changed_turns)
        for turn in self.conn.execute(turn_query, params).fetchall():
            turn_messages = messages_by_turn.get(turn["id"], [])
            users = [row for row in turn_messages if row["role"] == "user"]
            assistants = [row for row in turn_messages if row["role"] == "assistant"]
            user = users[0] if users else None
            finals = [row for row in assistants if row["phase"] == "final"]
            final = finals[-1] if finals else (assistants[-1] if assistants else None)
            tokens = tokens_by_turn.get(turn["id"], empty_tokens)
            event_times = times_by_turn.get(turn["id"], [])
            active = 0.0
            for left, right in zip(event_times, event_times[1:]):
                gap = seconds_between(left, right)
                active += min(gap, 300.0)
            started_at = turn["started_at"] or (user["timestamp"] if user else None)
            ended_at = turn["ended_at"] or (final["timestamp"] if final else None)
            self.conn.execute(
                """UPDATE turns SET user_prompt=?,final_answer=?,input_tokens=?,cached_input_tokens=?,output_tokens=?,
                   reasoning_tokens=?,total_tokens=?,wall_seconds=?,active_seconds=?,tool_calls=? WHERE id=?""",
                (
                    user["content"] if user else None, final["content"] if final else None, tokens["input_tokens"], tokens["cached"],
                    tokens["output_tokens"], tokens["reasoning"], tokens["total"], seconds_between(started_at, ended_at), active,
                    tools_by_turn.get(turn["id"], 0), turn["id"],
                ),
            )
        self._rebuild_session_titles()
        self._rebuild_issues(set(changed_turns) if changed_turns else None)
        self.conn.commit()

    def _rebuild_session_titles(self) -> None:
        sessions = self.conn.execute("SELECT id FROM sessions").fetchall()
        for session in sessions:
            row = self.conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY timestamp LIMIT 1", (session["id"],)
            ).fetchone()
            if row:
                self.conn.execute("UPDATE sessions SET title=? WHERE id=?", (title_from(row["content"]), session["id"]))

    def _rebuild_issues(self, changed_turns: set[str] | None = None) -> None:
        if changed_turns:
            session_ids = {
                row[0]
                for row in self.conn.execute(
                    "SELECT DISTINCT session_id FROM turns WHERE id IN (" + ",".join("?" for _ in changed_turns) + ")",
                    tuple(changed_turns),
                )
            }
            if not session_ids:
                return
            self.conn.execute("DELETE FROM issues WHERE session_id IN (" + ",".join("?" for _ in session_ids) + ")", tuple(session_ids))
            sessions = self.conn.execute("SELECT id,project FROM sessions WHERE id IN (" + ",".join("?" for _ in session_ids) + ")", tuple(session_ids)).fetchall()
        else:
            self.conn.execute("DELETE FROM issues")
            sessions = self.conn.execute("SELECT id,project FROM sessions").fetchall()
        for session in sessions:
            turns = self.conn.execute(
                """SELECT * FROM turns WHERE session_id=? AND user_prompt IS NOT NULL
                   ORDER BY COALESCE(started_at,ended_at),id""",
                (session["id"],),
            ).fetchall()
            previous_issue: dict[str, Any] | None = None
            for index, turn in enumerate(turns):
                prompt = turn["user_prompt"] or ""
                if not prompt.strip():
                    continue
                issue_id = "ISS-" + hashlib.sha1((session["id"] + turn["id"]).encode()).hexdigest()[:8].upper()
                follows = bool(previous_issue and len(compact_text(prompt, 500)) < 180 and FOLLOWUP_RE.search(prompt.strip()))
                parent_id = previous_issue["id"] if follows and previous_issue else None
                root_id = previous_issue["root_id"] if follows and previous_issue else issue_id
                next_prompt = turns[index + 1]["user_prompt"] if index + 1 < len(turns) else ""
                solution = turn["final_answer"] or ""
                changed_rows = self.conn.execute("SELECT changed_files FROM tools WHERE turn_id=?", (turn["id"],)).fetchall()
                changed_files: list[str] = []
                for changed_row in changed_rows:
                    try:
                        changed_files.extend(json.loads(changed_row["changed_files"] or "[]"))
                    except json.JSONDecodeError:
                        pass
                changed_files = sorted(set(changed_files))
                acceptance, confidence, evidence = "pending", 0.25, "尚未检测到后续用户验收语句"
                if next_prompt and NEGATIVE_RE.search(next_prompt):
                    acceptance, confidence, evidence = "rejected", 0.78, "后续用户消息包含否定或重试信号"
                elif next_prompt and POSITIVE_RE.search(next_prompt):
                    acceptance, confidence, evidence = "accepted", 0.74, "后续用户消息包含确认或继续信号"
                elif changed_files or turn["patch_count"]:
                    acceptance, confidence, evidence = "implemented_unverified", 0.66, "检测到代码修改，但没有独立业务验收"
                elif solution and PROPOSAL_RE.search(solution):
                    acceptance, confidence, evidence = "proposed", 0.55, "Codex 提供了方案，等待执行或验收"
                status = "open"
                if turn["status"] == "aborted":
                    status = "blocked"
                elif acceptance == "rejected":
                    status = "needs_followup"
                elif acceptance in {"accepted", "implemented_unverified"} or DONE_RE.search(solution):
                    status = "resolved" if acceptance == "accepted" else "verification"
                elif solution:
                    status = "proposed"
                created = turn["started_at"]
                updated = turn["ended_at"] or created
                self.conn.execute(
                    """INSERT INTO issues(id,root_id,parent_id,session_id,turn_id,project,created_at,updated_at,local_date,
                       type,title,prompt,solution,status,acceptance,acceptance_confidence,evidence,total_tokens,wall_seconds,
                       active_seconds,tool_calls,changed_files) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        issue_id, root_id, parent_id, session["id"], turn["id"], session["project"] or "未关联项目", created,
                        updated, local_day(created, self.tz), issue_type(prompt), title_from(prompt), prompt, solution, status,
                        acceptance, confidence, evidence, turn["total_tokens"], turn["wall_seconds"], turn["active_seconds"],
                        turn["tool_calls"], json_dump(changed_files),
                    ),
                )
                previous_issue = {"id": issue_id, "root_id": root_id}

    def materialize_dashboard(self, write_session_ids: set[str] | None = None) -> dict[str, Any]:
        clause, params = self._exclude_clause()
        issues = [dict(row) for row in self.conn.execute("SELECT * FROM issues WHERE 1=1" + clause + " ORDER BY updated_at DESC", params).fetchall()]
        sessions = [dict(row) for row in self.conn.execute("SELECT * FROM sessions WHERE 1=1" + clause + " ORDER BY last_event_at DESC", params).fetchall()]
        for issue in issues:
            issue["changed_files"] = json.loads(issue.get("changed_files") or "[]")
            issue["solution_preview"] = compact_text(issue.get("solution"), 260)
            issue["prompt_preview"] = compact_text(issue.get("prompt"), 220)
            # Full evidence remains in SQLite/session detail JSON; keep the dashboard light.
            issue.pop("solution", None)
            issue.pop("prompt", None)
        today = datetime.now(self.tz).date().isoformat()
        day_rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"sessions": set(), "issues": 0, "tokens": 0, "wall_seconds": 0.0, "accepted": 0})
        for row in self.conn.execute("SELECT local_date,session_id,total_tokens,wall_seconds,acceptance FROM issues WHERE local_date IS NOT NULL" + clause, params):
            day = day_rows[row["local_date"]]
            day["sessions"].add(row["session_id"])
            day["issues"] += 1
            day["tokens"] += row["total_tokens"]
            day["wall_seconds"] += row["wall_seconds"]
            day["accepted"] += int(row["acceptance"] == "accepted")
        daily = []
        for date in sorted(day_rows):
            item = day_rows[date]
            daily.append({"date": date, "sessions": len(item["sessions"]), "issues": item["issues"], "tokens": item["tokens"], "wall_seconds": round(item["wall_seconds"], 1), "accepted": item["accepted"]})
        project_rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"sessions": set(), "issues": 0, "bugs": 0, "resolved": 0, "accepted": 0, "tokens": 0, "wall_seconds": 0.0, "last_activity": None})
        for row in self.conn.execute("SELECT project,session_id,type,status,acceptance,total_tokens,wall_seconds,updated_at FROM issues WHERE 1=1" + clause, params):
            project = project_rows[row["project"]]
            project["sessions"].add(row["session_id"])
            project["issues"] += 1
            project["bugs"] += int(row["type"] == "bug")
            project["resolved"] += int(row["status"] in {"resolved", "verification"})
            project["accepted"] += int(row["acceptance"] == "accepted")
            project["tokens"] += row["total_tokens"]
            project["wall_seconds"] += row["wall_seconds"]
            if not project["last_activity"] or (row["updated_at"] or "") > project["last_activity"]:
                project["last_activity"] = row["updated_at"]
        projects = []
        for name, item in project_rows.items():
            projects.append({"name": name, **{k: (len(v) if k == "sessions" else round(v, 1) if isinstance(v, float) else v) for k, v in item.items()}})
        projects.sort(key=lambda p: p["tokens"], reverse=True)
        accepted = sum(1 for i in issues if i["acceptance"] == "accepted")
        decided = sum(1 for i in issues if i["acceptance"] in {"accepted", "rejected"})
        dashboard = {
            "meta": {
                "generated_at": utc_now(),
                "timezone": str(self.tz),
                "schema_version": SCHEMA_VERSION,
                "analysis_method": "rules-v1 / evidence-backed inference",
                "acceptance_note": "accepted/rejected 为基于后续用户消息的推断；implemented_unverified 表示已修改但未独立验收。",
            },
            "kpis": {
                "sessions": len(sessions),
                "today_sessions": len({i["session_id"] for i in issues if i["local_date"] == today}),
                "issues": len(issues),
                "open_issues": sum(1 for i in issues if i["status"] in {"open", "proposed", "needs_followup", "blocked"}),
                "bugs": sum(1 for i in issues if i["type"] == "bug"),
                "total_tokens": sum(int(i["total_tokens"] or 0) for i in issues),
                "wall_seconds": round(sum(float(i["wall_seconds"] or 0) for i in issues), 1),
                "accepted": accepted,
                "acceptance_rate": round(accepted / decided * 100, 1) if decided else 0,
                "cross_day_sessions": sum(1 for s in sessions if local_day(s.get("first_event_at"), self.tz) != local_day(s.get("last_event_at"), self.tz)),
            },
            "daily": daily,
            "projects": projects,
            "issues": issues,
            "sessions": [
                {
                    **session,
                    "created_day": local_day(session.get("created_at"), self.tz),
                    "last_day": local_day(session.get("last_event_at"), self.tz),
                    "spans_days": local_day(session.get("first_event_at"), self.tz) != local_day(session.get("last_event_at"), self.tz),
                    "issue_count": self.conn.execute("SELECT COUNT(*) FROM issues WHERE session_id=?", (session["id"],)).fetchone()[0],
                    "tokens": self.conn.execute("SELECT COALESCE(SUM(total_tokens),0) FROM issues WHERE session_id=?", (session["id"],)).fetchone()[0],
                }
                for session in sessions
            ],
        }
        dashboard_path = self.output_dir / "dashboard.json"
        dashboard_path.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding="utf-8")
        session_dir = self.output_dir / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        if self.excluded_projects:
            # 名单变更后移除历史遗留的详情文件，避免被排除会话继续通过静态目录暴露。
            placeholders = ",".join("?" for _ in self.excluded_projects)
            excluded_ids = [row["id"] for row in self.conn.execute(
                f"SELECT id FROM sessions WHERE project IN ({placeholders})", sorted(self.excluded_projects))]
            for session_id in excluded_ids:
                (session_dir / f"{session_id}.json").unlink(missing_ok=True)
        for session in sessions:
            if write_session_ids is None or session["id"] in write_session_ids:
                messages = [dict(r) for r in self.conn.execute("SELECT timestamp,turn_id,role,phase,content FROM messages WHERE session_id=? ORDER BY timestamp", (session["id"],))]
                detail = {"session": session, "messages": messages}
                (session_dir / f"{session['id']}.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        return dashboard


def default_sessions_dir() -> Path:
    override = os.environ.get("CODEX_SESSIONS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex" / "sessions"


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-dir", action="append", type=Path, default=None, help="Codex sessions root; repeatable")
    parser.add_argument("--db", type=Path, default=project_root / "data" / "codex_insights.db")
    parser.add_argument("--output", type=Path, default=project_root / "public" / "data")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--full", action="store_true", help="Re-index every source file")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Keep scanning at this interval")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    roots = args.sessions_dir or [default_sessions_dir()]
    analyzer = SessionAnalyzer(args.db, args.output, args.timezone)
    try:
        while True:
            started = time.monotonic()
            result = analyzer.scan(roots, full=args.full)
            elapsed = time.monotonic() - started
            print(
                f"scan complete: files={result.files_seen}, changed={result.files_changed}, "
                f"events+={result.events_added}, bytes={result.bytes_read}, errors={result.errors}, {elapsed:.2f}s"
            )
            args.full = False
            if not args.watch:
                break
            time.sleep(max(5, args.watch))
    except KeyboardInterrupt:
        return 130
    finally:
        analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
