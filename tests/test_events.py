from __future__ import annotations

import queue
import unittest

from backend import events


class EventsTest(unittest.TestCase):
    def tearDown(self) -> None:
        # 清掉残留订阅,避免影响别的测试。
        import backend.events as ev
        with ev._lock:
            ev._subscribers.clear()

    def test_publish_fans_out_to_subscribers(self) -> None:
        a = events.subscribe()
        b = events.subscribe()
        events.publish({"type": "trade_filled", "symbol": "600519.SH"})
        self.assertEqual(a.get_nowait()["symbol"], "600519.SH")
        self.assertEqual(b.get_nowait()["symbol"], "600519.SH")

    def test_unsubscribe_stops_delivery(self) -> None:
        q = events.subscribe()
        events.unsubscribe(q)
        events.publish({"type": "x"})
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_full_queue_drops_without_blocking(self) -> None:
        q = events.subscribe(maxsize=1)
        events.publish({"n": 1})
        events.publish({"n": 2})  # 队列满 → 丢弃,不抛错/不阻塞
        self.assertEqual(q.get_nowait()["n"], 1)
        self.assertTrue(q.empty())


if __name__ == "__main__":
    unittest.main()
