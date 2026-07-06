"""单一版本来源。API 演进时只改这里;agent/skill 通过 /api/meta 读取做兼容判断。"""

APP_NAME = "量化模拟盘 Paper Trading"
__version__ = "1.15.7"
# API 大版本:破坏性改动才 +1。skill/agent 用它判断是否兼容,这样后端能演进而不悄悄打破旧客户端。
# v2:拔除 sleeve 资金单元,改单一 account.cash 现金模型(去 sleeve 路由/参数,老库自动迁移)。
API_VERSION = 2
