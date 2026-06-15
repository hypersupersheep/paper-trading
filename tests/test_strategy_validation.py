from __future__ import annotations

import unittest

from backend.strategy_validation import validate_strategy_code


class StrategyValidationTest(unittest.TestCase):
    def test_valid_on_bar_passes(self) -> None:
        code = "def on_bar(ctx, bar):\n    pass\n"
        self.assertEqual(validate_strategy_code(code), code)

    def test_on_bar_with_defaults_and_varargs_passes(self) -> None:
        validate_strategy_code("def on_bar(ctx, bar, extra=None):\n    pass\n")
        validate_strategy_code("def on_bar(*args):\n    pass\n")

    def test_empty_code_reports_empty(self) -> None:
        with self.assertRaisesRegex(ValueError, "代码为空"):
            validate_strategy_code("")
        with self.assertRaisesRegex(ValueError, "代码为空"):
            validate_strategy_code(None)

    def test_syntax_error_reports_line_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "语法错误.*第 2 行"):
            validate_strategy_code("x = 1\ndef on_bar(ctx, bar:\n    pass\n")

    def test_missing_on_bar_lists_found_functions(self) -> None:
        with self.assertRaisesRegex(ValueError, "顶层函数: handle_bar、main"):
            validate_strategy_code(
                "def handle_bar(ctx, bar):\n    pass\n\ndef main():\n    pass\n"
            )

    def test_script_without_functions_reports_no_functions(self) -> None:
        with self.assertRaisesRegex(ValueError, "没有任何顶层函数"):
            validate_strategy_code("print('hello')\n")

    def test_method_inside_class_does_not_count(self) -> None:
        # class 里的 on_bar 不是模块顶层入口，应明确拒绝。
        with self.assertRaisesRegex(ValueError, "模块顶层定义"):
            validate_strategy_code(
                "class Strategy:\n    def on_bar(self, ctx, bar):\n        pass\n"
            )

    def test_wrong_arg_count_reports_signature(self) -> None:
        with self.assertRaisesRegex(ValueError, "位置参数"):
            validate_strategy_code("def on_bar(ctx):\n    pass\n")
        with self.assertRaisesRegex(ValueError, "位置参数"):
            validate_strategy_code("def on_bar(ctx, bar, extra):\n    pass\n")

    def test_async_on_bar_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "async"):
            validate_strategy_code("async def on_bar(ctx, bar):\n    pass\n")

    def test_kind_label_appears_in_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "择时策略"):
            validate_strategy_code("print(1)\n", kind="择时策略")


if __name__ == "__main__":
    unittest.main()
