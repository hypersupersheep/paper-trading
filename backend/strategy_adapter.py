from __future__ import annotations

import ast
from typing import Any

from backend.strategy_validation import validate_strategy_code


# 常见框架/习惯用法的回调名，按优先级转接到平台入口 on_bar。
ALIAS_NAMES = ["handle_bar", "handle_data", "on_data", "on_tick", "on_quote"]

ADAPTER_HEADER = '''

# === 以下为平台自动生成的驱动适配代码(导入时追加，请勿手改) ===
# 平台调度器以 on_bar(ctx, bar) 逐根 K 线驱动策略；
# 适配器把你文件里的原有入口接到这个驱动上。
'''

# 信号式策略(函数只吃 bar、靠返回值表达意图) 的翻译器：选股版。
STOCK_SIGNAL_HELPER = '''
def _pt_apply_signal(ctx, bar, signal):
    # 返回值约定：None/"HOLD" 忽略；"BUY"/"SELL" 按 1 手(100股) 市价；
    # (side, qty) 指定数量；dict 支持 side/quantity/symbol/reason。
    if signal is None:
        return
    side = None
    quantity = 100
    symbol = bar.get("symbol")
    reason = "adapted signal strategy"
    if isinstance(signal, str):
        side = signal.strip().upper()
        if side in {"", "HOLD", "NONE"}:
            return
    elif isinstance(signal, (tuple, list)) and signal:
        side = str(signal[0]).strip().upper()
        if len(signal) > 1:
            quantity = int(signal[1])
    elif isinstance(signal, dict):
        side = str(signal.get("side", "")).strip().upper()
        quantity = int(signal.get("quantity", 100))
        symbol = signal.get("symbol") or symbol
        reason = signal.get("reason") or reason
    else:
        ctx.log("WARNING", f"adapter ignored unsupported signal: {signal!r}")
        return
    if side not in {"BUY", "SELL"}:
        ctx.log("WARNING", f"adapter ignored unsupported signal side: {side!r}")
        return
    ctx.order_market(symbol, quantity, side=side, reason=reason)
'''

# 信号式择时策略的翻译器：返回值映射成 TimingDecision。
TIMING_SIGNAL_HELPER = '''
def _pt_apply_signal(ctx, bar, signal):
    # 返回值约定：True/False 或 "risk_on"/"risk_off" 控制开仓；
    # dict 直接透传给 ctx.set_decision(支持 allow_open/position_policy 等字段)。
    if signal is None:
        return
    if isinstance(signal, bool):
        ctx.set_decision(
            allow_open=signal,
            position_policy="hold" if signal else "reduce_only",
            reason="adapted timing signal",
        )
        return
    if isinstance(signal, str):
        value = signal.strip().lower()
        if value in {"risk_on", "allow", "open", "true"}:
            ctx.set_decision(allow_open=True, position_policy="hold", reason="adapted timing signal")
            return
        if value in {"risk_off", "block", "reduce", "false"}:
            ctx.set_decision(allow_open=False, position_policy="reduce_only", reason="adapted timing signal")
            return
    if isinstance(signal, dict):
        allowed = {"allow_open", "position_policy", "target_exposure", "reason", "valid_until", "metadata"}
        ctx.set_decision(**{key: value for key, value in signal.items() if key in allowed})
        return
    ctx.log("WARNING", f"adapter ignored unsupported timing signal: {signal!r}")
'''

SUPPORTED_HINT = (
    "支持的写法：1) def on_bar(ctx, bar)；2) 常见回调名 handle_bar/handle_data/on_data/on_tick；"
    "3) class 策略(含 on_bar/handle_bar 方法)；4) 单个入口函数——两个参数按 (ctx, bar) 调用，"
    "一个参数按信号函数处理(返回 BUY/SELL/(side,qty)/dict)"
)


def adapt_strategy_code(code: str | None, *, kind: str = "策略", flavor: str = "stock") -> dict[str, Any]:
    """分析导入代码并自动接入平台驱动，返回 {code, mode, entry}。

    无法适配时抛出说明支持写法的 ValueError。flavor 决定信号翻译器：
    stock → 下单，timing → TimingDecision。
    """
    if not code or not code.strip():
        raise ValueError(f"{kind}代码为空：请先选择 .py 文件或在代码框粘贴内容")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        location = f"第 {exc.lineno} 行" if exc.lineno else "未知位置"
        raise ValueError(f"{kind}代码有 Python 语法错误({location}): {exc.msg}") from exc

    top_functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_bar":
            raise ValueError(f"{kind}的 on_bar 不支持 async def，请改成普通函数 def on_bar(ctx, bar)")
        if isinstance(node, ast.FunctionDef):
            top_functions[node.name] = node

    # 1. 原生 on_bar：只校验签名，不改代码。
    if "on_bar" in top_functions:
        validate_strategy_code(code, kind=kind)
        return {"code": code, "mode": "native", "entry": "on_bar"}

    # 2. 常见回调名转接。
    for alias in ALIAS_NAMES:
        node = top_functions.get(alias)
        if node:
            return _adapt_function(code, node, kind=kind, flavor=flavor, mode="alias_function")

    # 3. class 策略：唯一一个带 bar 回调方法的类。
    class_candidates: list[tuple[ast.ClassDef, ast.FunctionDef]] = []
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for member in cls.body:
            if isinstance(member, ast.FunctionDef) and member.name in ("on_bar", *ALIAS_NAMES):
                class_candidates.append((cls, member))
                break
    if len(class_candidates) > 1:
        names = "、".join(cls.name for cls, _ in class_candidates)
        raise ValueError(f"{kind}文件里有多个候选策略类({names})，无法确定入口，请只保留一个或自行定义 on_bar")
    if len(class_candidates) == 1:
        return _adapt_class(code, *class_candidates[0], kind=kind, flavor=flavor)

    # 4. 唯一的顶层函数：不管叫什么名字都当作入口。
    if len(top_functions) == 1:
        node = next(iter(top_functions.values()))
        return _adapt_function(code, node, kind=kind, flavor=flavor, mode="single_function")

    found = "、".join(top_functions) if top_functions else "(没有任何顶层函数)"
    raise ValueError(f"{kind}文件无法自动接入驱动；当前顶层函数: {found}。{SUPPORTED_HINT}")


def _positional_signature(node: ast.FunctionDef, *, drop_self: bool = False) -> tuple[int, int, bool]:
    """返回 (位置参数个数, 必填个数, 是否有 *args)；drop_self 用于类方法。"""
    positional = len(node.args.posonlyargs) + len(node.args.args)
    if drop_self and positional:
        positional -= 1
    required = positional - len(node.args.defaults)
    return positional, max(required, 0), node.args.vararg is not None


def _signal_helper(flavor: str) -> str:
    return TIMING_SIGNAL_HELPER if flavor == "timing" else STOCK_SIGNAL_HELPER


def _adapt_function(code: str, node: ast.FunctionDef, *, kind: str, flavor: str, mode: str) -> dict[str, Any]:
    positional, required, has_vararg = _positional_signature(node)
    if has_vararg or positional >= 2:
        if required > 2:
            raise ValueError(
                f"{kind}入口 {node.name} 必填参数有 {required} 个，平台只会传 (ctx, bar) 两个，请减少必填参数"
            )
        adapter = f"{ADAPTER_HEADER}\ndef on_bar(ctx, bar):\n    return {node.name}(ctx, bar)\n"
        return {"code": code + adapter, "mode": mode, "entry": node.name}
    if positional == 1:
        adapter = (
            f"{ADAPTER_HEADER}{_signal_helper(flavor)}\n"
            f"def on_bar(ctx, bar):\n    _pt_apply_signal(ctx, bar, {node.name}(bar))\n"
        )
        return {"code": code + adapter, "mode": "signal_function", "entry": node.name}
    raise ValueError(f"{kind}入口 {node.name} 不接收任何参数，无法按 bar 驱动。{SUPPORTED_HINT}")


def _adapt_class(code: str, cls: ast.ClassDef, method: ast.FunctionDef, *, kind: str, flavor: str) -> dict[str, Any]:
    init = next(
        (member for member in cls.body if isinstance(member, ast.FunctionDef) and member.name == "__init__"),
        None,
    )
    if init:
        _, init_required, _ = _positional_signature(init, drop_self=True)
        if init_required > 0:
            raise ValueError(
                f"{kind}类 {cls.name} 的 __init__ 有必填参数，平台无法自动实例化；请提供无参构造或改用函数式入口"
            )

    positional, required, has_vararg = _positional_signature(method, drop_self=True)
    lines = [ADAPTER_HEADER, f"_pt_strategy_instance = {cls.name}()\n"]
    if has_vararg or positional >= 2:
        if required > 2:
            raise ValueError(
                f"{kind}类 {cls.name}.{method.name} 必填参数有 {required} 个，平台只会传 (ctx, bar) 两个"
            )
        lines.append(f"\ndef on_bar(ctx, bar):\n    return _pt_strategy_instance.{method.name}(ctx, bar)\n")
        mode = "class_method"
    elif positional == 1:
        lines.append(_signal_helper(flavor))
        lines.append(
            f"\ndef on_bar(ctx, bar):\n    _pt_apply_signal(ctx, bar, _pt_strategy_instance.{method.name}(bar))\n"
        )
        mode = "class_signal"
    else:
        raise ValueError(f"{kind}类 {cls.name}.{method.name} 不接收 bar 参数，无法驱动。{SUPPORTED_HINT}")

    on_init = next(
        (member for member in cls.body if isinstance(member, ast.FunctionDef) and member.name == "on_init"),
        None,
    )
    if on_init:
        init_positional, _, _ = _positional_signature(on_init, drop_self=True)
        call = "_pt_strategy_instance.on_init(ctx)" if init_positional >= 1 else "_pt_strategy_instance.on_init()"
        lines.append(f"\ndef on_init(ctx):\n    {call}\n")

    return {"code": code + "".join(lines), "mode": mode, "entry": f"{cls.name}.{method.name}"}
