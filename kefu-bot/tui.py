"""命令行界面（TUI）：方向键选择菜单，管理快手客服机器人。

先启动后端（start.bat 或 app.py），再运行：python tui.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import msvcrt
except ImportError:  # 非 Windows
    msvcrt = None

BASE = "http://127.0.0.1:8800"
ROOT = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 基础
def api(path, data=None, method=None, timeout=60):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        BASE + path, data=body, method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def pause(msg="按任意键返回"):
    print()
    print(msg)
    if msvcrt:
        msvcrt.getch()
    else:
        input()


def ask(prompt, default=""):
    clear()
    print(prompt)
    if default != "":
        print(f"（直接回车 = {default}）")
    print()
    v = input("> ").strip()
    return v if v else default


def ask_yes(prompt, default=False):
    d = "y" if default else "n"
    v = ask(prompt + " [y/n]", d).lower()
    return v.startswith("y")


def read_key():
    k = msvcrt.getch()
    if k in (b"\x00", b"\xe0"):
        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(
            msvcrt.getch().decode("latin1"), "")
    if k == b"\r":
        return "enter"
    if k == b"\x1b":
        return "esc"
    try:
        c = k.decode("utf-8", "ignore").lower()
    except Exception:
        c = ""
    if c in ("q", "\x03"):
        return "esc"
    return c


def menu(title, items, footer=""):
    """items: [(显示文字, 返回值)]；返回选中项的值，Esc/q 返回 None。"""
    if not items:
        return None
    sel = 0
    while True:
        clear()
        print(title)
        print("=" * 58)
        for i, (label, _) in enumerate(items):
            print(("  \u25b6 " if i == sel else "    ") + label)
        print()
        if footer:
            print(footer)
            print()
        print("\u2191\u2193 移动 · 回车确认 · Esc 返回")
        k = read_key()
        if k == "up":
            sel = (sel - 1) % len(items)
        elif k == "down":
            sel = (sel + 1) % len(items)
        elif k == "enter":
            return items[sel][1]
        elif k == "esc":
            return None


def onoff(v):
    return "开" if v else "关"


# ---------------------------------------------------------------- 状态
def screen_status():
    while True:
        clear()
        try:
            ks = api("/api/kuaishou", timeout=15)
            cfg = api("/api/config", timeout=15)
        except Exception as e:
            print("连不上后端（先运行 start.bat 启动）")
            print(e)
            pause()
            return
        st = ks.get("status", {})
        ago = int(time.time() - (st.get("last_poll") or 0)) if st.get("last_poll") else -1
        print("运行状态")
        print("=" * 58)
        print(f"后端        127.0.0.1:8800")
        try:
            br = api("/api/ks/action", {"action": "status"}, timeout=8)
            root_txt = ("已开启（%s）" % br.get("serial", "")) if br.get("root") \
                else "未开启（界面模式够用；数据库模式需要）"
        except Exception:
            root_txt = "未知（桥没连上模拟器）"
        print(f"Root 权限   {root_txt}")
        print(f"自动回复    {onoff(ks.get('enabled'))}")
        print(f"读消息方式  {'界面' if ks.get('source') == 'ui' else '数据库'}")
        print(f"轮询间隔    {ks.get('poll')} 秒")
        print(f"上次轮询    {ago if ago >= 0 else '还没读'} 秒前")
        print(f"已回复      {st.get('replied')} 条")
        print(f"最近错误    {st.get('last_error') or '无'}")
        print(f"模型        {cfg.get('vision_model')}")
        print(f"群聊        {', '.join(ks.get('groups') or []) or '（未设置）'}")
        print(f"前缀        {ks.get('prefix') or '（无）'}")
        print(f"识别媒体    {onoff(ks.get('stickers'))}")
        print(f"自动分享    {onoff(ks.get('share_link'))} / {ks.get('share_min')} 分钟")
        print(f"发感想      {onoff(ks.get('think_on'))} / {ks.get('think_min')} 分钟")
        print(f"闲聊        每 {ks.get('idle_min')} 分钟" if ks.get("idle_min") else "闲聊        关")
        print()
        print("（自动刷新，按任意键返回）")
        if msvcrt:
            t0 = time.time()
            while time.time() - t0 < 5:
                if msvcrt.kbhit():
                    msvcrt.getch()
                    return
                time.sleep(0.1)


def set_ks(key, value):
    api("/api/kuaishou", {key: value})


def screen_groups():
    while True:
        ks = api("/api/kuaishou", timeout=15)
        cfg = api("/api/config", timeout=15)
        c = menu("群聊与回复", [
            (f"自动回复：{onoff(ks.get('enabled'))}", "toggle_enabled"),
            (f"读消息方式：{'界面' if ks.get('source') == 'ui' else '数据库'}", "source"),
            (f"轮询间隔：{ks.get('poll')} 秒", "poll"),
            (f"群聊列表：{', '.join(ks.get('groups') or []) or '（未设置）'}", "groups"),
            (f"@ 前缀：{ks.get('prefix') or '（无，任何消息都回）'}", "prefix"),
            (f"识别视频/图片/表情：{onoff(ks.get('stickers'))}", "toggle_stickers"),
            (f"自动下载并看视频：{onoff(ks.get('auto_video'))}", "toggle_auto_video"),
            (f"视频/图片上限：{cfg.get('max_file_mb')} MB（超过先压缩）", "max_mb"),
            (f"自动分享视频：{onoff(ks.get('share_link'))}", "toggle_share"),
            (f"分享间隔：{ks.get('share_min')} 分钟 / 每次 {ks.get('share_count')} 条", "share_tune"),
            (f"自动发感想：{onoff(ks.get('think_on'))}", "toggle_think"),
            (f"感想间隔：{ks.get('think_min')} 分钟", "think_min"),
            (f"闲聊间隔：{ks.get('idle_min')} 分钟（0=关）", "idle"),
            (f"接管通知语：{ks.get('notice') or '（空，不发）'}", "notice"),
        ])
        if c is None:
            return
        try:
            if c == "toggle_enabled":
                set_ks("ks_enabled", not ks.get("enabled"))
            elif c == "source":
                set_ks("ks_source", "db" if ks.get("source") == "ui" else "ui")
            elif c == "poll":
                set_ks("ks_poll", int(ask("轮询间隔（秒，最小 3）", str(ks.get("poll")))))
            elif c == "groups":
                v = ask("群聊名称，多个用英文逗号隔开", ",".join(ks.get("groups") or []))
                set_ks("ks_groups", [x.strip() for x in v.split(",") if x.strip()])
            elif c == "prefix":
                set_ks("ks_prefix", ask("回复前缀（留空=任何消息都回）", ks.get("prefix") or ""))
            elif c == "toggle_stickers":
                set_ks("ks_stickers", not ks.get("stickers"))
            elif c == "toggle_auto_video":
                set_ks("ks_auto_video", not ks.get("auto_video"))
            elif c == "max_mb":
                api("/api/config", {"max_file_mb": int(ask("视频/图片上限（MB）",
                                                         str(cfg.get("max_file_mb"))))})
            elif c == "toggle_share":
                set_ks("ks_share_link", not ks.get("share_link"))
            elif c == "share_tune":
                set_ks("ks_share_min", int(ask("分享间隔（分钟）", str(ks.get("share_min")))))
                set_ks("ks_share_count", int(ask("每次分享几条", str(ks.get("share_count")))))
            elif c == "toggle_think":
                set_ks("ks_think_on", not ks.get("think_on"))
            elif c == "think_min":
                set_ks("ks_think_min", int(ask("感想间隔（分钟，0=关）", str(ks.get("think_min")))))
            elif c == "idle":
                set_ks("ks_idle_min", int(ask("闲聊间隔（分钟，0=关）", str(ks.get("idle_min")))))
            elif c == "notice":
                set_ks("ks_notice", ask("接管时发到群里的一句话（留空=不发）", ks.get("notice") or ""))
        except ValueError:
            pause("请输入数字")
        except Exception as e:
            pause(f"保存失败：{e}")


def screen_model():
    while True:
        cfg = api("/api/config", timeout=15)
        c = menu("模型与提示词", [
            (f"模型：{cfg.get('vision_model')}", "model"),
            (f"API Key：{cfg.get('api_key') or '（未填）'}", "key"),
            (f"上下文：{'全部记录' if cfg.get('full_context') else '最近若干条'}", "ctx"),
            ("编辑人设提示词（让 AI 更像人）", "prompt"),
        ])
        if c is None:
            return
        try:
            if c == "model":
                v = ask("模型名", cfg.get("vision_model"))
                api("/api/config", {"model": v, "vision_model": v})
            elif c == "key":
                api("/api/config", {"api_key": ask("API Key")})
            elif c == "ctx":
                api("/api/config", {"full_context": not cfg.get("full_context")})
            elif c == "prompt":
                print()
                print("当前提示词：")
                print(cfg.get("system_prompt"))
                print()
                v = input("新的提示词（直接回车不改）: ").strip()
                if v:
                    api("/api/config", {"system_prompt": v})
        except Exception as e:
            pause(f"保存失败：{e}")


def screen_plugins():
    while True:
        data = api("/api/plugins", timeout=20)
        pls = data.get("plugins", [])
        items = []
        for p in pls:
            state = "开" if p.get("enabled") else "关"
            desc = p.get("error") or p.get("desc") or "（无说明）"
            cmds = p.get("commands") or []
            extra = ("  指令：" + "/".join(cmds)) if cmds else ""
            items.append((f"[{state}] {p['name']}  {desc}{extra}", ("toggle", p["name"])))
        if items:
            items.append(("-" * 40, ("noop", "")))
        items += [
            ("＋ 从本地文件安装", ("install", "file")),
            ("＋ 从链接安装", ("install", "url")),
            ("＋ 粘贴代码新建", ("install", "code")),
            ("－ 删除插件", ("remove", "")),
            ("打开插件目录", ("open", "")),
            ("重新加载插件列表", ("reload", "")),
        ]
        footer = f"插件目录：{data.get('dir')}"
        c = menu("插件管理", items, footer)
        if c is None:
            return
        if c[0] in (None, "noop"):
            continue
        try:
            if c[0] == "toggle":
                cur = next((p["enabled"] for p in pls if p["name"] == c[1]), True)
                api("/api/plugins", {"name": c[1], "enabled": not cur})
            elif c[0] == "install":
                if c[1] == "code":
                    name = ask("插件名（英文，如 weather）")
                    print()
                    print("粘贴代码，输入一行 END 结束：")
                    lines = []
                    while True:
                        line = input()
                        if line.strip() == "END":
                            break
                        lines.append(line)
                    api("/api/plugins", {"install": True, "code": "\n".join(lines), "name": name})
                else:
                    what = "文件完整路径" if c[1] == "file" else "插件 .py 的下载链接"
                    v = ask(what)
                    api("/api/plugins", {"install": True, "source": c[1], "value": v})
                pause("安装完成")
            elif c[0] == "remove":
                api("/api/plugins?name=" + urllib.parse.quote(ask("要删除的插件名")),
                    method="DELETE")
                pause("已删除")
            elif c[0] == "open":
                os.startfile(data.get("dir") or os.path.join(ROOT, "plugins"))
            elif c[0] == "reload":
                continue
        except Exception as e:
            pause(f"操作失败：{e}")


def screen_send():
    ks = api("/api/kuaishou", timeout=15)
    groups = ks.get("groups") or []
    if len(groups) > 1:
        g = menu("选择群聊", [(x, x) for x in groups])
        if not g:
            return
    elif groups:
        g = groups[0]
    else:
        g = ask("群聊名称")
    text = ask(f"发到「{g}」的内容")
    if not text:
        return
    clear()
    print("发送中…")
    try:
        r = api("/api/ks/action", {"action": "send", "group": g, "text": text}, timeout=180)
        print("已发送" if r.get("ok", True) else r)
    except Exception as e:
        print("发送失败：", e)
    pause()


# ---------------------------------------------------------------- 主循环
def main():
    try:
        api("/api/config", timeout=5)
    except Exception:
        clear()
        print("后端没有启动。先双击 start.bat，或在本目录运行：python app.py")
        pause()
        sys.exit(1)
    if not msvcrt:
        print("这个界面需要 Windows 控制台")
        sys.exit(1)
    while True:
        c = menu("快手客服机器人", [
            ("运行状态", "status"),
            ("群聊与回复", "groups"),
            ("模型与提示词", "model"),
            ("插件管理", "plugins"),
            ("手动发消息到群里", "send"),
            ("退出", "quit"),
        ], footer="用 ↑↓ 选择，回车确认")
        if c in (None, "quit"):
            clear()
            print("已退出（机器人仍在后台运行）")
            return
        {"status": screen_status, "groups": screen_groups, "model": screen_model,
         "plugins": screen_plugins, "send": screen_send}[c]()


if __name__ == "__main__":
    try:
        import urllib.parse  # noqa: F401  (screen_plugins 里用到)
        main()
    except KeyboardInterrupt:
        print("\n已退出")
