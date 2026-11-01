"""长期记忆存储的单元测试（数据库落在临时目录，不碰真实 data/）。"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"
sys.path.insert(0, str(_PLUGIN))

import memory_store


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = Path(self._tmp.name) / "memory.db"
        self._orig_db = memory_store.DB_PATH
        self._orig_conn = memory_store._conn
        self._orig_day = memory_store._last_purge_day
        memory_store.DB_PATH = str(self._db)
        memory_store._conn = None
        memory_store._last_purge_day = ""
        self.addCleanup(self._restore)

    def _restore(self):
        memory_store.close_db()
        memory_store.DB_PATH = self._orig_db
        memory_store._conn = self._orig_conn
        memory_store._last_purge_day = self._orig_day
        self._tmp.cleanup()

    # ---- 写入与去重 -------------------------------------------------------

    def test_add_fact_new(self):
        self.assertEqual(memory_store.add_fact(1, "fact", "在读研究生"), "new")
        self.assertEqual(memory_store.count_facts(), 1)

    def test_add_fact_refreshes_duplicate(self):
        memory_store.add_fact(1, "fact", "在读研究生")
        result = memory_store.add_fact(1, "fact", "在读研究生")
        self.assertEqual(result, "refreshed")
        self.assertEqual(memory_store.count_facts(), 1)

        row = memory_store.get_conn().execute(
            "SELECT hits FROM user_facts WHERE user_id = 1"
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_same_content_different_users(self):
        memory_store.add_fact(1, "fact", "在学 Rust")
        memory_store.add_fact(2, "fact", "在学 Rust")
        self.assertEqual(memory_store.count_facts(), 2)

    # ---- 检索 -----------------------------------------------------------

    def test_recall_keyword_scoped(self):
        memory_store.add_fact(1, "profile", "该用户在读研究生")
        memory_store.add_fact(1, "fact", "该用户养了一只猫")
        memory_store.add_fact(2, "fact", "别人家的记忆不该出现")

        # 问题与事实共享词块（研究生）才召回——纯关键词检索的语义边界
        hits = memory_store.recall_facts(1, "他研究生读得怎么样")
        self.assertTrue(any("研究生" in h for h in hits))
        self.assertTrue(all("别人家" not in h for h in hits))
        self.assertTrue(all("猫" not in h for h in hits))

    def test_recall_excludes_recency_only(self):
        """纯近因、无关键词命中的记忆不该被塞进上下文。"""
        memory_store.add_fact(1, "fact", "完全无关的记忆内容xyz")
        self.assertEqual(memory_store.recall_facts(1, "学业问题"), [])

    def test_recall_budget_limits_chars(self):
        for i in range(20):
            memory_store.add_fact(1, "fact", f"关键词项目编号{i:02d}补充信息" * 3)
        hits = memory_store.recall_facts(1, "关键词项目")
        self.assertLessEqual(sum(len(h) for h in hits), memory_store._RECALL_MAX_CHARS)
        self.assertLessEqual(len(hits), memory_store._RECALL_TOP)

    def test_recall_empty_question(self):
        memory_store.add_fact(1, "fact", "该用户在读研究生")
        self.assertEqual(memory_store.recall_facts(1, ""), [])

    # ---- 上限与保留 -------------------------------------------------------

    def _insert_many(self, user_id, n, base_ts=None):
        now = int(time.time())
        conn = memory_store.get_conn()
        for i in range(n):
            ts = base_ts if base_ts is not None else now - i * 3600
            conn.execute(
                "INSERT INTO user_facts (user_id, group_id, kind, content, ts, last_seen) "
                "VALUES (?, NULL, 'fact', ?, ?, ?)",
                (user_id, f"事实{i}", ts, ts),
            )
        conn.commit()

    def test_enforce_max_per_user_deletes_oldest(self):
        self._insert_many(1, 5)
        deleted = memory_store.enforce_max_per_user(1, 3)
        self.assertEqual(deleted, 2)
        remaining = memory_store.get_conn().execute(
            "SELECT content FROM user_facts WHERE user_id = 1 ORDER BY last_seen DESC"
        ).fetchall()
        # 删的是 last_seen 最旧的（i 越大 ts 越旧）
        contents = {row[0] for row in remaining}
        self.assertEqual(len(contents), 3)
        self.assertNotIn("事实4", contents)

    def test_purge_old_facts(self):
        old_ts = int(time.time()) - 200 * 86400
        self._insert_many(1, 3, base_ts=old_ts)
        self._insert_many(1, 2)  # 新记忆

        deleted = memory_store.purge_old_facts(90)
        self.assertEqual(deleted, 3)
        self.assertEqual(memory_store.count_facts(), 2)

    def test_purge_zero_keeps_everything(self):
        self._insert_many(1, 2)
        self.assertEqual(memory_store.purge_old_facts(0), 0)
        self.assertEqual(memory_store.count_facts(), 2)

    def test_lazy_daily_purge_runs_once(self):
        self._insert_many(1, 1)
        first = memory_store.lazy_daily_purge(90)
        second = memory_store.lazy_daily_purge(90)
        # 第二次同一天内应是空操作（即使返回 0 也不该重复删）
        self.assertEqual(first, second)

    def test_forget_user(self):
        self._insert_many(1, 3)
        self._insert_many(2, 2)
        deleted = memory_store.forget_user(1)
        self.assertEqual(deleted, 3)
        self.assertEqual(memory_store.count_facts(), 2)

    def test_db_file_created_with_restricted_mode(self):
        import os
        import stat

        self._insert_many(1, 1)
        if os.name != "nt":
            mode = stat.S_IMODE(Path(self._db).stat().st_mode)
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
