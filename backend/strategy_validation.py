from __future__ import annotations

import ast


def validate_strategy_code(code: str | None, *, kind: str = "策略") -> str:
    """校验导入的策略/择时代码，返回原始代码；不合法时抛出可定位原因的错误。

    用 AST(语法树) 而不是字符串匹配，这样能区分：代码为空、语法错误、
    缺少 on_bar 入口、on_bar 参数不对等不同失败原因。
    错误信息面向小组成员，用中文描述，方便直接照着改文件。
    """
    if not code or not code.strip():
        raise ValueError(f"{kind}代码为空：请先选择 .py 文件或在代码框粘贴内容")

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        location = f"第 {exc.lineno} 行" if exc.lineno else "未知位置"
        raise ValueError(f"{kind}代码有 Python 语法错误({location}): {exc.msg}") from exc

    top_level_functions: list[str] = []
    on_bar: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_bar":
            raise ValueError(f"{kind}的 on_bar 不支持 async def，请改成普通函数 def on_bar(ctx, bar)")
        if isinstance(node, ast.FunctionDef):
            top_level_functions.append(node.name)
            if node.name == "on_bar":
                on_bar = node

    if on_bar is None:
        found = "、".join(top_level_functions) if top_level_functions else "(没有任何顶层函数)"
        raise ValueError(
            f"{kind}文件必须在模块顶层定义 def on_bar(ctx, bar) 作为入口；"
            f"当前文件的顶层函数: {found}。"
            f"如果你的逻辑写在 class 或其他框架回调里，需要包一层 on_bar 转接"
        )

    # on_bar 运行时会被以 on_bar(ctx, bar) 两个位置参数调用。
    positional = len(on_bar.args.posonlyargs) + len(on_bar.args.args)
    has_vararg = on_bar.args.vararg is not None
    defaults = len(on_bar.args.defaults)
    required = positional - defaults
    if not has_vararg and (positional < 2 or required > 2):
        raise ValueError(
            f"{kind}的 on_bar 会被以 on_bar(ctx, bar) 调用，"
            f"当前定义了 {positional} 个位置参数(必填 {required} 个)，请改为 def on_bar(ctx, bar)"
        )
    return code
