"""示例插件：演示指令、定时任务、发送前改写、插件自己的存储。

可以做的：
- 指令：群里发「复读 你好」→ 机器人回「你好」
- 计数：群里发「次数」→ 回这是第几次（存在插件自己的 state 里）
- 定时：每 N 秒自动干点什么，见 on_start 里的注释
- 改字：机器人每次要发消息前都会经过 on_before_send，可以改写或拦截
- 全部能力：可以 import 任何库、读写文件、发 HTTP、调 ctx["api"].send("群名", "内容")

改完保存即生效，不用重启。停用请在 TUI 的「插件管理」里操作。
"""
DESC = "示例：指令/计数/定时/发送前改写"


def _repeat(arg, ctx):
    return arg or "说点啥"


COMMANDS = {"复读": _repeat}


def on_start(ctx):
    api = ctx["api"]
    api.log("已启动")
    # 每 10 分钟往群里发一句（不想要就别打开）
    # api.every(600, lambda api: api.send("我还在"))


def handle(text, ctx):
    api = ctx["api"]
    if (text or "").strip() == "次数":
        n = int(api.state.get("n", 0)) + 1
        api.state["n"] = n
        api.save()
        return f"第 {n} 次"
    return ""


def on_before_send(group, text, ctx):
    # 想拦住某类内容就 return False；想改写就返回新字符串；不想管就返回 None
    if "广告" in text:
        return None
    return text
