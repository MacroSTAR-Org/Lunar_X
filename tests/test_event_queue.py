"""有界事件队列测试：满时丢最旧事件，监听循环永远不被写阻塞。"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.transport import BoundedEventQueue


class BoundedEventQueueTests(unittest.TestCase):
    def test_drops_oldest_when_full(self):
        async def scenario():
            queue = BoundedEventQueue(maxsize=2)
            for i in range(4):
                await queue.put(i)
            self.assertEqual(queue.qsize(), 2)
            self.assertEqual(queue.dropped, 2)
            self.assertEqual(await queue.get(), 2)
            self.assertEqual(await queue.get(), 3)

        asyncio.run(scenario())

    def test_stop_clears_backlog_and_appends_sentinel(self):
        async def scenario():
            queue = BoundedEventQueue(maxsize=4)
            await queue.put("a")
            await queue.put("b")
            queue.stop()
            self.assertEqual(queue.qsize(), 1)
            self.assertIsNone(await queue.get())

        asyncio.run(scenario())

    def test_maxsize_is_at_least_one(self):
        queue = BoundedEventQueue(maxsize=0)
        self.assertEqual(queue.maxsize, 1)


if __name__ == "__main__":
    unittest.main()
