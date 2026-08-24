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
