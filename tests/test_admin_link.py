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

    def test_default_admin_url_enabled_until_cleared(self) -> None:
        # 内置默认 → 新机器开箱即"已对接"(零配置)。
        self.assertTrue(self.admin_link.is_enabled())
        self.assertEqual(self.admin_link.load()["admin_url"], self.admin_link.DEFAULT_ADMIN_URL)
        # 改成自定义地址 → 用自定义。
        self.admin_link.save({"admin_url": "http://192.168.1.5:9000"})
        self.assertEqual(self.admin_link.load()["admin_url"], "http://192.168.1.5:9000")
        self.assertTrue(self.admin_link.is_enabled())
        # 清空 → 显式断开,默认不再回灌。
        self.admin_link.save({"admin_url": ""})
        self.assertEqual(self.admin_link.load()["admin_url"], "")
        self.assertFalse(self.admin_link.is_enabled())

    def test_default_not_persisted_so_it_can_change(self) -> None:
        # 仅写 node_id/node_token 的 save 不应把默认 admin_url 固化进文件(换老板机时未手配的同事能跟新默认)。
        self.admin_link.node_id()  # 触发一次 save({})
        import json
        raw = json.loads(self.admin_link._path().read_text(encoding="utf-8"))
        self.assertNotIn("admin_url", raw)  # 文件里没有 admin_url
        self.assertTrue(self.admin_link.is_enabled())  # 但 load 仍注入默认

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
        self.assertEqual(node["api_version"], 2)
        self.assertEqual(node["token"], self.admin_link.node_token())  # node.token = 节点反控 token
        seg = self.admin_link.account_segment({"id": "acct_x", "name": "主账户", "initial_cash": 1_000_000})
        self.assertEqual(seg["owner"], "主账户")  # owner 缺省回退 name
        self.assertEqual(seg["currency"], "CNY")

    def test_pick_lan_ip_prefers_physical_skips_virtual(self) -> None:
        pick = self.admin_link.pick_lan_ip
        # Docker 桥 + 真实 WiFi → 选 192.168
        self.assertEqual(pick({"172.19.0.1", "192.168.0.186", "127.0.0.1"}), "192.168.0.186")
        # Parallels 10.x + WiFi 192.168 → 仍选 192.168
        self.assertEqual(pick({"10.211.55.2", "192.168.1.7"}), "192.168.1.7")
        # 只有真实 10.x 局域网 → 选它
        self.assertEqual(pick({"10.0.0.5", "127.0.0.1"}), "10.0.0.5")
        # 只有容器段 + 回环 → 兜底容器段(总比回环强,且留 base_url 手填逃生)
        self.assertEqual(pick({"172.19.0.1", "127.0.0.1"}), "172.19.0.1")
        # Tailscale CGNAT vs WiFi → 选 WiFi
        self.assertEqual(pick({"100.100.1.2", "192.168.0.9"}), "192.168.0.9")
        self.assertEqual(pick(set()), "127.0.0.1")

    def test_bind_host_auto_lan_when_admin_configured(self) -> None:
        self.assertEqual(self.admin_link.bind_host(), "0.0.0.0")  # 默认指向老板机 → 绑局域网
        self.admin_link.save({"admin_url": ""})  # 清空=纯本地
        self.assertEqual(self.admin_link.bind_host(), "127.0.0.1")  # 断开后只听本机
        self.assertEqual(self.admin_link.bind_host("192.168.1.7"), "192.168.1.7")  # 显式 HOST 最优先

    def test_node_token_stable_and_secret(self) -> None:
        t = self.admin_link.node_token()
        self.assertTrue(t and len(t) >= 16)
        self.assertEqual(t, self.admin_link.node_token())  # 稳定
        self.assertNotEqual(t, self.admin_link.node_id())  # 与 node_id 不同

    def test_authorize_loopback_always_ok(self) -> None:
        for ip in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(self.admin_link.authorize(ip, None))  # 本机无需 token

    def test_authorize_remote_requires_matching_token(self) -> None:
        good = self.admin_link.node_token()
        self.assertFalse(self.admin_link.authorize("192.168.1.50", None))  # 远程无 token → 拒
        self.assertFalse(self.admin_link.authorize("192.168.1.50", "wrong"))  # 错 token → 拒
        self.assertTrue(self.admin_link.authorize("192.168.1.50", good))  # 对 token → 放行

    def test_deregister_path_uses_stable_node_id(self) -> None:
        nid = self.admin_link.node_id()
        self.assertEqual(self.admin_link.deregister_path("acct_9"), f"/api/admin/accounts/{nid}/acct_9/delete")

    def test_empty_token_keeps_previous(self) -> None:
        self.admin_link.save({"admin_url": "http://a:9000", "admin_token": "tok1"})
        self.admin_link.save({"admin_url": "http://a:9000", "admin_token": ""})  # 留空=不改
        self.assertTrue(self.admin_link.public_view()["has_token"])


if __name__ == "__main__":
    unittest.main()
