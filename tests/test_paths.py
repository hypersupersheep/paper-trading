from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend import paths


class PathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("PAPER_TRADING_HOME")
        self._saved_ptr = os.environ.get("PAPER_TRADING_POINTER")
        # 把指针文件隔离到临时不存在路径,避免本机真实指针干扰测试。
        self._ptr_dir = tempfile.TemporaryDirectory()
        os.environ["PAPER_TRADING_POINTER"] = str(Path(self._ptr_dir.name) / "home")

    def tearDown(self) -> None:
        self._ptr_dir.cleanup()
        for key, val in (("PAPER_TRADING_HOME", self._saved), ("PAPER_TRADING_POINTER", self._saved_ptr)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_pointer_overrides_default_and_clear_resets(self) -> None:
        os.environ.pop("PAPER_TRADING_HOME", None)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mydata"
            saved = paths.set_home_pointer(target)
            self.assertTrue(saved.exists())
            self.assertEqual(paths.home(), target.resolve())
            # 环境变量优先级高于指针
            os.environ["PAPER_TRADING_HOME"] = "/tmp/envwins"
            self.assertEqual(paths.home(), Path("/tmp/envwins"))
            os.environ.pop("PAPER_TRADING_HOME", None)
            paths.clear_home_pointer()
            self.assertNotEqual(paths.home(), target.resolve())

    def test_default_home_is_code_root(self) -> None:
        os.environ.pop("PAPER_TRADING_HOME", None)
        self.assertEqual(paths.home(), paths.ROOT)
        # 默认 db 在代码根的 data/ 下,保持现状不破坏。
        self.assertEqual(paths.db_path(), paths.ROOT / "data" / "audit.sqlite3")

    def test_env_override_relocates_all_writable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["PAPER_TRADING_HOME"] = tmp
            home = Path(tmp)
            self.assertEqual(paths.home(), home)
            self.assertEqual(paths.db_path(), home / "data" / "audit.sqlite3")
            self.assertEqual(paths.connector_settings_path(), home / "data" / "connector_settings.json")
            # 目录函数会自动创建(打包后用户目录首次启动即用)。
            self.assertTrue(paths.strategies_dir().exists())
            self.assertTrue(paths.timing_strategies_dir().exists())
            self.assertTrue(paths.data_dir().exists())

    def test_public_dir_follows_code_not_home(self) -> None:
        # 静态资源跟代码走(打包进 bundle),不随数据目录搬家。
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["PAPER_TRADING_HOME"] = tmp
            self.assertEqual(paths.public_dir(), paths.ROOT / "public")


if __name__ == "__main__":
    unittest.main()
