from __future__ import annotations

import os
import tempfile
import unittest


class AppSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # 保存外部(如 shell/CI)预设的环境,tearDown 还原,避免污染后续测试的默认数据源。
        self._saved_home = os.environ.get("PAPER_TRADING_HOME")
        self._saved_ds = os.environ.get("PT_DEFAULT_DATA_SOURCE")
        os.environ["PAPER_TRADING_HOME"] = self.tmp.name
        os.environ.pop("PT_DEFAULT_DATA_SOURCE", None)
        from backend import app_settings
        self.app_settings = app_settings

    def tearDown(self) -> None:
        for key, saved in (
            ("PAPER_TRADING_HOME", self._saved_home),
            ("PT_DEFAULT_DATA_SOURCE", self._saved_ds),
        ):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
        self.tmp.cleanup()

    def test_code_default_is_tongdaxin(self) -> None:
        self.assertEqual(self.app_settings.default_data_source(), "tongdaxin")

    def test_set_persists_and_reads_back(self) -> None:
        self.app_settings.set_default_data_source("WIND")
        self.assertEqual(self.app_settings.default_data_source(), "wind")  # 归一化小写
        # 重新读(模块无内存缓存,直接读文件)
        self.assertEqual(self.app_settings.load()["default_data_source"], "wind")

    def test_env_override_wins(self) -> None:
        self.app_settings.set_default_data_source("wind")
        os.environ["PT_DEFAULT_DATA_SOURCE"] = "fixture"
        self.assertEqual(self.app_settings.default_data_source(), "fixture")

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.app_settings.set_default_data_source("")


if __name__ == "__main__":
    unittest.main()
