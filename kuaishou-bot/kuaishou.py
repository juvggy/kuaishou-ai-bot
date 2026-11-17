#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快手 ADB 自动化插件（纯规则执行，不依赖模型会调用工具）。

覆盖：
  - 刷视频（自动上滑/停留/点赞）
  - 群聊：列会话、读消息、发文字、发表情、抓群里的视频/图片
  - 录屏并按 200MB 上限压缩（优先保清晰度/文字）
  - 自动活跃气氛 + 规则自动回复（可选接入 GLM-4.1）
  - 本地 HTTP API：任何模型（哪怕不会 function calling）发个 POST 就能用

示例：
  python kuaishou.py status
  python kuaishou.py login --wait
  python kuaishou.py watch --minutes 10
  python kuaishou.py msgs
  python kuaishou.py read 兄弟群 -n 30
  python kuaishou.py send 兄弟群 "来了"
  python kuaishou.py sticker 兄弟群 --index 2
  python kuaishou.py media 兄弟群 --mode clip --seconds 8
  python kuaishou.py record --seconds 60
  python kuaishou.py compress D:\a.mp4 --max-mb 200
  python kuaishou.py auto --group 兄弟群
  python kuaishou.py api --port 8765
"""
import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PKG = "com.smile.gifmaker"
HOME_ACTIVITY = "com.yxcorp.gifshow.HomeActivity"
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "state.json")
CONFIG_PATH = os.path.join(BASE_DIR, "rules.json")
_MEIPASS = getattr(sys, "_MEIPASS", BASE_DIR)
KB_APK = os.path.join(_MEIPASS, "ADBKeyboard.apk")
MEDIA_DIR = os.path.join(BASE_DIR, "media")

ADB_FALLBACKS = [
    os.path.join(BASE_DIR, "platform-tools", "adb.exe"),   # 放在程序旁边的 adb
    r"C:\platform-tools\adb.exe",
    r"C:\Android\platform-tools\adb.exe",
]

# 底部导航（1080x1920）
TAB_HOME = (108, 1845)
TAB_MSG = (756, 1845)
TAB_ME = (972, 1845)


def find_adb():
    exe = shutil.which("adb")
    if exe:
        return exe
    for p in ADB_FALLBACKS:
        if os.path.exists(p):
            return p
    raise SystemExit("未找到 adb，请把 platform-tools 加入 PATH")


ADB = find_adb()

# 常见模拟器的 ADB 端口：没出现在 adb devices 里就主动连一下
EMU_PORTS = ["127.0.0.1:16384", "127.0.0.1:7555", "127.0.0.1:21503",
             "127.0.0.1:5555", "127.0.0.1:5554", "127.0.0.1:62001"]


def _connect_emulators():
    for p in EMU_PORTS:
        try:
            subprocess.run([ADB, "connect", p], capture_output=True, timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------- 设备层
class Device:
    def __init__(self, serial=None):
        self.serial = serial or self._pick_serial()
        self.root = False
        self._ensure_root()
        self.w, self.h = self._screen_size()

    def _ensure_root(self):
        """尽量拿到 root：adb root → 重连 → 复查，最多 3 轮。"""
        for i in range(3):
            try:
                out = self._raw(["shell", "id"], timeout=30).stdout.decode("utf-8", "replace")
                if "uid=0" in out:
                    self.root = True
                    print("[device] 已获得 root 权限")
                    return
            except Exception as e:
                print("[device] 检查 root 失败:", e)
            if i == 0:
                try:
                    r = self._raw(["root"], timeout=30)
                    msg = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
                    if msg:
                        print("[device] adb root:", msg.splitlines()[0])
                except Exception as e:
                    print("[device] adb root 失败:", e)
                time.sleep(2)
                if re.search(r":\d+$", self.serial or ""):
                    try:
                        self._raw(["connect", self.serial], timeout=30)
                    except Exception:
                        pass
                    time.sleep(1.5)
            time.sleep(1)
        print("[device] 未拿到 root：数据库读取/部分文件访问会失败。"
              "请在模拟器设置里打开 root 权限并重启模拟器。")

    def _raw(self, args, timeout=90):
        cmd = [ADB] + (["-s", self.serial] if self.serial else []) + args
        return subprocess.run(cmd, capture_output=True, timeout=timeout)

    def run(self, args, retries=6, timeout=90):
        last = ""
        for _ in range(retries):
            try:
                r = self._raw(args, timeout=timeout)
            except subprocess.TimeoutExpired:
                last = "timeout"
                time.sleep(1)
                continue
            out = r.stdout.decode("utf-8", "replace")
            err = r.stderr.decode("utf-8", "replace")
            if r.returncode == 0 and "device offline" not in err and "no devices" not in err:
                return out
            last = (err or out).strip()
            if "not found" in last or "device offline" in last:
                self._recover()
            time.sleep(0.8)
        raise RuntimeError(f"adb {' '.join(args)} 失败: {last}")

    def _recover(self):
        """设备掉了就自动重连 / 重新挑一个在线设备。"""
        try:
            if self.serial and re.search(r":\d+$", self.serial):
                subprocess.run([ADB, "connect", self.serial], capture_output=True, timeout=25)
            else:
                r = subprocess.run([ADB, "devices"], capture_output=True, timeout=20)
                for line in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
                    p = line.split()
                    if len(p) >= 2 and p[1] == "device":
                        self.serial = p[0]
                        break
            time.sleep(1.5)
        except Exception:
            pass

    def shell(self, cmd, timeout=90):
        return self.run(["shell", cmd], timeout=timeout)

    @staticmethod
    def _pick_serial():
        _connect_emulators()
        for i in range(10):
            r = subprocess.run([ADB, "devices"], capture_output=True, timeout=20)
            for line in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
                p = line.split()
                if len(p) >= 2 and p[1] == "device":
                    return p[0]
            if i % 3 == 2:
                _connect_emulators()
            time.sleep(1)
        raise SystemExit("没有检测到 ADB 设备：请在模拟器设置里打开 ADB 调试（MuMu：设置 → 其他 → ADB 调试）")

    def _screen_size(self):
        m = re.search(r"(\d+)x(\d+)", self.shell("wm size"))
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)

    def tap(self, x, y):
        self.shell(f"input tap {int(x)} {int(y)}")
        return (int(x), int(y))

    def swipe(self, x1, y1, x2, y2, ms=260):
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")

    def back(self):
        self.shell("input keyevent 4")

    def launch(self):
        self.shell(f"am start -n {PKG}/{HOME_ACTIVITY}")
        time.sleep(4)

    def clickable(self, text, exact=False):
        root = self.dump_ui()
        hits = find(root, text=text, exact=exact)
        for n in hits:
            c = center(n)
            if c:
                self.tap(*c)
                return True
        return False

    # ---------- UI ----------
    def dump_ui(self, retries=6):
        last = ""
        for _ in range(retries):
            r = self._raw(["exec-out", "uiautomator", "dump", "--compressed", "/dev/tty"])
            data = r.stdout.decode("utf-8", "replace")
            s = data.find("<?xml")
            e = data.find("</hierarchy>", s)
            if s >= 0 and e > s:
                try:
                    return ET.fromstring(data[s:e + len("</hierarchy>")])
                except ET.ParseError as ex:
                    last = str(ex)
            else:
                last = (data + r.stderr.decode("utf-8", "replace")).strip()[-200:]
            time.sleep(0.7)
        raise RuntimeError(f"界面读取失败: {last}")

    def screenshot(self, path=None):
        os.makedirs(MEDIA_DIR, exist_ok=True)
        path = path or os.path.join(MEDIA_DIR, f"shot_{int(time.time())}.png")
        r = subprocess.run([ADB, "-s", self.serial, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=60)
        with open(path, "wb") as f:
            f.write(r.stdout)
        return path

    def input_text(self, text):
        if not kb_installed(self.serial):
            raise RuntimeError("未安装 ADBKeyboard，先执行: python kuaishou.py install-kb")
        old = self.shell("settings get secure default_input_method").strip()
        self.shell("ime enable com.android.adbkeyboard/.AdbIME")
        self.shell("ime set com.android.adbkeyboard/.AdbIME")
        time.sleep(0.4)
        payload = text.replace("'", "\\'")
        self.shell(f"am broadcast -a ADB_INPUT_TEXT --es msg '{payload}'")
        time.sleep(0.6)
        if old and "adbkeyboard" not in old:
            self.shell(f"ime set {old}")


# ---------------------------------------------------------------- UI 解析
def bounds(node):
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", node.get("bounds", ""))
    return tuple(map(int, m.groups())) if m else None


def center(node):
    b = bounds(node)
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) if b else None


def text_of(node):
    return (node.get("text") or node.get("content-desc") or "").strip()


def nodes(root):
    return list(root.iter("node"))


def find(root, text=None, rid=None, desc=None, cls=None, exact=False):
    out = []
    for n in nodes(root):
        if rid and rid not in n.get("resource-id", ""):
            continue
        if cls and cls not in n.get("class", ""):
            continue
        t = (n.get("text") or "").strip()
        d = (n.get("content-desc") or "").strip()
        if text:
            ok = (t == text or d == text) if exact else (text in t or text in d)
            if not ok:
                continue
        if desc and desc not in d:
            continue
        out.append(n)
    return out


def kb_installed(serial=None):
    try:
        cmd = [ADB] + (["-s", serial] if serial else []) + \
              ["shell", "pm list packages com.android.adbkeyboard"]
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        return "com.android.adbkeyboard" in r.stdout.decode("utf-8", "replace")
    except Exception:
        return False


# ---------------------------------------------------------------- 快手流程
def goto_messages(dev):
    dev.tap(*TAB_MSG)
    time.sleep(1.8)


def dismiss_login(dev):
    """未登录时快捷登录弹窗会挡住视频/消息，自动退出登录页。"""
    for _ in range(3):
        try:
            root = dev.dump_ui(2)
        except Exception:
            return False
        if not (find(root, text="登录后更精彩") or find(root, rid="phone_et")):
            return True
        dev.back()
        time.sleep(1.2)
        try:
            r2 = dev.dump_ui(2)
            btn = find(r2, text="坚持退出")
            if btn:
                dev.tap(*center(btn[0]))
                time.sleep(1.2)
        except Exception:
            pass
    return False


def goto_home(dev):
    dev.tap(*TAB_HOME)
    time.sleep(1.5)
    dismiss_login(dev)


def list_conversations(dev):
    goto_messages(dev)
    root = dev.dump_ui()
    ignore = {"消息", "私信", "互动消息", "群聊", "通知", "全部", "返回", "帮助",
              "首页", "我", "精选", "发布", "拍摄"}
    items = []
    for n in nodes(root):
        t = (n.get("text") or "").strip()
        b = bounds(n)
        if not t or t in ignore or b is None:
            continue
        if b[3] < 200 or b[1] > dev.h - 180:
            continue
        if len(t) > 40:
            continue
        items.append({"name": t, "y": (b[1] + b[3]) // 2, "x": (b[0] + b[2]) // 2})
    # 按 y 去重（同一行可能有多个文本）
    items.sort(key=lambda i: i["y"])
    seen, out = set(), []
    for it in items:
        key = (it["name"], it["y"] // 60)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def open_conversation(dev, name):
    # 已经停在该聊天页就不折腾，避免误点标题进群设置
    try:
        cur = dev.dump_ui(2)
    except Exception:
        cur = None
    if cur is not None and name in "".join((n.get("text") or "") for n in nodes(cur)) \
            and find(cur, rid="editor"):
        return True
    # 把首页拉到前台（不用 force-stop，避免掉登录），再进消息页
    dev.shell(f"am start -n {PKG}/{HOME_ACTIVITY}")
    time.sleep(1.5)
    for _ in range(4):
        try:
            root = dev.dump_ui(2)
        except Exception:
            root = None
        texts = [((n.get("text") or "").strip()) for n in nodes(root)] if root is not None else []
        if "消息" in texts:
            break
        dev.shell("input keyevent 4")
        time.sleep(0.8)
    goto_messages(dev)
    root = dev.dump_ui()
    hit = None
    for n in find(root, text=name):
        b = bounds(n)
        if b and b[3] - b[1] < 200:
            hit = n
            break
    if hit is None:
        return False
    c = center(hit)
    dev.tap(*c)
    time.sleep(1.8)
    return True


def read_messages(dev, limit=30):
    root = dev.dump_ui()
    rows = []
    avatars = []
    skip_rid = ("sender_name", "message_time", "read_status",
                "active_tag", "relation_tag", "navi_", "title_tv")
    for n in nodes(root):
        b = bounds(n)
        if b is None:
            continue
        if b[1] < 210 or b[3] > dev.h - 260:
            continue
        cls = n.get("class", "")
        rid = n.get("resource-id", "")
        # 头像在左边=对方，在右边=自己；用它判断整段气泡归属
        if "avatar" in rid:
            avatars.append({"avatar": True, "y": (b[1] + b[3]) // 2,
                            "side": "me" if b[0] > dev.w // 2 else "other"})
            continue
        if cls.endswith("EditText"):
            continue
        t = (n.get("text") or "").strip()
        if t:
            # 昵称、时间、已读、标签都不是聊天内容
            if any(k in rid for k in skip_rid):
                continue
            rows.append({"text": t, "l": b[0], "r": b[2],
                         "y": (b[1] + b[3]) // 2, "x": (b[0] + b[2]) // 2})
            continue
        if "ImageView" in cls and (b[2] - b[0]) >= 90 and (b[3] - b[1]) >= 90:
            kind = ("video" if "photo_cover" in rid else
                    "sticker" if ("sticker" in rid or "emotion" in rid) else "image")
            label = {"video": "[视频]", "sticker": "[表情]", "image": "[图片]"}[kind]
            rows.append({"text": label, "kind": kind, "box": list(b), "l": b[0], "r": b[2],
                         "y": (b[1] + b[3]) // 2, "x": (b[0] + b[2]) // 2})
    rows.sort(key=lambda r: r["y"])
    # 合并同一气泡内的多行文本（媒体行不参与合并）
    merged = []
    for r in rows:
        if merged and not r.get("kind") and not merged[-1].get("kind") \
                and abs(r["y"] - merged[-1]["y"]) < 30 and abs(r["x"] - merged[-1]["x"]) < 120:
            merged[-1]["text"] += " " + r["text"]
        else:
            merged.append(dict(r))
    # 按 y 顺序遇到头像就切换归属，长气泡也能判断对
    timeline = sorted(merged + avatars, key=lambda r: r["y"])
    side = None
    for m in timeline:
        if m.get("avatar"):
            side = m["side"]
        elif side:
            m["side"] = side
        else:
            # 上方气泡已滚出屏幕、找不到头像时按“自己”处理，避免自问自答
            m["side"] = "me"
    out = [m for m in timeline if not m.get("avatar")]
    return out[-limit:]


def ensure_adb_ime(dev):
    """ADBKeyboard 不在前台时切回来，否则广播输入无效。"""
    try:
        cur = dev.shell("settings get secure default_input_method")
    except Exception:
        cur = ""
    if "adbkeyboard" not in cur:
        dev.shell("ime set com.android.adbkeyboard/.AdbIME")
        time.sleep(0.8)


def share_to_group(dev, group):
    """精选页刷新 -> 转发面板 -> 点「常分享」第一个群头像，快手内置分享作品。"""
    dev.shell(f"am start -n {PKG}/{HOME_ACTIVITY}")
    time.sleep(2.5)
    dev.tap(324, 1845)              # 精选
    time.sleep(3.0)
    dev.tap(324, 1845)              # 再点一次 = 刷新
    time.sleep(4.0)
    dev.tap(984, 1345)              # 右侧转发按钮
    time.sleep(3.0)
    dev.tap(146, 1012)              # 一键分享至 第一个（常分享 = 本群）
    time.sleep(3.5)
    dev.shell("input keyevent 4")
    time.sleep(1.5)
    return True


def send_text(dev, group, text):
    if not open_conversation(dev, group):
        raise RuntimeError(f"找不到群聊: {group}")
    ensure_adb_ime(dev)
    root = dev.dump_ui()
    box = find(root, cls="EditText") or find(root, rid="editor")
    if not box:
        vb = find(root, rid="voice_btn")
        if vb:
            dev.tap(*center(vb[0]))
            time.sleep(1.2)
            root = dev.dump_ui()
            box = find(root, cls="EditText") or find(root, rid="editor")
    for _ in range(2):
        if box:
            break
        ph = find(root, text="输入消息") or find(root, rid="editor") or find(root, rid="chat_input")
        if not ph:
            break
        dev.tap(*center(ph[0]))
        time.sleep(1.2)
        root = dev.dump_ui()
        box = find(root, cls="EditText") or find(root, rid="editor")
    if not box:
        raise RuntimeError("没找到输入框，可能未进入聊天页")
    dev.tap(*center(box[0]))
    time.sleep(0.5)
    dev.shell("am broadcast -a ADB_CLEAR_TEXT")
    time.sleep(0.3)
    dev.input_text(text)
    time.sleep(0.8)
    if text[:4] not in "".join((n.get("text") or "") for n in nodes(dev.dump_ui(3))):
        ensure_adb_ime(dev)
        dev.tap(*center(box[0]))
        dev.input_text(text)
        time.sleep(1.0)
    try:
        page = dev.dump_ui(3)
    except Exception:
        page = None
    btn = (find(page, rid="send_btn") or find(page, text="发送", exact=True)) if page is not None else []
    if btn:
        dev.tap(*center(btn[0]))
    else:
        dev.tap(int(dev.w * 0.89), dev.h - 90)
    time.sleep(1.0)
    return True


def sticker_panel(dev, group):
    """打开表情面板并截图，交给模型自己挑表情和坐标。"""
    if not open_conversation(dev, group):
        raise RuntimeError(f"找不到群聊: {group}")
    root = dev.dump_ui()
    emoji = find(root, rid="emotion_btn") or find(root, desc="表情") or find(root, text="表情")
    if emoji:
        dev.tap(*center(emoji[0]))
    else:
        dev.tap(96, dev.h - 105)
    time.sleep(1.8)
    return dev.screenshot()


def send_sticker(dev, group, index=0):
    if not open_conversation(dev, group):
        raise SystemExit(f"找不到群聊: {group}")
    root = dev.dump_ui()
    emoji = find(root, rid="emotion_btn") or find(root, desc="表情") or find(root, text="表情")
    if emoji:
        dev.tap(*center(emoji[0]))
    else:
        dev.tap(96, dev.h - 105)
    time.sleep(1.2)
    panel = dev.dump_ui()
    cells = []
    for n in nodes(panel):
        b = bounds(n)
        if not b:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if b[1] > dev.h * 0.55 and 60 <= w <= 220 and 60 <= h <= 220:
            cells.append(n)
    if not cells:
        raise SystemExit("表情面板没找到可点的表情")
    cells.sort(key=lambda n: (bounds(n)[1] // 80, bounds(n)[0]))
    target = cells[min(index, len(cells) - 1)]
    dev.tap(*center(target))
    time.sleep(0.8)
    return True


def grab_media(dev, group, mode="clip", seconds=8, max_seconds=180):
    """打开群里最新的视频/图片；视频会一直录到播放结束再停。group 传空表示当前已打开的聊天。"""
    if group:
        if not open_conversation(dev, group):
            raise SystemExit(f"找不到群聊: {group}")
    root = dev.dump_ui()
    cands = []
    ads = []
    for n in nodes(root):
        rid = n.get("resource-id", "")
        if "pendant" in rid or "ad_" in rid or "redpack" in rid or "coin" in rid:
            b = bounds(n)
            if b and b[2] > b[0] and b[3] > b[1]:
                ads.append(b)

    def in_ad(x, y):
        return any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in ads)

    for n in nodes(root):
        b = bounds(n)
        if not b:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if 250 <= w <= dev.w and 200 <= h <= dev.h * 0.7 and b[1] > 210:
            cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
            if in_ad(cx, cy):
                continue
            cands.append((b[1], n))
    if not cands:
        raise SystemExit("聊天里没有找到媒体消息")
    cands.sort(key=lambda x: x[0])
    dev.tap(*center(cands[-1][1]))
    time.sleep(2.0)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    if mode == "shot":
        return dev.screenshot()
    path = clip_until_end(dev, max_seconds)
    return compress(path)


def screencap_bytes(dev):
    r = subprocess.run([ADB, "-s", dev.serial, "exec-out", "screencap", "-p"],
                       capture_output=True, timeout=60)
    return r.stdout


# ---------------------------------------------------------------- 直接下载
def _pull_im_db(dev):
    """把当前账号的 IM 库拉到本地并返回 sqlite 连接。

    快手库是 WAL 模式，主库和 -wal 不是同一瞬间复制的，单独打开会看到旧消息。
    所以多拉几次，取「最新消息时间」最大的那一份，保证拿到的是最新的消息。
    """
    import sqlite3
    local_dir = os.path.join(BASE_DIR, "_db")
    os.makedirs(local_dir, exist_ok=True)
    for name in os.listdir(local_dir):
        if name.startswith("im_") and name.endswith((".db", ".db-wal", ".db-shm")):
            try:
                os.remove(os.path.join(local_dir, name))
            except Exception:
                pass

    def pull(tag):
        dst = os.path.join(local_dir, f"im_{tag}.db")
        if os.path.exists(dst):
            os.remove(dst)
        dev.shell("cp /data/data/com.smile.gifmaker/databases/2456878448imsdk.db* /sdcard/Download/ 2>/dev/null")
        for ext in ("", "-wal", "-shm"):
            subprocess.run([ADB, "-s", dev.serial, "pull",
                            f"/sdcard/Download/2456878448imsdk.db{ext}", dst + ext],
                           capture_output=True, timeout=180)
        if os.path.exists(dst) and os.path.getsize(dst) > 20000:
            return dst
        return None

    def newest(path):
        try:
            c = sqlite3.connect(path)
            v = c.execute("select max(createTime) from kwai_message").fetchone()[0] or 0
            c.close()
            return int(v)
        except Exception:
            return 0

    best = (0, None)
    for attempt in range(3):
        tag = f"{time.time_ns()}_{attempt}"
        db = pull(tag)
        if not db and attempt == 0 and ":" in dev.serial:
            subprocess.run([ADB, "connect", dev.serial], capture_output=True, timeout=30)
            time.sleep(2)
            db = pull(tag + "r")
        if not db:
            time.sleep(1)
            continue
        mt = newest(db)
        if mt > best[0]:
            best = (mt, db)
        if mt >= _DB_LAST_MAX.get(dev.serial, 0):
            break
        time.sleep(1)
    if not best[1]:
        raise RuntimeError(f"读不到快手消息库（{dev.serial}），检查模拟器和 adb 是否还连着")
    _DB_LAST_MAX[dev.serial] = max(_DB_LAST_MAX.get(dev.serial, 0), best[0])
    return sqlite3.connect(best[1])


_DB_LAST_MAX = {}


def _strings(blob):
    if not isinstance(blob, (bytes, bytearray)):
        return ""
    return " ".join(m.group().decode("ascii", "ignore")
                    for m in re.finditer(rb"[\x20-\x7e]{4,}", blob))


UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
PID_RE = re.compile(r"(?<!\d)(\d{17,20})(?!\d)")


def _web_mp4(photo_id, ua):
    """作品页解析真实 mp4 地址。"""
    import requests
    for url in (f"https://v.m.chenzhongtech.com/fw/photo/{photo_id}",
                f"https://m.gifshow.com/fw/photo/{photo_id}"):
        try:
            html = requests.get(url, headers={"User-Agent": ua}, timeout=25).text
        except Exception:
            continue
        hits = re.findall(r"https?://[^\s\"'\\]+?\.mp4[^\s\"'\\]*", html)
        if hits:
            return hits[0].replace("&amp;", "&")
    return None


def db_messages(dev, group=None, limit=30):
    """直接读本地消息库，不看界面：有新消息就能立刻发现。"""
    conn = _pull_im_db(dev)
    conn.row_factory = None
    gid = group
    if group:
        row = conn.execute("select groupId from kwai_group_info where groupName=?", (group,)).fetchone()
        if row:
            gid = row[0]
    sql = ("select _id, sender, isLocalMsg, outboundStatus, msgType, text, contentBytes, createTime "
           "from kwai_message")
    args = []
    if gid:
        sql += " where target=?"
        args.append(gid)
    sql += " order by createTime desc limit ?"
    args.append(limit)
    out = []
    for _id, sender, local, outbound, mtype, text, content, ctime in conn.execute(sql, args):
        blob = b""
        for v in (content, text):
            if isinstance(v, (bytes, bytearray)):
                blob += v
        s = _strings(blob)
        mp4 = re.findall(r"https?://[^\s\"'|\\]+?\.mp4[^\s\"'|\\]*", s)
        img = re.findall(r"https?://[^\s\"'|\\]+?\.(?:jpg|jpeg|png|webp|heif)[^\s\"'|\\]*", s)
        pid = PID_RE.search(s)
        plain = text if isinstance(text, str) else ""
        kind = "video" if (mp4 or pid) else ("image" if img else "")
        out.append({"id": _id, "sender": str(sender or ""),
                    "me": bool(local) or int(outbound or 0) == 1,
                    "msg_type": mtype, "text": plain, "kind": kind,
                    "pid": pid.group(1) if pid else "",
                    "url": (mp4 or img or [""])[0], "time": ctime / 1000 if ctime else 0})
    out.reverse()
    return {"messages": out, "last_id": max([m["id"] for m in out]) if out else 0}


def msg_media(dev, msg_id, max_mb=200):
    """按消息 id 取真实媒体文件：先按作品 id 解析出真 mp4，拿不到才退回封面图。"""
    conn = _pull_im_db(dev)
    row = conn.execute("select contentBytes, text from kwai_message where _id=?",
                       (str(msg_id),)).fetchone()
    if not row:
        row = conn.execute("select contentBytes, text from kwai_message where _id=?",
                           (int(msg_id),)).fetchone()
    if not row:
        return {"ok": False, "error": "消息不存在"}
    blob = b""
    for v in row:
        if isinstance(v, (bytes, bytearray)):
            blob += v
    s = _strings(blob)
    mp4 = re.findall(r"https?://[^\s\"'|\\]+?\.mp4[^\s\"'|\\]*", s)
    img = re.findall(r"https?://[^\s\"'|\\]+?\.(?:jpg|jpeg|png|webp|heif)[^\s\"'|\\]*", s)
    pid = PID_RE.search(s)
    pid = pid.group(1) if pid else ""
    errs = []
    for url in ([mp4[0]] if mp4 else []) + ([_web_mp4(pid, UA_MOBILE)] if pid else []):
        if not url:
            continue
        try:
            path = fetch_url(url, max_mb)
            dur = duration_of(path)
            if dur >= 1:
                return {"ok": True, "kind": "video", "file": path, "url": url,
                        "pid": pid, "duration": round(dur, 1),
                        "size": os.path.getsize(path)}
            os.remove(path)
            errs.append("不是视频")
        except Exception as e:
            errs.append(str(e)[:60])
    if img:
        try:
            path = fetch_url(img[0], max_mb)
            return {"ok": True, "kind": "image", "file": path, "url": img[0],
                    "pid": pid, "size": os.path.getsize(path)}
        except Exception as e:
            errs.append(str(e)[:60])
    return {"ok": False, "error": "；".join(errs) or "没有媒体直链"}


def db_media(dev, msg_id):
    """按消息 id 取出这条消息里的真实媒体直链（区分视频/封面图）。"""
    conn = _pull_im_db(dev)
    row = conn.execute("select contentBytes, text from kwai_message where _id=?", (msg_id,)).fetchone()
    if not row:
        return {"mp4": [], "image": []}
    blob = b""
    for v in row:
        if isinstance(v, (bytes, bytearray)):
            blob += v
    s = _strings(blob)
    mp4 = re.findall(r"https?://[^\s\"'|\\]+?\.mp4[^\s\"'|\\]*", s)
    img = re.findall(r"https?://[^\s\"'|\\]+?\.(?:jpg|jpeg|png|webp)[^\s\"'|\\]*", s)
    return {"mp4": mp4, "image": img}


def fetch_url(url, max_mb=200):
    """按直链下载媒体文件到本地；CDN 偶发断连时自动重试。"""
    import requests
    os.makedirs(MEDIA_DIR, exist_ok=True)
    ext = os.path.splitext(url.split("?")[0])[1] or ".mp4"
    last = None
    for attempt in range(3):
        dst = os.path.join(MEDIA_DIR, f"dl_{int(time.time() * 1000)}{ext}")
        try:
            with requests.get(url, stream=True, timeout=(15, 120),
                              headers={"User-Agent": UA_MOBILE,
                                       "Connection": "close"}) as r:
                r.raise_for_status()
                size = 0
                with open(dst, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        size += len(chunk)
                        if size > max_mb * 1024 * 1024:
                            f.close()
                            os.remove(dst)
                            raise RuntimeError("超过大小上限")
                        f.write(chunk)
            if os.path.getsize(dst) < 5000:
                os.remove(dst)
                raise RuntimeError("下到的文件太小")
            return dst
        except Exception as e:
            last = e
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except Exception:
                    pass
            if attempt < 2:
                time.sleep(2 + attempt * 3)
    raise RuntimeError(f"下载失败: {last}")


def download_media(dev, group=None, index=0, max_mb=200, only_others=True):
    """从消息库定位群里的视频/图片，直接下载原始文件（不录屏）。"""
    import requests
    UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
    conn = _pull_im_db(dev)
    conn.row_factory = None
    target = None
    if group:
        row = conn.execute("select groupId from kwai_group_info where groupName=?", (group,)).fetchone()
        target = row[0] if row else group
    sql = "select _id, msgType, contentBytes, text from kwai_message"
    args = []
    if target:
        sql += " where target=?"
        args.append(target)
    if only_others:
        sql += (" and " if target else " where ") + "(isLocalMsg=0 or isLocalMsg is null)"
    sql += " order by createTime desc limit 60"
    rows = conn.execute(sql, args).fetchall()
    os.makedirs(MEDIA_DIR, exist_ok=True)
    videos, images = [], []
    for _id, mtype, content, text in rows:
        blob = b""
        for v in (content, text):
            if isinstance(v, (bytes, bytearray)):
                blob += v
        s = _strings(blob)
        mp4 = re.findall(r"https?://[^\s\"'|\\]+?\.mp4[^\s\"'|\\]*", s)
        if mp4:
            videos.append(mp4[0])
        pid = re.search(r"(?<!\d)(\d{19})(?!\d)", s)
        if pid:
            videos.append(("photo", pid.group(1)))
        for u in re.findall(r"https?://[^\s\"'|\\]+?\.(?:jpg|jpeg|png|webp|heif)", s):
            images.append(u)
    if not videos and not images:
        raise SystemExit("这个会话里没找到可下载的媒体")

    def save(url, name):
        r = requests.get(url, headers={"User-Agent": UA}, timeout=120, stream=True)
        path = os.path.join(MEDIA_DIR, name)
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        return path

    if videos:
        item = videos[min(index, len(videos) - 1)]
        if isinstance(item, tuple):
            url = _web_mp4(item[1], UA)
            if not url:
                raise SystemExit("作品页没解析到 mp4 地址")
        else:
            url = item
        path = save(url, f"dl_{int(time.time())}.mp4")
        if os.path.getsize(path) < 5000:
            raise SystemExit("下载到的文件异常")
        return compress(path, max_mb=max_mb) if os.path.getsize(path) / 1048576 > max_mb else path
    url = images[min(index, len(images) - 1)]
    return save(url, f"dl_{int(time.time())}.jpg")


# ---------------------------------------------------------------- 金币（已移除：误触悬浮件）


def clip_until_end(dev, max_seconds=180, stable_needed=2, interval=1.0):
    """录屏，检测画面静止（播放结束/停在最后一帧）后自动停止。"""
    from io import BytesIO
    from PIL import Image
    import numpy as np

    remote = "/sdcard/ks_chat.mp4"
    dev.shell(f"rm -f {remote}")
    proc = subprocess.Popen([ADB, "-s", dev.serial, "shell", "screenrecord",
                             "--bit-rate", "12000000", remote],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    prev, stable, start, history = None, 0, time.time(), []
    while time.time() - start < max_seconds:
        time.sleep(interval)
        try:
            img = Image.open(BytesIO(screencap_bytes(dev))).convert("L").resize((64, 64))
        except Exception:
            continue
        arr = np.asarray(img, dtype=np.int16)
        if prev is not None:
            diff = float(np.abs(arr - prev).mean())
            stable = stable + 1 if diff < 1.5 else 0
            if stable >= stable_needed and time.time() - start > 4:
                break
            # 视频循环/回到开头：和较早的画面几乎一致
            if time.time() - start > 4 and history:
                if min(float(np.abs(arr - h).mean()) for h in history[:-2] or history) < 1.2:
                    break
        prev = arr
        history.append(arr)
        if len(history) > 120:
            history.pop(0)
    dev.shell("pkill -INT screenrecord")
    time.sleep(1.5)
    proc.terminate()
    out = os.path.join(MEDIA_DIR, f"chat_{int(time.time())}.mp4")
    subprocess.run([ADB, "-s", dev.serial, "pull", remote, out],
                   capture_output=True, timeout=180)
    if not os.path.exists(out) or os.path.getsize(out) < 1000:
        raise SystemExit("录屏失败")
    return out


# ---------------------------------------------------------------- 录屏/压缩
def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("未找到 ffmpeg，请安装并加入 PATH")
    return exe


def duration_of(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, timeout=60)
        return float(out.stdout.decode().strip())
    except Exception:
        return 0.0


def record_screen(dev, seconds, out=None):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    out = out or os.path.join(MEDIA_DIR, f"rec_{int(time.time())}.mp4")
    remote = "/sdcard/ks_rec.mp4"
    chunk = 170
    tmp = []
    left = seconds
    i = 0
    while left > 0:
        seg = min(chunk, left)
        dev.shell(f"rm -f {remote}.{i}")
        dev.shell(f"screenrecord --bit-rate 12000000 --time-limit {seg} {remote}.{i}", timeout=seg + 40)
        local = os.path.join(MEDIA_DIR, f"_seg{i}.mp4")
        subprocess.run([ADB, "-s", dev.serial, "pull", f"{remote}.{i}", local],
                       capture_output=True, timeout=120)
        if os.path.exists(local) and os.path.getsize(local) > 1000:
            tmp.append(local)
        left -= seg
        i += 1
    if not tmp:
        raise SystemExit("录屏失败")
    if len(tmp) == 1:
        shutil.move(tmp[0], out)
    else:
        lst = os.path.join(MEDIA_DIR, "_list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for p in tmp:
                f.write(f"file '{p.replace(os.sep, '/')}'\n")
        subprocess.run([_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", out], capture_output=True, timeout=600)
        for p in tmp:
            os.remove(p)
    return out


def compress(src, out=None, max_mb=200):
    """压到 max_mb 以内；优先保留文字清晰度（码率优先，其次降分辨率）。"""
    ff = _ffmpeg()
    size_mb = os.path.getsize(src) / 1024 / 1024
    out = out or os.path.splitext(src)[0] + f"_under{max_mb}MB.mp4"
    if size_mb <= max_mb:
        if os.path.abspath(out) != os.path.abspath(src):
            shutil.copy(src, out)
        return out
    dur = duration_of(src) or 1
    total_kbps = max_mb * 0.92 * 8 * 1024 / dur
    vk = int(max(500, total_kbps - 128))
    scale = "trunc(iw/2)*2:trunc(ih/2)*2" if vk >= 2200 else "scale=-2:720"
    for attempt in range(3):
        cmd = [ff, "-y", "-i", src, "-c:v", "libx264", "-preset", "medium",
               "-b:v", f"{vk}k", "-maxrate", f"{int(vk * 1.2)}k", "-bufsize", f"{vk * 2}k",
               "-vf", scale, "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out]
        subprocess.run(cmd, capture_output=True, timeout=3600)
        if not os.path.exists(out):
            raise SystemExit("ffmpeg 压缩失败")
        if os.path.getsize(out) / 1024 / 1024 <= max_mb:
            return out
        vk = int(vk * 0.8)
        if vk < 1200:
            scale = "scale=-2:720"
    return out


# ---------------------------------------------------------------- 刷视频
def backup_data(dev, out=None):
    """备份登录态（数据库/配置/files，排除大缓存），掉登录可直接还原。"""
    out = out or os.path.join(BASE_DIR, "ks_login_backup.tgz")
    cmd = ("tar czf - -C /data/data/com.smile.gifmaker databases shared_prefs files "
           "--exclude=files/image_cache --exclude=files/yoda_offline_package "
           "--exclude=files/magic_emoji_resource --exclude=files/photo_map "
           "--exclude=files/.frame_upload_image_info")
    r = subprocess.run([ADB, "-s", dev.serial, "exec-out"] + cmd.split(),
                       capture_output=True, timeout=3600)
    with open(out, "wb") as f:
        f.write(r.stdout)
    if os.path.getsize(out) < 1024:
        raise SystemExit("备份失败，检查 root 是否可用")
    return out


def restore_data(dev, path):
    """还原备份（会先停止快手进程）。"""
    remote = "/sdcard/ks_restore.tgz"
    subprocess.run([ADB, "-s", dev.serial, "push", path, remote], capture_output=True, timeout=600)
    dev.shell(f"am force-stop {PKG}")
    uid = dev.shell("stat -c %u /data/data/com.smile.gifmaker").strip() or "u0_a59"
    dev.shell(f"rm -rf /data/data/com.smile.gifmaker/databases /data/data/com.smile.gifmaker/shared_prefs")
    dev.shell(f"tar xzf {remote} -C /data/data/com.smile.gifmaker")
    dev.shell(f"chown -R {uid}:{uid} /data/data/com.smile.gifmaker")
    dev.shell("restorecon -R /data/data/com.smile.gifmaker 2>/dev/null")
    dev.launch()
    return True


def is_logged_in(dev):
    try:
        root = dev.dump_ui(2)
    except Exception:
        return None
    if find(root, text="登录后更精彩") or find(root, rid="phone_et") or find(root, rid="btn_onekey_login"):
        return False
    return True


def watch(dev, minutes=0, like=0.0, min_dwell=3.0, max_dwell=9.0):
    goto_home(dev)
    dev.tap(*TAB_HOME)   # 关掉登录弹窗后可能停在「精选」，再回一次首页
    time.sleep(1.2)
    end = time.time() + minutes * 60 if minutes else float("inf")
    x = dev.w // 2
    count = 0
    while time.time() < end:
        if like and random.random() < like:
            dev.tap(dev.w - 84, int(dev.h * 0.62))
            time.sleep(0.2)
        dev.swipe(x, int(dev.h * 0.78), x, int(dev.h * 0.20), 200)
        count += 1
        time.sleep(random.uniform(min_dwell, max_dwell))
    return count


# ---------------------------------------------------------------- 配置/状态
def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    cfg = {
        "poll_seconds": 8,
        "rules": [
            {"contains": ["在吗", "你好"], "reply": "在的，刚看到"},
            {"contains": ["哈哈"], "reply": "哈哈哈哈"},
            {"contains": ["视频", "图片"], "reply": "我看看"}
        ],
        "agitate": ["有人一起刷快手吗", "这个视频太好笑了", "潜水的人都出来"],
        "agitate_seconds": 600,
        "llm": {"enabled": False, "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": "", "model": "glm-4.1-flash", "persona": "你在快手里像个普通群友，回话简短口语化"}
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": {}}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def _key(group, text):
    return hashlib.md5(f"{group}|{text}".encode()).hexdigest()


# ---------------------------------------------------------------- 长期记忆库
MEM_PATH = os.path.join(BASE_DIR, "memory.db")


def mem_init():
    import sqlite3
    c = sqlite3.connect(MEM_PATH)
    c.execute("""create table if not exists messages(
        id integer primary key autoincrement,
        grp text, ts integer, side text, sender text, text text, uniq text unique)""")
    c.execute("create index if not exists idx_grp on messages(grp, ts)")
    c.commit()
    return c


def mem_add(group, msgs):
    c = mem_init()
    n = 0
    for m in msgs:
        u = _key(group, f"{m.get('sender','')}|{m['text']}")
        try:
            c.execute("insert into messages(grp,ts,side,sender,text,uniq) values(?,?,?,?,?,?)",
                      (group, int(time.time()), m.get("side", ""), m.get("sender", ""),
                       m["text"], u))
            n += 1
        except Exception:
            pass
    c.commit()
    c.close()
    return n


def _grams(s):
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def mem_search(group, query, limit=20):
    """关键词/字二元组打分召回，替代超出 128K 后丢失的上下文。"""
    c = mem_init()
    rows = c.execute("select ts,side,sender,text from messages where grp=? order by id desc",
                     (group,)).fetchall()
    c.close()
    q = _grams(query)
    scored = []
    for i, (ts, side, sender, text) in enumerate(rows):
        g = _grams(text)
        overlap = len(q & g)
        score = overlap / (len(q) + 1) + 0.15 * max(0, 1 - i / 300)
        scored.append((score, ts, side, sender, text))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [dict(ts=s[1], side=s[2], sender=s[3], text=s[4]) for s in scored[:limit]]


def context_for_llm(group, query, budget=12000):
    """把「最近对话」+「历史相关片段」拼进预算内，突破模型上下文上限。"""
    c = mem_init()
    recent = c.execute("select ts,side,sender,text from messages where grp=? order by id desc limit 60",
                       (group,)).fetchall()
    c.close()
    recent = list(reversed(recent))
    parts, used = [], 0
    for ts, side, sender, text in recent:
        line = f"[{time.strftime('%m-%d %H:%M', time.localtime(ts))}] {side}: {text}"
        if used + len(line) > budget:
            break
        parts.append(line)
        used += len(line)
    hits = mem_search(group, query, 25)
    old = [h for h in hits if h["text"] not in "\n".join(parts)]
    if old:
        parts.append("—— 历史相关片段 ——")
        for h in old:
            line = f"{h['side']}: {h['text']}"
            if used + len(line) > budget:
                break
            parts.append(line)
            used += len(line)
    return "\n".join(parts)


def mem_stats():
    c = mem_init()
    rows = c.execute("select grp,count(*) from messages group by grp order by 2 desc").fetchall()
    total = c.execute("select count(*) from messages").fetchone()[0]
    c.close()
    return total, rows


def sync_memory(dev, group=""):
    """把聊天记录灌进本地记忆库（增量去重）。"""
    added = 0
    groups = [group] if group else [c["name"] for c in list_conversations(dev)]
    for g in groups:
        if not open_conversation(dev, g):
            continue
        msgs = read_messages(dev, 40)
        added += mem_add(g, msgs)
        print(f"{g}: {len(msgs)} 条")
    return added


def ask_question(cfg, group, question):
    """用记忆库检索 + GLM 回答，不受 128K 限制。"""
    ctx = context_for_llm(group, question, 20000)
    llm = cfg.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key"):
        return "未配置 GLM（rules.json 里 llm.enabled/api_key），以下是检索到的上下文：\n" + ctx
    try:
        import requests
    except ImportError:
        return "缺少 requests"
    body = {
        "model": llm.get("model", "glm-4.1-flash"),
        "messages": [
            {"role": "system", "content": "你熟悉这个快手群聊的长期记录，回答要简洁准确，基于给定记录。"},
            {"role": "user", "content": f"群聊记录：\n{ctx}\n\n问题：{question}"}
        ],
        "temperature": 0.3,
    }
    r = requests.post(llm["base_url"].rstrip("/") + "/chat/completions",
                      headers={"Authorization": f"Bearer {llm['api_key']}",
                               "Content-Type": "application/json"},
                      json=body, timeout=60)
    return r.json()["choices"][0]["message"]["content"].strip()


def llm_reply(cfg, group, msgs):
    llm = cfg.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key"):
        return None
    try:
        import requests
    except ImportError:
        return None
    query = msgs[-1]["text"] if msgs else ""
    ctx = context_for_llm(group, query, 12000)
    body = {
        "model": llm.get("model", "glm-4.1-flash"),
        "messages": [
            {"role": "system", "content": llm.get("persona", "你是普通群友，回复简短")},
            {"role": "user", "content": (f"群聊「{group}」的历史与最近消息（按时间/相关度整理）：\n{ctx}\n\n"
                                         "只输出你要发的一句话。")}
        ],
        "temperature": 0.9,
    }
    try:
        r = requests.post(llm["base_url"].rstrip("/") + "/chat/completions",
                          headers={"Authorization": f"Bearer {llm['api_key']}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=40)
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[llm] 调用失败: {e}", file=sys.stderr)
        return None


def auto_loop(dev, group=None, once=False):
    cfg = load_config()
    st = load_state()
    last_agitate = 0
    while True:
        groups = [group] if group else [c["name"] for c in list_conversations(dev)]
        for g in groups:
            if not open_conversation(dev, g):
                continue
            msgs = read_messages(dev, 20)
            mem_add(g, msgs)
            news = [m for m in msgs if m["side"] == "other" and _key(g, m["text"]) not in st["seen"]]
            for m in news:
                st["seen"][_key(g, m["text"])] = int(time.time())
            if news:
                reply = None
                for m in news:
                    for rule in cfg.get("rules", []):
                        if any(k in m["text"] for k in rule.get("contains", [])):
                            reply = rule.get("reply")
                            break
                    if reply:
                        break
                if not reply and cfg.get("llm", {}).get("enabled"):
                    reply = llm_reply(cfg, g, msgs)
                if reply:
                    try:
                        send_text(dev, g, reply)
                    except Exception as e:
                        print(f"[send] {g}: {e}", file=sys.stderr)
            now = time.time()
            if cfg.get("agitate") and now - last_agitate > cfg.get("agitate_seconds", 600):
                try:
                    send_text(dev, g, random.choice(cfg["agitate"]))
                    last_agitate = now
                except Exception as e:
                    print(f"[agitate] {e}", file=sys.stderr)
        save_state(st)
        if once:
            return
        time.sleep(cfg.get("poll_seconds", 8))


# ---------------------------------------------------------------- HTTP API
DEV_LOCK = threading.RLock()


class Api:
    def __init__(self, dev):
        self.dev = dev

    def status(self):
        return {"serial": self.dev.serial, "screen": f"{self.dev.w}x{self.dev.h}",
                "root": bool(getattr(self.dev, "root", False)),
                "adbkeyboard": kb_installed(self.dev.serial)}

    def groups(self):
        return {"groups": [c["name"] for c in list_conversations(self.dev)]}

    def read(self, group, n=20):
        if not open_conversation(self.dev, group):
            raise RuntimeError("群聊不存在")
        try:
            return {"messages": read_messages(self.dev, n)}
        except Exception as e:
            # 视频播放、动画不断刷新时 uiautomator 会拿不到界面，回到会话再试一次
            time.sleep(1.2)
            try:
                open_conversation(self.dev, group)
                return {"messages": read_messages(self.dev, n),
                        "warn": f"重试后恢复（{e}）"}
            except Exception:
                return {"messages": [], "warn": f"本轮界面读取失败：{e}"}

    def send(self, group, text):
        send_text(self.dev, group, text)
        return {"ok": True}

    def sticker(self, group, index=0):
        send_sticker(self.dev, group, index)
        return {"ok": True}

    def sticker_panel(self, group):
        return {"file": sticker_panel(self.dev, group)}

    def db(self, group=None, limit=30):
        return db_messages(self.dev, group, limit)

    def dbmedia(self, msg_id):
        return db_media(self.dev, msg_id)

    def msgmedia(self, msg_id, max_mb=200):
        return msg_media(self.dev, msg_id, max_mb)

    def fetch(self, url, max_mb=200):
        return {"file": fetch_url(url, max_mb)}

    def tap(self, x, y):
        self.dev.tap(int(x), int(y))
        return {"ok": True, "point": [int(x), int(y)]}

    def watch(self, minutes=5, like=0.05):
        return {"count": watch(self.dev, minutes, like)}

    def record(self, seconds=30, max_mb=200):
        p = record_screen(self.dev, seconds)
        return {"file": compress(p, max_mb=max_mb)}

    def media(self, group, mode="shot", seconds=8):
        return {"file": grab_media(self.dev, group, mode, seconds)}

    def compress(self, path, max_mb=200):
        return {"file": compress(path, max_mb=max_mb)}

    def download(self, group, index=0, max_mb=200):
        return {"file": download_media(self.dev, group, index, max_mb)}

    def shot(self, box=None):
        """截当前屏幕；box=[x1,y1,x2,y2] 时裁剪该区域。"""
        raw = self.dev.screenshot()
        if not box:
            return {"file": raw, "box": None}
        x1, y1, x2, y2 = [int(v) for v in box]
        w, h = max(2, x2 - x1), max(2, y2 - y1)
        out = raw.replace(".png", f"_crop.png")
        subprocess.run(["ffmpeg", "-y", "-i", raw, "-vf",
                        f"crop={w}:{h}:{max(0, x1)}:{max(0, y1)}", out],
                       capture_output=True, timeout=60)
        return {"file": out if os.path.exists(out) else raw, "box": [x1, y1, x2, y2]}

    def share(self, group):
        """刷新推荐页，把当前视频用快手内置转发分享到群里。"""
        share_to_group(self.dev, group)
        return {"ok": True, "group": group}


def serve(dev, port=8765):
    api = Api(dev)
    hits = {}

    class H(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                hits[self.path.split("?")[0]] = hits.get(self.path.split("?")[0], 0) + 1
                if self.path.startswith("/stats"):
                    self._send(hits)
                elif self.path.startswith("/status"):
                    self._send(api.status())
                elif self.path.startswith("/groups"):
                    self._send(api.groups())
                elif self.path.startswith("/help"):
                    self._send({"endpoints": ["GET /status", "GET /groups",
                                              "POST /read {group,n}", "POST /send {group,text}",
                                              "POST /sticker {group,index}",
                                              "POST /watch {minutes,like}",
                                              "POST /record {seconds,max_mb}",
                                              "POST /media {group,mode,seconds}",
                                              "POST /compress {path,max_mb}"]})
                else:
                    self._send({"error": "not found"}, 404)
            except Exception as e:
                self._send({"error": str(e)}, 500)

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n) or b"{}")
                p = self.path.split("?")[0].rstrip("/")
                hits[p] = hits.get(p, 0) + 1
                with DEV_LOCK:
                    return self._dispatch(p, data)
            except Exception as e:
                self._send({"error": str(e)}, 500)

        def _dispatch(self, p, data):
            if True:
                if p == "/read":
                    self._send(api.read(data.get("group", ""), int(data.get("n", 20))))
                elif p == "/send":
                    self._send(api.send(data.get("group", ""), data.get("text", "")))
                elif p == "/sticker":
                    self._send(api.sticker(data.get("group", ""), int(data.get("index", 0))))
                elif p == "/watch":
                    self._send(api.watch(float(data.get("minutes", 5)), float(data.get("like", 0.05))))
                elif p == "/record":
                    self._send(api.record(int(data.get("seconds", 30)), int(data.get("max_mb", 200))))
                elif p == "/media":
                    self._send(api.media(data.get("group", ""), data.get("mode", "shot"),
                                         int(data.get("seconds", 8))))
                elif p == "/compress":
                    self._send(api.compress(data["path"], int(data.get("max_mb", 200))))
                elif p == "/download":
                    self._send(api.download(data.get("group", ""), int(data.get("index", 0)),
                                            int(data.get("max_mb", 200))))
                elif p == "/shot":
                    self._send(api.shot(data.get("box")))
                elif p == "/share":
                    self._send(api.share(data.get("group", "")))
                elif p == "/stickers":
                    self._send(api.sticker_panel(data.get("group", "")))
                elif p == "/tap":
                    self._send(api.tap(data.get("x", 0), data.get("y", 0)))
                elif p == "/db":
                    self._send(api.db(data.get("group"), int(data.get("limit") or 30)))
                elif p == "/fetch":
                    self._send(api.fetch(data.get("url", ""), int(data.get("max_mb") or 200)))
                elif p == "/dbmedia":
                    self._send(api.dbmedia(data.get("id")))
                elif p == "/msgmedia":
                    self._send(api.msgmedia(data.get("id"), int(data.get("max_mb") or 200)))
                else:
                    self._send({"error": "not found"}, 404)

        def log_message(self, *a):
            pass

    print(f"API 已启动: http://127.0.0.1:{port}   (GET /help 查看接口)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description="快手 ADB 自动化插件")
    p.add_argument("--serial", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("open")
    sub.add_parser("install-kb")
    lg = sub.add_parser("login")
    lg.add_argument("--wait", action="store_true")
    sub.add_parser("ui")

    st = sub.add_parser("shot")
    st.add_argument("--out", default=None)

    w = sub.add_parser("watch")
    w.add_argument("--minutes", type=float, default=0)
    w.add_argument("--like", type=float, default=0.03)
    w.add_argument("--min-dwell", type=float, default=3)
    w.add_argument("--max-dwell", type=float, default=9)

    sub.add_parser("msgs")

    rd = sub.add_parser("read")
    rd.add_argument("group")
    rd.add_argument("-n", type=int, default=20)

    sd = sub.add_parser("send")
    sd.add_argument("group")
    sd.add_argument("text")

    sk = sub.add_parser("sticker")
    sk.add_argument("group")
    sk.add_argument("--index", type=int, default=0)

    md = sub.add_parser("media")
    md.add_argument("group", nargs="?", default="", help="群名；留空=当前已打开的聊天")
    md.add_argument("--mode", choices=["shot", "clip"], default="clip")
    md.add_argument("--seconds", type=int, default=8)
    md.add_argument("--max-seconds", type=int, default=180)

    rec = sub.add_parser("record")
    rec.add_argument("--seconds", type=int, default=30)
    rec.add_argument("--out", default=None)
    rec.add_argument("--max-mb", type=int, default=200)

    dl = sub.add_parser("download")
    dl.add_argument("group", nargs="?", default="")
    dl.add_argument("--index", type=int, default=0)
    dl.add_argument("--max-mb", type=int, default=200)

    mem = sub.add_parser("memory")
    mem.add_argument("action", choices=["sync", "search", "stats"])
    mem.add_argument("group", nargs="?", default="")
    mem.add_argument("query", nargs="?", default="")

    ask = sub.add_parser("ask")
    ask.add_argument("group")
    ask.add_argument("question")

    cp = sub.add_parser("compress")
    cp.add_argument("src")
    cp.add_argument("--out", default=None)
    cp.add_argument("--max-mb", type=int, default=200)

    au = sub.add_parser("auto")
    au.add_argument("--group", default=None)
    au.add_argument("--once", action="store_true")

    ap = sub.add_parser("api")
    ap.add_argument("--port", type=int, default=8765)

    bk = sub.add_parser("backup")
    bk.add_argument("--out", default=None)

    rs = sub.add_parser("restore")
    rs.add_argument("file")

    args = p.parse_args()
    dev = Device(args.serial)

    if args.cmd == "status":
        print(json.dumps({"serial": dev.serial, "screen": f"{dev.w}x{dev.h}",
                          "adbkeyboard": kb_installed(dev.serial)}, ensure_ascii=False, indent=2))
    elif args.cmd == "open":
        dev.launch()
    elif args.cmd == "install-kb":
        subprocess.run([ADB, "-s", dev.serial, "install", "-r", KB_APK], capture_output=True, timeout=180)
        print("ADBKeyboard 安装完成" if kb_installed(dev.serial) else "安装失败")
    elif args.cmd == "login":
        goto_messages(dev)
        print("已打开登录页，请在模拟器完成登录")
        if args.wait:
            while True:
                time.sleep(5)
                try:
                    root = dev.dump_ui(3)
                except Exception:
                    continue
                if not find(root, text="登录后更精彩") and not find(root, rid="phone_et"):
                    print("检测到已登录")
                    break
    elif args.cmd == "ui":
        root = dev.dump_ui()
        for n in nodes(root):
            t = text_of(n)
            if not t:
                continue
            print(f"{t!r:<40} {n.get('class','').split('.')[-1]:<16} {n.get('resource-id','')} {n.get('bounds','')}")
    elif args.cmd == "shot":
        print(dev.screenshot(args.out))
    elif args.cmd == "watch":
        n = watch(dev, args.minutes, args.like, args.min_dwell, args.max_dwell)
        print(f"刷了 {n} 个")
    elif args.cmd == "msgs":
        for c in list_conversations(dev):
            print(c["name"])
    elif args.cmd == "read":
        if not open_conversation(dev, args.group):
            raise SystemExit("群聊不存在")
        for m in read_messages(dev, args.n):
            print(f"[{m['side']}] {m['text']}")
    elif args.cmd == "send":
        send_text(dev, args.group, args.text)
        print("已发送")
    elif args.cmd == "sticker":
        send_sticker(dev, args.group, args.index)
        print("已发表情")
    elif args.cmd == "media":
        print(grab_media(dev, args.group, args.mode, args.seconds, args.max_seconds))
    elif args.cmd == "record":
        path = record_screen(dev, args.seconds, args.out)
        path = compress(path, max_mb=args.max_mb)
        print(path)
    elif args.cmd == "download":
        print(download_media(dev, args.group, args.index, args.max_mb))
    elif args.cmd == "memory":
        if args.action == "sync":
            print(f"新增 {sync_memory(dev, args.group)} 条")
        elif args.action == "search":
            for m in mem_search(args.group, args.query or "", 20):
                print(f"{m['side']}: {m['text']}")
        else:
            total, rows = mem_stats()
            print(f"共 {total} 条")
            for g, n in rows:
                print(f"  {g}: {n}")
    elif args.cmd == "ask":
        print(ask_question(load_config(), args.group, args.question))
    elif args.cmd == "compress":
        print(compress(args.src, args.out, args.max_mb))
    elif args.cmd == "auto":
        auto_loop(dev, args.group, args.once)
    elif args.cmd == "api":
        serve(dev, args.port)
    elif args.cmd == "backup":
        print(backup_data(dev, args.out))
    elif args.cmd == "restore":
        restore_data(dev, args.file)
        print("已还原")


if __name__ == "__main__":
    main()
