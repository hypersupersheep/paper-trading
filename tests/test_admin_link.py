from __future__ import annotations

import os
import tempfile
import unittest


class AdminLinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_home = os.environ.get("PAPER_TRADING_HOME")
        os.environ["PAPER_TRADING_HOME"] = self.tmp.name
        from backend import admin_link
        self.admin_link = admin_link

    def tearDown(self) -> None:
        if self._saved_home is None:
            os.environ.pop("PAPER_TRADING_HOME", None)
        else:
            os.environ["PAPER_TRADING_HOME"] = self._saved_home
        self.tmp.cleanup()

    def test_disabled_until_admin_url_set(self) -> None:
        self.assertFalse(self.admin_link.is_enabled())
        self.admin_link.save({"admin_url": "http://192.168.1.5:9000"})
        self.assertTrue(self.admin_link.is_enabled())

    def test_node_id_is_stable(self) -> None:
        first = self.admin_link.node_id()
        self.assertTrue(first)
        self.assertEqual(first, self.admin_link.node_id())  # 多次调用同一个

    def test_public_view_masks_token(self) -> None:
        self.admin_link.save({"admin_url": "http://a:9000", "admin_token": "secret-xyz", "node_name": "Alice"})
        view = self.admin_link.public_view()
        self.assertNotIn("admin_token", view)  # 不回明文
        self.assertTrue(view["has_token"])
        self.assertEqual(view["node_name"], "Alice")
        self.assertTrue(view["enabled"])

    def test_empty_token_keeps_previous(self) -> None:
        self.admin_link.save({"admin_url": "http://a:9000", "admin_token": "tok1"})
        self.admin_link.save({"admin_url": "http://a:9000", "admin_token": ""})  # 留空=不改
        self.assertTrue(self.admin_link.public_view()["has_token"])


if __name__ == "__main__":
    unittest.main()
