import json
import tempfile
import unittest
from pathlib import Path

from scripts.analyze_sessions import SessionAnalyzer


def event(timestamp, top_type, payload):
    return json.dumps({"timestamp": timestamp, "type": top_type, "payload": payload}, ensure_ascii=False) + "\n"


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions" / "2026" / "08" / "11"
        self.sessions.mkdir(parents=True)
        self.source = self.sessions / "rollout-test.jsonl"
        self.db = self.root / "data" / "test.db"
        self.output = self.root / "public" / "data"
        first_turn = [
            event("2026-08-11T15:59:50Z", "session_meta", {"id": "session-1", "timestamp": "2026-08-11T15:59:50Z", "cwd": "D:\\projects\\demo", "git": {"branch": "main"}}),
            event("2026-08-11T15:59:51Z", "event_msg", {"type": "task_started", "turn_id": "turn-1"}),
            event("2026-08-11T15:59:52Z", "turn_context", {"turn_id": "turn-1", "model": "gpt-test", "effort": "high"}),
            event("2026-08-11T15:59:53Z", "event_msg", {"type": "user_message", "message": "修复导出时报错的 bug"}),
            event("2026-08-11T16:00:00Z", "event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 80, "cached_input_tokens": 20, "output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": 100}}}),
            event("2026-08-11T16:00:01Z", "event_msg", {"type": "agent_message", "phase": "final", "message": "已修复根因并补充测试。"}),
            event("2026-08-11T16:00:02Z", "event_msg", {"type": "task_complete", "turn_id": "turn-1"}),
        ]
        self.source.write_text("".join(first_turn), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

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
            # 无新增事件时 scan 也必须因名单变更而重新物化（watch 模式承诺）。
            analyzer.scan([self.root / "sessions"])
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
            analyzer.scan([self.root / "sessions"])
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
            analyzer.scan([self.root / "sessions"])
            again = json.loads((self.output / "dashboard.json").read_text(encoding="utf-8"))
            self.assertEqual(again["kpis"], baseline["kpis"])
            self.assertEqual(again["projects"], baseline["projects"])
            self.assertEqual(again["issues"], baseline["issues"])
            self.assertEqual(again["sessions"], baseline["sessions"])
        finally:
            analyzer.close()

    def test_incremental_cross_day_and_cost_attribution(self):
        analyzer = SessionAnalyzer(self.db, self.output)
        try:
            first = analyzer.scan([self.root / "sessions"])
            self.assertEqual(first.files_changed, 1)
            first_count = analyzer.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            self.assertEqual(analyzer.conn.execute("SELECT total_tokens FROM issues").fetchone()[0], 100)

            second_turn = [
                event("2026-08-12T02:00:00Z", "event_msg", {"type": "task_started", "turn_id": "turn-2"}),
                event("2026-08-12T02:00:01Z", "event_msg", {"type": "user_message", "message": "可以，继续优化日志"}),
                event("2026-08-12T02:00:02Z", "event_msg", {"type": "agent_message", "phase": "final", "message": "建议将日志改为结构化输出。"}),
                event("2026-08-12T02:00:03Z", "event_msg", {"type": "task_complete", "turn_id": "turn-2"}),
            ]
            with self.source.open("a", encoding="utf-8") as handle:
                handle.write("".join(second_turn))
            second = analyzer.scan([self.root / "sessions"])
            self.assertEqual(second.files_changed, 1)
            self.assertEqual(analyzer.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], first_count + 4)
            issue = analyzer.conn.execute("SELECT acceptance FROM issues WHERE turn_id='turn-1'").fetchone()
            self.assertEqual(issue[0], "accepted")
            dashboard = json.loads((self.output / "dashboard.json").read_text(encoding="utf-8"))
            self.assertEqual(dashboard["kpis"]["cross_day_sessions"], 1)
            self.assertEqual({item["date"] for item in dashboard["daily"]}, {"2026-08-11", "2026-08-12"})

            third = analyzer.scan([self.root / "sessions"])
            self.assertEqual(third.events_added, 0)
            self.assertEqual(analyzer.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], first_count + 4)
        finally:
            analyzer.close()


if __name__ == "__main__":
    unittest.main()
