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

    def test_node_descriptor_and_account_segment_shape(self) -> None:
        self.admin_link.save({"admin_url": "http://a:9000", "node_name": "Alice机"})
        node = self.admin_link.node_descriptor(8123)
        self.assertEqual(node["name"], "Alice机")
        self.assertTrue(node["base_url"].endswith(":8123"))
        self.assertEqual(node["api_version"], 1)
        self.assertEqual(node["token"], "")  # node.token 暂空(node_patch 才填)
        seg = self.admin_link.account_segment({"id": "acct_x", "name": "主账户", "initial_cash": 1_000_000})
        self.assertEqual(seg["owner"], "主账户")  # owner 缺省回退 name
        self.assertEqual(seg["currency"], "CNY")

    def test_deregister_path_uses_stable_node_id(self) -> None:
        nid = self.admin_link.node_id()
        self.assertEqual(self.admin_link.deregister_path("acct_9"), f"/api/admin/accounts/{nid}/acct_9/delete")

    def test_empty_token_keeps_previous(self) -> None:
        self.admin_link.save({"admin_url": "http://a:9000", "admin_token": "tok1"})
        self.admin_link.save({"admin_url": "http://a:9000", "admin_token": ""})  # 留空=不改
        self.assertTrue(self.admin_link.public_view()["has_token"])


if __name__ == "__main__":
    unittest.main()
