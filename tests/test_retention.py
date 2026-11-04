"""消息保留策略（数据保留）的单元测试。

群消息属于个人数据，默认不该无限期堆在磁盘上。
这里覆盖：按天数清理、永久保留模式、全量清空、边界条件。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

# 插件目录里的 store.py 用相对路径定位 data/，测试时先把它顶掉，
# 避免污染真实的 data/tools/tools.db
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "tools"
sys.path.insert(0, str(_PLUGIN_DIR))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = Path(self._tmp.name) / "tools.db"

        import store

        self.store = store
        # 把 store 的模块级路径指向临时库
        self._orig_db = store.DB_PATH
        self._orig_conn = store._conn
        store.DB_PATH = str(self._db)
        store._conn = None
        self.addCleanup(self._restore)

    def _restore(self):
        self.store.DB_PATH = self._orig_db
        self.store.close_db()
        self.store._conn = self._orig_conn
        self._tmp.cleanup()

    def _insert(self, age_days: int, text: str = "x") -> None:
        """插入一条 age_days 天前的消息。"""
        self._insert_at(int(time.time()) - age_days * 86400, text)

    def _insert_at(self, ts: int, text: str = "x") -> None:
        """插入一条指定时间戳的消息（边界测试需要秒级精度）。"""
        conn = self.store.get_conn()
        conn.execute(
            "INSERT INTO messages (ts, scene, group_id, user_id, text) VALUES (?,?,?,?,?)",
            (ts, "group", 123, 456, text),
        )
        conn.commit()

    def test_purge_removes_only_expired(self):
        self._insert(10)
        self._insert(40)
        self._insert(100)

        deleted = self.store.purge_old_messages(30)

        self.assertEqual(deleted, 2)
        self.assertEqual(self.store.count_stored_messages(), 1)

    def test_purge_zero_keeps_everything(self):
        """保留天数为 0 表示永久保留，不能当成"删掉全部"。"""
        self._insert(1)
        self._insert(999)

        deleted = self.store.purge_old_messages(0)

        self.assertEqual(deleted, 0)
        self.assertEqual(self.store.count_stored_messages(), 2)

    def test_purge_negative_keeps_everything(self):
        self._insert(500)
        self.assertEqual(self.store.purge_old_messages(-1), 0)
        self.assertEqual(self.store.count_stored_messages(), 1)

    def test_purge_all(self):
        self._insert(1)
        self._insert(2)
        self._insert(3)

        deleted = self.store.purge_all_messages()

        self.assertEqual(deleted, 3)
        self.assertEqual(self.store.count_stored_messages(), 0)

    def test_purge_on_empty_db(self):
        self.assertEqual(self.store.purge_old_messages(30), 0)
        self.assertEqual(self.store.purge_all_messages(), 0)
        self.assertEqual(self.store.count_stored_messages(), 0)

    def test_boundary_exactly_at_cutoff(self):
        """边界：恰好卡在保留天数上的消息要保留（清理条件是严格小于 cutoff）。

        不能真的插一条"正好 30 天前"的消息——插入和清理之间时间在走，
        那一刻的"正好"到清理时就变成"刚好超过"了，测试会随机挂。
        这里以 cutoff 为基准前后各留 5 秒余量，锁定边界行为且不会抖。
        """
        cutoff_base = int(time.time()) - 30 * 86400
        self._insert_at(cutoff_base + 5, "just_inside")  # 略新于边界 → 保留
        self._insert_at(cutoff_base - 5, "just_outside")  # 略旧于边界 → 删除

        deleted = self.store.purge_old_messages(30)

        self.assertEqual(deleted, 1)
        remaining = self.store.search_messages("just_inside", group_id=123)
        self.assertEqual(len(remaining), 1)

    def test_count_is_consistent(self):
        for i in range(5):
            self._insert(i)
        self.assertEqual(self.store.count_stored_messages(), 5)


class SearchSafetyTests(unittest.TestCase):
    """消息检索必须限定在群内，且 LIKE 通配符要转义。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = Path(self._tmp.name) / "tools.db"

        import store

        self.store = store
        self._orig_db = store.DB_PATH
        self._orig_conn = store._conn
        store.DB_PATH = str(self._db)
        store._conn = None
        self.addCleanup(self._restore)

    def _restore(self):
        self.store.DB_PATH = self._orig_db
        self.store.close_db()
        self.store._conn = self._orig_conn
        self._tmp.cleanup()

    def _insert(self, group_id, user_id, text):
        conn = self.store.get_conn()
        conn.execute(
            "INSERT INTO messages (ts, scene, group_id, user_id, text) VALUES (?,?,?,?,?)",
            (int(time.time()), "group", group_id, user_id, text),
        )
        conn.commit()

    def test_search_requires_group(self):
        """不带 group_id 一律返回空，防止跨群/私聊泄露。"""
        self._insert(111, 222, "秘密内容")
        self.assertEqual(self.store.search_messages("秘密"), [])
        self.assertEqual(self.store.count_messages(7), [])

    def test_search_scoped_to_group(self):
        self._insert(111, 222, "关键词在这")
        self._insert(999, 222, "关键词在别的群")

        results = self.store.search_messages("关键词", group_id=111)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "关键词在这")

    def test_like_wildcards_escaped(self):
        """搜索 '%' 不应匹配所有消息。"""
        self._insert(111, 222, "普通消息")
        self._insert(111, 222, "含%号的字面量")

        results = self.store.search_messages("%", group_id=111)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "含%号的字面量")


if __name__ == "__main__":
    unittest.main()
