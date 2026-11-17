"""示例插件：群里发「ping」，机器人回 pong。

写一个插件只需要三件事：
1. 顶层常量 DESC（一句话说明这个插件干嘛的，可留空）
2. 可选常量 AUTHOR
3. 函数 handle(text, ctx)，返回要发到群里的文字；返回空字符串就表示“我不处理，交给模型”

保存到 plugins/ 目录就生效，不用重启。停用/启用请在 TUI 的「插件管理」里操作。
"""
DESC = "群里发「ping」就回「pong」"


def handle(text, ctx):
    t = (text or "").strip().lower()
    if t in ("ping", "ping!", "ping！"):
        return "pong"
    return ""
