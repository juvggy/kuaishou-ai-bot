"""客服机器人平台：网页对话 + 图片/视频理解 + 超长上下文 + 多会话。

只用标准库，依赖 ffmpeg（已在 PATH 中）。
运行：python app.py   然后浏览器打开 http://127.0.0.1:8800
"""
import atexit
import base64
import cgi
import json
import mimetypes
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CONV_DIR = os.path.join(DATA, "conversations")
UP_DIR = os.path.join(DATA, "uploads")
CFG_PATH = os.path.join(ROOT, "config.json")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".flv"}
LOCK = threading.Lock()

for d in (DATA, CONV_DIR, UP_DIR):
    os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------- 配置
DEFAULTS = {
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "",
    "model": "glm-4v-flash",
    "vision_model": "glm-4v-flash",
    "max_file_mb": 200,
    "video_inline_seconds": 3600,
    "keep_recent_messages": 16,
    "summarize_after": 24,
    "full_context": False,
    "context_mode": "retrieval",
    "summary_model": "glm-4-flash",
    "max_frames": 8,
    "port": 8800,
    "ks_enabled": False,
    "ks_bridge": "http://127.0.0.1:8765",
    "ks_groups": [],
    "ks_prefix": "",
    "ks_poll": 8,
    "ks_stickers": False,
    "ks_notice": "",
    "ks_tools": True,
    "ks_idle_min": 25,
    "ks_share_link": True,
    "ks_share_min": 8,
    "ks_share_count": 2,
    "ks_think_min": 30,
    "ks_think_on": True,
    "ks_think_prompt": "用一句口语化的感想说说你自己对这件事的看法，不超过 30 字，只输出这一句。",
    "ks_source": "ui",
    "ks_auto_video": True,
    "system_prompt": "你在一个快手同学群里聊天，你就是个普通同学，不是客服、不是助手、不是AI。"
                     "说话短、口语、随意，像微信聊天：一条一般 5~20 个字，能一句话说完就不写两句。"
                     "不要长篇大论，不要分点列条，不要书面语，不要说“作为AI/我理解/希望对你有帮助/以上”。"
                     "可以附和、吐槽、开玩笑、反问，偶尔用“哈哈、嗯、啊、确实、行吧”。"
                     "不用每句都加表情符号。别人问你什么就正常答，没兴趣的话题少说两句就行。",
}


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("[config] 读取失败:", e)
    return cfg


def save_cfg(patch):
    cfg = load_cfg()
    cfg.update({k: v for k, v in patch.items() if k in DEFAULTS})
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# ---------------------------------------------------------------- 存储
def session_path(sid):
    return os.path.join(CONV_DIR, sid + ".json")


def new_session(title="新对话"):
    sid = uuid.uuid4().hex[:12]
    convo = {"id": sid, "title": title, "created": time.time(), "updated": time.time(),
             "summary": "", "messages": []}
    write_session(convo)
    return convo


def read_session(sid):
    p = session_path(sid)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def write_session(convo):
    convo["updated"] = time.time()
    with open(session_path(convo["id"]), "w", encoding="utf-8") as f:
        json.dump(convo, f, ensure_ascii=False, indent=1)


def list_sessions():
    out = []
    for name in os.listdir(CONV_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(CONV_DIR, name), "r", encoding="utf-8") as f:
                c = json.load(f)
            out.append({"id": c["id"], "title": c.get("title", "新对话"),
                        "updated": c.get("updated", 0),
                        "count": len(c.get("messages", []))})
        except Exception:
            continue
    return sorted(out, key=lambda x: x["updated"], reverse=True)


# ---------------------------------------------------------------- 媒体处理
def human(n):
    return f"{n / 1024 / 1024:.1f}MB" if n >= 1024 * 1024 else f"{n / 1024:.0f}KB"


def probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0


def compress_video(src, dst, limit_bytes):
    """超过上限就压到 720p 并逐步提高 CRF，直到低于上限。"""
    plan = [28, 32, 36, 40]
    for crf in plan:
        cmd = ["ffmpeg", "-y", "-i", src,
               "-vf", "scale='min(1280,iw)':-2",
               "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
               "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst]
        subprocess.run(cmd, capture_output=True, timeout=3600)
        if os.path.exists(dst) and os.path.getsize(dst) <= limit_bytes:
            return dst, crf
        if os.path.exists(dst):
            os.remove(dst)
    # 兜底：允许 480p
    subprocess.run(["ffmpeg", "-y", "-i", src, "-vf", "scale=-2:480",
                    "-c:v", "libx264", "-crf", "34", "-preset", "veryfast",
                    "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", dst],
                   capture_output=True, timeout=3600)
    return dst, 34


def extract_frames(path, out_dir, count=8):
    dur = probe_duration(path) or 0
    frames = []
    if dur <= 0:
        count = 1
    for i in range(count):
        t = (dur * (i + 0.5) / count) if dur else 0
        out = os.path.join(out_dir, f"f{i + 1}.jpg")
        subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", path,
                        "-frames:v", "1", "-vf", "scale='min(960,iw)':-2", out],
                       capture_output=True, timeout=120)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            frames.append(out)
    return frames


def handle_upload(filename, data, cfg):
    """保存文件；视频超限先压缩，再抽帧。"""
    ext = os.path.splitext(filename)[1].lower()
    uid = uuid.uuid4().hex[:10]
    safe = re.sub(r"[^\w\u4e00-\u9fa5.\-]", "_", os.path.basename(filename)) or f"file{ext}"
    dst_dir = os.path.join(UP_DIR, uid)
    os.makedirs(dst_dir, exist_ok=True)
    src = os.path.join(dst_dir, safe)
    with open(src, "wb") as f:
        f.write(data)

    size = len(data)
    info = {"id": uid, "name": safe, "size": size, "size_text": human(size),
            "kind": "file", "url": "", "frames": [], "note": ""}

    if ext in IMAGE_EXT:
        info["kind"] = "image"
        info["url"] = f"/u/{uid}/{quote(safe)}"
        return info

    if ext in VIDEO_EXT:
        info["kind"] = "video"
        limit = int(cfg.get("max_file_mb", 200)) * 1024 * 1024
        playable = src
        if size > limit:
            out = os.path.join(dst_dir, "compressed.mp4")
            playable, crf = compress_video(src, out, limit)
            new_size = os.path.getsize(playable)
            info["note"] = (f"原视频 {human(size)} 超过 {cfg.get('max_file_mb')}MB，"
                            f"已压缩为 {human(new_size)}（CRF {crf}）")
            info["compressed_size"] = new_size
            info["compressed_size_text"] = human(new_size)
            info["size"] = new_size
            info["size_text"] = human(new_size)
        info["url"] = f"/u/{uid}/{quote(os.path.basename(playable))}"
        info["frames"] = extract_frames(playable, dst_dir, int(cfg.get("max_frames", 8)))
        info["duration"] = round(probe_duration(playable), 1)
        return info

    info["url"] = f"/u/{uid}/{quote(safe)}"
    return info


# ---------------------------------------------------------------- 模型
def b64_image(path):
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def b64_video(path):
    mime = mimetypes.guess_type(path)[0] or "video/mp4"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def model_content(msg, inline_video=False):
    """把一条历史消息还原成 OpenAI 多模态格式。
    inline_video=True 时视频直接整段给模型（≤200MB），否则用抽帧。"""
    parts = []
    text = msg.get("content", "")
    atts = msg.get("attachments", [])
    if atts:
        for a in atts:
            if a.get("kind") == "image" and a.get("local"):
                parts.append({"type": "image_url",
                              "image_url": {"url": b64_image(a["local"])}})
            elif a.get("kind") == "video":
                local = a.get("local") or ""
                if inline_video and local and os.path.splitext(local)[1].lower() in VIDEO_EXT \
                        and can_inline_video(local):
                    parts.append({"type": "video_url", "video_url": {"url": b64_video(local)}})
                    parts.append({"type": "text", "text": "（上面这条是用户发来的视频）"})
                else:
                    for i, fp in enumerate(a.get("frames", [])[:8]):
                        parts.append({"type": "image_url",
                                      "image_url": {"url": b64_image(fp)}})
                        parts.append({"type": "text", "text": f"（视频第 {i + 1} 帧）"})
    if text:
        parts.append({"type": "text", "text": text})
    if not parts:
        parts.append({"type": "text", "text": "（空）"})
    if all(p["type"] == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


MODEL_LOCK = threading.Lock()


def call_model(cfg, messages, timeout=180):
    key = cfg.get("api_key", "").strip()
    if not key:
        raise RuntimeError("还没有配置 API Key，去右上角「设置」里填")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({"model": cfg.get("vision_model") or cfg["model"],
                       "messages": messages, "temperature": 0.6,
                       "stream": False}, ensure_ascii=False).encode("utf-8")
    last = ""
    data = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with MODEL_LOCK:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:400]
            last = f"模型接口 {e.code}: {detail}"
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(3 + attempt * 4)
                continue
            raise RuntimeError(last)
        except Exception as e:
            last = f"模型调用失败: {e}"
            if attempt < 2:
                time.sleep(3 + attempt * 4)
                continue
            raise RuntimeError(last)
    if data is None:
        raise RuntimeError(last or "模型未返回")
    try:
        msg = data["choices"][0]["message"]
    except Exception:
        raise RuntimeError("模型返回格式异常: " + json.dumps(data, ensure_ascii=False)[:300])
    return (msg.get("content") or msg.get("reasoning_content") or "").strip()


def summarize(cfg, convo, old_msgs):
    lines = []
    for m in old_msgs:
        who = "用户" if m["role"] == "user" else "客服"
        t = (m.get("content") or "").replace("\n", " ")[:200]
        extra = ""
        if m.get("attachments"):
            names = "、".join(a.get("name", "") for a in m["attachments"])
            extra = f"[附件:{names}]"
        lines.append(f"{who}: {extra}{t}")
    prompt = ("把下面的对话压缩成不超过 400 字的中文摘要，保留：用户身份/需求、已给出的方案、"
              "关键数据、尚未解决的问题。只输出摘要正文。\n\n" + "\n".join(lines))
    try:
        summary = call_model(cfg, [{"role": "user", "content": prompt}], timeout=120)
    except Exception as e:
        print("[memory] 摘要失败:", e)
        return convo.get("summary", "")
    return (convo.get("summary", "") + "\n" + summary).strip()[-2000:]


def build_messages(cfg, convo, user_text, atts, memory=""):
    msgs = [{"role": "system", "content": cfg["system_prompt"]}]
    if memory:
        msgs.append({"role": "system", "content": "【相关记录（现查的）】\n" + memory})
    if convo.get("summary"):
        msgs.append({"role": "system",
                     "content": "【更早对话的摘要】\n" + convo["summary"]})
    if cfg.get("full_context", True):
        history = convo["messages"]
    else:
        keep = max(2, int(cfg.get("keep_recent_messages", 16)))
        history = convo["messages"][-keep:]
    for m in history:
        msgs.append({"role": m["role"], "content": model_content(m)})
    cur = {"role": "user", "content": user_text, "attachments": atts}
    msgs.append({"role": "user", "content": model_content(cur, inline_video=True)})
    return msgs


# ---------------------------------------------------------------- 快手联动
KS_STATE = os.path.join(DATA, "ks_state.json")
KS_SEEN = {}
KS_SENT = {}
KS_LOCK = threading.Lock()
KS_STAT = {"last_poll": 0, "last_error": "", "replied": 0, "running": False}
# 分享视频要独占手机界面，期间暂停轮询，否则两边抢界面
SHARE_LOCK = threading.Event()


def ks_conf(cfg):
    return {"enabled": bool(cfg.get("ks_enabled")),
            "bridge": cfg.get("ks_bridge") or "http://127.0.0.1:8765",
            "groups": cfg.get("ks_groups") or [],
            "prefix": cfg.get("ks_prefix") or "",
            "poll": int(cfg.get("ks_poll") or 8),
            "stickers": bool(cfg.get("ks_stickers")),
            "notice": cfg.get("ks_notice") or "",
            "tools": bool(cfg.get("ks_tools")),
            "idle_min": int(cfg.get("ks_idle_min") or 0),
            "share_link": bool(cfg.get("ks_share_link")),
            "share_min": float(cfg.get("ks_share_min") or 0),
            "share_count": int(cfg.get("ks_share_count") or 1),
            "think_min": float(cfg.get("ks_think_min") or 0),
            "think_on": bool(cfg.get("ks_think_on")),
            "think_prompt": cfg.get("ks_think_prompt") or "",
            "source": (cfg.get("ks_source") or "ui").lower(),
            "auto_video": bool(cfg.get("ks_auto_video"))}


def load_seen():
    try:
        with open(KS_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen():
    try:
        with open(KS_STATE, "w", encoding="utf-8") as f:
            json.dump(KS_SEEN, f, ensure_ascii=False)
    except Exception:
        pass


SAFE_RETRY = ("/status", "/groups", "/db", "/read", "/msgmedia", "/fetch", "/shot", "/stickers")


def ks_call(url, payload=None, timeout=60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    tries = 2 if url.rstrip("/").endswith(SAFE_RETRY) else 1
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json",
                                              "Connection": "close"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            last = e
            if attempt + 1 < tries:
                time.sleep(2)
    raise RuntimeError(f"连不上快手桥（{last}）")


def group_session(name):
    for s in list_sessions():
        c = read_session(s["id"])
        if c and c.get("group") == name:
            return c
    c = new_session("快手 · " + name)
    c["group"] = name
    write_session(c)
    return c


def ks_useful(t):
    """过滤时间戳、"3人已读"、按钮文字这类 UI 噪声。"""
    t = t.strip()
    if not t or re.fullmatch(r"\d{1,2}:\d{2}", t):
        return False
    if re.fullmatch(r"\d+人已读|已读|还未看|已阅|送花|抢", t):
        return False
    if "撤回了一条消息" in t or "撤回了一条" in t or "撤回了" in t:
        return False
    if t.endswith("加入了群聊") or t.endswith("退出了群聊") or "成为群成员" in t:
        return False
    if "修改了群名" in t or "群公告" in t or "被移出群聊" in t:
        return False
    if "最活跃" in t or "已读" in t:
        return False
    return True


DSL_SPEC = """

【快手群聊规则】你现在就在快手群聊里，直接和大家聊天。
1. 直接输出你要发到群里的那句话，不要加引号、不要解释、不要写“我回复”。
2. 要说几句就换行，每行一句，一行是一次发送。
3. 不想说话就只输出：不回复
4. 需要额外动作时，把指令单独占一行（只写指令本身），系统会执行，这一行不会发到群里：
   发表情 开心     打开表情面板，看图自己挑一个合适的表情发出去
   看视频 5        刷 5 分钟视频
   读消息          重新读一遍群里最新消息
   下载视频        下载群里最新的视频
   状态            查看快手当前状态
   查记录 关键词     在自己保存的全部聊天记录里查（记不清之前说过什么就用这个）
5. 系统每次会自动附上【相关记录（现查的）】，那是从历史里检索出来的，可以直接引用；觉得不够就用「查记录 关键词」再查。
6. 对方的图片、表情包、视频已经给你看过了，按看到的内容回应。
7. 这是同学群，说话像同学。像人一样聊天：
   - 短。一条 5~20 字，特别有话说也最多三行；能用“啊、嗯、哈哈、确实、可以啊、真的假的”就别用书面词。
   - 不要每次都用完整句子，不要“首先其次最后”，不要分点，不要写小作文，不要复述对方的话。
   - 不要自称AI/助手/机器人，不要说“我理解你的意思”“希望能帮到你”，不要解释自己怎么回答。
   - 语气跟着对方走：对方随口问就随口答；对方开玩笑就接梗；不会的话题可以直接说“这个我真不懂”。
   - 不要每条都回得很热情，有时候一句“行”“好耶”“？”就够了。
   - 不用每句都带表情符号，一条消息里最多一个，多数时候不带。"""

DIRECTIVE_RE = re.compile(
    r"^[【\[\(]?\s*(下载视频|看视频|发表情|读消息|状态|查记录|查聊天|查群记录)\s*[:：]?\s*(.*?)\s*[】\]\)]?\s*$")

# 网页面板里说“在群里发……”时，直接把生成的内容发到群
SEND_INTENT_RE = re.compile(
    r"(在|往|给|对).{0,12}群[聊里中]?.{0,6}(发|说|喊|回)|发到.{0,12}群|发.{0,4}群里")


def parse_directives(reply):
    """把提示词指令行拆出来，剩下的才是要发到群里的话。"""
    acts, keep = [], []
    for line in reply.splitlines():
        s = line.strip()
        if not s:
            continue
        m = DIRECTIVE_RE.match(s)
        if m:
            acts.append((m.group(1), m.group(2) or ""))
        else:
            keep.append(s)
    return acts, "\n".join(keep).strip()


STOP_WORDS = set("我们 你们 他们 这个 那个 什么 怎么 可以 就是 但是 因为 所以 没有 一个 现在 "
                 "还是 已经 不是 这样 那样 时候 自己 知道 觉得 真的 好的 哈哈".split())


def query_tokens(text):
    raw = re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z0-9]{2,}", text or "")
    out = []
    for w in raw:
        if w in STOP_WORDS:
            continue
        out.append(w)
        if len(w) >= 4:
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
    seen, res = set(), []
    for w in out:
        if w not in seen and w not in STOP_WORDS:
            seen.add(w)
            res.append(w)
    return res[:12]


def search_records(keyword, group="", limit=12):
    """在所有保存下来的对话/群记录里检索（按词命中打分），供模型“查记录”用。"""
    words = query_tokens(keyword)
    hits = []

    def scan(convo):
        for m in convo.get("messages", []):
            c = (m.get("content") or "").strip()
            if not c:
                continue
            score = sum(1 for w in words if w in c)
            if words and score == 0:
                continue
            who = "AI" if m.get("role") == "assistant" else "对方"
            hits.append((score, m.get("ts") or 0, f"{who}: {c[:200]}"))

    try:
        if group:
            scan(group_session(group))
    except Exception:
        pass
    for name in os.listdir(CONV_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(CONV_DIR, name), "r", encoding="utf-8") as f:
                scan(json.load(f))
        except Exception:
            continue
    hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
    seen, out = set(), []
    for _, _, line in hits:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= limit:
            break
    return "\n".join(out) if out else "（没查到相关记录）"


def memory_context(group, query, cfg=None):
    """每次回消息前现查一遍历史，再交给免费的文本模型压成要点。"""
    cfg = cfg or load_cfg()
    if (cfg.get("context_mode") or "retrieval") != "retrieval":
        return ""
    raw = search_records(query, group, limit=24)
    if not raw or raw.startswith("（没查到"):
        return ""
    sm = (cfg.get("summary_model") or "").strip()
    if sm:
        try:
            text = call_model({**cfg, "vision_model": sm},
                              [{"role": "user", "content":
                                "把下面这些聊天记录压成不超过 200 字的中文要点，"
                                "保留人名、事件、数字、结论，只输出要点。\n\n" + raw}],
                              timeout=90)
            return text.strip()[:600]
        except Exception as e:
            print("[memory] 免费模型总结失败:", e)
    return "\n".join(raw.splitlines()[:6])


def ks_sticker_pick(group, mood):
    """打开表情面板 -> 截图 -> 模型看图给坐标 -> 点下去（坐标越界就重来一次）。"""
    cfg = load_cfg()
    bridge = (cfg.get("ks_bridge") or "http://127.0.0.1:8765").rstrip("/")
    last = ""
    for _ in range(2):
        shot = ks_call(bridge + "/stickers", {"group": group}, timeout=180)
        path = shot.get("file")
        if not path or not os.path.exists(path):
            raise RuntimeError("表情面板截图失败")
        ask = ("这是快手群聊表情面板截图，分辨率 1080x1920。要表达的意思：" + (mood or "随便来一个")
               + "。表情格子只在 y 约 1150~1850 的区域里，请挑一个最贴合的，"
                 "只输出它在整张 1080x1920 截图里的中心坐标，格式 x,y，不要其他文字。")
        ans = call_model(cfg, [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": b64_image(path)}},
            {"type": "text", "text": ask}]}])
        m = re.findall(r"(\d{2,4})\s*[,，]\s*(\d{2,4})", ans)
        if not m:
            last = "模型没给出坐标: " + ans[:60]
            continue
        x, y = int(m[0][0]), int(m[0][1])
        if not (0 < x < 1080 and 1100 < y < 1900):
            last = f"坐标越界({x},{y})，重试"
            print("[表情]", last)
            continue
        ks_call(bridge + "/tap", {"x": x, "y": y}, timeout=60)
        return {"ok": True, "point": [x, y]}
    raise RuntimeError(last or "表情定位失败")


def run_directive(name, arg, group):
    if name in ("查记录", "查聊天", "查群记录"):
        return search_records(arg, group)
    if name == "发表情":
        if (arg or "").strip().isdigit():
            return ks_action("sticker", group=group, index=int(arg))
        return ks_sticker_pick(group, arg)
    if name == "看视频":
        return ks_action("watch", minutes=float(arg or 5))
    if name == "读消息":
        return ks_action("read", group=group, n=20)
    if name == "下载视频":
        return ks_action("download", group=group, index=0)
    if name == "状态":
        return ks_action("status")
    raise RuntimeError("未知指令: " + name)


def ks_action(action, **kw):
    cfg = load_cfg()
    bridge = (cfg.get("ks_bridge") or "http://127.0.0.1:8765").rstrip("/")
    if action == "status":
        return ks_call(bridge + "/status", timeout=30)
    if action == "read":
        return ks_call(bridge + "/read", {"group": kw.get("group", ""), "n": int(kw.get("n", 20))},
                       timeout=120)
    if action == "send":
        return ks_call(bridge + "/send", {"group": kw["group"], "text": kw["text"]}, timeout=120)
    if action == "sticker":
        return ks_call(bridge + "/sticker", {"group": kw["group"], "index": int(kw.get("index", 0))},
                       timeout=120)
    if action == "watch":
        # 刷视频本身就要跑几分钟，客户端必须等得住
        return ks_call(bridge + "/watch", {"minutes": float(kw.get("minutes", 5)), "like": 0.05},
                       timeout=900)
    if action == "download":
        return ks_call(bridge + "/download",
                       {"group": kw["group"], "index": int(kw.get("index", 0))}, timeout=300)
    if action == "shot":
        return ks_call(bridge + "/shot", {"box": kw.get("box")}, timeout=120)
    if action == "share":
        SHARE_LOCK.set()
        try:
            return ks_call(bridge + "/share", {"group": kw["group"]}, timeout=300)
        finally:
            SHARE_LOCK.clear()
    if action == "takeover":
        cfg = save_cfg({"ks_enabled": True})
        groups = kw.get("groups") or cfg.get("ks_groups") or []
        notice = kw.get("text") or cfg.get("ks_notice") or ""
        out = {}
        for g in groups:
            KS_SEEN.pop("notice::" + g, None)
            if not notice:
                out[g] = "未配置接管通知，仅启用"
                continue
            try:
                out[g] = ks_call(bridge + "/send", {"group": g, "text": notice})
                KS_SEEN["notice::" + g] = time.time()
            except Exception as e:
                out[g] = "提醒发送失败: " + str(e)
        save_seen()
        return {"ok": True, "enabled": True, "groups": groups, "notice": notice, "result": out}
    if action == "release":
        cfg = save_cfg({"ks_enabled": False})
        groups = kw.get("groups") or cfg.get("ks_groups") or []
        notice = kw.get("text") or ""
        out = {}
        if notice:
            for g in groups:
                try:
                    out[g] = ks_call(bridge + "/send", {"group": g, "text": notice})
                except Exception as e:
                    out[g] = "提醒发送失败: " + str(e)
        return {"ok": True, "enabled": False, "notice": notice, "result": out}
    if action == "think":
        conf = ks_conf(load_cfg())
        conf["bridge"] = bridge
        g = kw.get("group") or (conf["groups"] or [""])[0]
        return {"line": ks_think(g, conf)}
    raise RuntimeError("未知动作: " + action)


def describe_images(paths, hint):
    """逐张单独调用模型：一次只喂一张图，避免超限。"""
    cfg = load_cfg()
    outs = []
    paths = [p for p in paths if p and os.path.exists(p)]
    for i, p in enumerate(paths):
        text = hint if len(paths) == 1 else f"（第 {i + 1} 张，共 {len(paths)} 张）{hint}"
        parts = [{"type": "image_url", "image_url": {"url": b64_image(p)}},
                 {"type": "text", "text": text}]
        try:
            outs.append(call_model(cfg, [{"role": "user", "content": parts}], timeout=120).strip())
        except Exception as e:
            outs.append(f"（第 {i + 1} 张识别失败：{e}）")
    return " ".join(o for o in outs if o).strip()


def can_inline_video(path):
    """能不能把整段视频塞给模型：受文件大小和 64K 上下文双重限制。"""
    cfg = load_cfg()
    limit = int(cfg.get("max_file_mb", 200)) * 1024 * 1024
    if not (path and os.path.exists(path) and os.path.getsize(path) <= limit):
        return False
    d = probe_duration(path)
    return 0 < d <= float(cfg.get("video_inline_seconds", 180))


def all_frames(path, out_dir, fps=1, limit=54):
    """整段视频按 1 秒 1 帧全部抽出来（上限 54 帧）。"""
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.startswith("af_"):
            try:
                os.remove(os.path.join(out_dir, old))
            except Exception:
                pass
    subprocess.run(["ffmpeg", "-y", "-i", path, "-vf", f"fps={fps}",
                    "-q:v", "3", os.path.join(out_dir, "af_%03d.jpg")],
                   capture_output=True, timeout=1800)
    frames = sorted(os.path.join(out_dir, n) for n in os.listdir(out_dir) if n.startswith("af_"))
    return frames[:limit]


def grid3x3(frames, out):
    """把最多 9 帧拼成一张 3x3 九宫格图，省上下文。"""
    frames = frames[:9]
    if not frames:
        return None
    cell_w, cell_h = 360, 640
    args = ["ffmpeg", "-y"]
    for p in frames:
        args += ["-i", p]
    n = len(frames)
    filt = "".join(
        f"[{i}:v]scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
        f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2[v{i}];" for i in range(n))
    layout = "|".join(f"{(i % 3) * cell_w}_{(i // 3) * cell_h}" for i in range(n))
    stack = "".join(f"[v{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={layout}:fill=black[out]"
    args += ["-filter_complex", filt + stack, "-map", "[out]", "-frames:v", "1", out]
    subprocess.run(args, capture_output=True, timeout=300)
    return out if os.path.exists(out) and os.path.getsize(out) > 0 else None


def describe_video(path):
    """视频文件整段发给模型；超上下文时抽 9 帧拼成一张九宫格再识别。"""
    cfg = load_cfg()
    if can_inline_video(path):
        parts = [{"type": "video_url", "video_url": {"url": b64_video(path)}},
                 {"type": "text", "text": "这是快手群聊里对方发的视频，用两句话说说内容和氛围。"}]
        try:
            return call_model(cfg, [{"role": "user", "content": parts}], timeout=600).strip()
        except Exception as e:
            print("[video] 整段识别失败，改用九宫格:", e)
    if not path:
        return "（拿不到视频内容）"
    frames = all_frames(path, os.path.join(os.path.dirname(path), "frames"))
    if not frames:
        return "（拿不到视频内容）"
    out_dir = os.path.dirname(path)
    texts = []
    for i in range(0, len(frames), 9):
        g = grid3x3(frames[i:i + 9], os.path.join(out_dir, f"grid_{i // 9}.jpg"))
        if not g:
            continue
        texts.append(describe_images(
            [g], "这是同一段视频按时间顺序抽帧后拼的九宫格（从左到右、从上到下按时间先后），"
                 "用一句话概括这几帧发生了什么。"))
    return "；".join(t for t in texts if t) or "（拿不到视频内容）"


def chat_in_group(cfg, convo, text, atts, state=None, memory=""):
    """纯提示词方式：模型正常说话，顺带用简单指令行控制快手动作。"""
    msgs = build_messages(cfg, convo, text, atts, memory)
    if cfg.get("ks_tools"):
        msgs[0]["content"] += DSL_SPEC
    group = convo.get("group") or ((cfg.get("ks_groups") or [""])[0] or "")
    reply = call_model(cfg, msgs)
    for _ in range(2):
        acts, body = parse_directives(reply)
        if not acts:
            return body
        results = []
        for name, arg in acts[:3]:
            try:
                out = json.dumps(run_directive(name, arg, group), ensure_ascii=False)[:500]
                if state is not None:
                    state.setdefault("trace", []).append({"tool": name, "args": arg, "result": out})
            except Exception as e:
                out = "失败: " + str(e)
                if state is not None:
                    state.setdefault("trace", []).append({"tool": name, "args": arg, "error": str(e)})
            results.append(f"{name}：{out}")
        if body:
            return body
        msgs.append({"role": "assistant", "content": reply})
        msgs.append({"role": "user",
                     "content": "【动作结果】\n" + "\n".join(results) + "\n现在直接说你要发到群里的话。"})
        reply = call_model(cfg, msgs)
    return parse_directives(reply)[1]


def ks_say(g, text):
    """由 AI 决定说什么、要不要发；模型输出即为群消息内容。"""
    cfg = load_cfg()
    convo = group_session(g)
    convo["messages"].append({"role": "user", "content": text, "ts": time.time()})
    state = {"trace": []}
    reply, pname = plugin_reply(text, g)
    if reply:
        print(f"[插件] {pname} 处理了这条消息")
    else:
        mem = memory_context(g, text, cfg)
        reply = chat_in_group(cfg, convo, text, [], state, mem)
    clean = (reply or "").strip()
    if re.fullmatch(r"[\[【（(]?\s*(不回复|跳过|SKIP)\s*[\]】）)。.]?", clean, re.I):
        clean = ""
    sent = False
    if clean:
        clean = plugin_before_send(g, clean)
    if clean:
        try:
            ks_call((cfg.get("ks_bridge") or "").rstrip("/") + "/send", {"group": g, "text": clean})
            sent = True
            KS_SENT[g] = clean
        except Exception as e:
            print("[快手] 发送失败", g, e)
    convo["messages"].append({"role": "assistant", "content": clean or "(未回复)", "ts": time.time()})
    write_session(convo)
    state["sent"] = sent
    return clean


KS_MEDIA = os.path.join(os.path.dirname(ROOT), "kuaishou-bot", "media")


def idle_talk(g, conf):
    """没人的时候主动说一句；开了自动视频就从下载过的视频里挑一个推荐。"""
    cfg = load_cfg()
    video_desc = ""
    if conf["auto_video"] and os.path.isdir(KS_MEDIA):
        vids = [os.path.join(KS_MEDIA, n) for n in os.listdir(KS_MEDIA)
                if n.lower().endswith(".mp4")]
        if vids:
            try:
                video_desc = describe_video(random.choice(vids))
            except Exception as e:
                print("[活跃] 视频识别失败", e)
    prompt = ("你在一个快手同学群里，现在没人说话。你刚看到一个视频，内容大概是：" + video_desc +
              "。用一句话推荐给大家，口语化，不超过 25 字，只输出这句话。") if video_desc else \
             "你在一个快手同学群里，现在没人说话，你主动发一句话活跃气氛，口语化，不超过 20 字，只输出这句话。"
    try:
        line = call_model(cfg, [{"role": "user", "content": prompt}], timeout=120).strip()
    except Exception as e:
        print("[活跃] 生成失败", e)
        return ""
    return line.splitlines()[0][:60] if line else ""


def fetch_group_msgs(bridge, g, conf):
    """读一下群里现在有什么：默认走界面（像人一样去看一眼，稳），也可走数据库（快）。"""
    if conf["source"] == "db":
        res = ks_call(bridge + "/db", {"group": g, "limit": 20}, timeout=120)
        msgs = res.get("messages") or []
        for m in msgs:
            m["side"] = "me" if m.get("me") else "other"
        return msgs
    res = ks_call(bridge + "/read", {"group": g, "n": 20}, timeout=180)
    out = []
    for m in res.get("messages") or []:
        out.append({"id": 0, "text": (m.get("text") or "").strip(),
                    "kind": m.get("kind") or "", "box": m.get("box"),
                    "side": m.get("side") or "other", "y": m.get("y") or 0})
    return out


def msg_key(g, m):
    """同一条消息的指纹：数据库模式用 id，界面模式用内容+位置。"""
    if m.get("id"):
        return g + "::id|" + str(m["id"])
    kind = m.get("kind") or ""
    text = (m.get("text") or "").strip()
    side = m.get("side") or "other"
    if kind and not text:
        box = m.get("box") or [0, 0, 0, 0]
        cx = (box[0] + box[2]) // 120
        cy = (box[1] + box[3]) // 120
        return g + f"::mk|{kind}|{side}|{cx},{cy}"
    return g + f"::mt|{side}|{text[:50]}"


def ks_think(g, conf):
    """发一条自己的感想：结合群里最近聊的内容，像个人一样说句看法。"""
    cfg = load_cfg()
    convo = group_session(g)
    recent = [m.get("content") or "" for m in convo["messages"][-6:] if m.get("content")]
    ctx = "\n".join(recent[-6:]) or "群里刚刚没什么新话题"
    prompt = ("你在这个快手的同学群里。群里最近的聊天记录是：\n" + ctx +
              "\n\n现在你主动发一条自己的感想。" + (conf.get("think_prompt") or "") +
              "不要用引号，不要解释你在做什么。")
    line = call_model(cfg, [{"role": "user", "content": prompt}], timeout=120).strip()
    if not line:
        return ""
    line = line.splitlines()[0][:80]
    ks_call((conf["bridge"] or "").rstrip("/") + "/send", {"group": g, "text": line}, timeout=120)
    KS_SENT[g] = line
    convo["messages"].append({"role": "assistant", "content": line, "ts": time.time()})
    write_session(convo)
    return line


BRIDGE_LAST_TRY = [0.0]


def ks_loop():
    global KS_SEEN
    KS_SEEN = load_seen()
    while True:
        cfg = load_cfg()
        conf = ks_conf(cfg)
        ks = KS_STAT
        ks["running"] = True
        # 桥没起来（比如模拟器后开）时隔一分钟自动再拉一次
        if time.time() - BRIDGE_LAST_TRY[0] > 60:
            BRIDGE_LAST_TRY[0] = time.time()
            try:
                ks_call(conf["bridge"].rstrip("/") + "/status", timeout=3)
            except Exception:
                ensure_bridge_async()
        if SHARE_LOCK.is_set():
            time.sleep(2)
            continue
        if conf["enabled"] and conf["groups"]:
            bridge = conf["bridge"].rstrip("/")
            # 接管通知：每个群只发一次
            if conf["notice"]:
                for g in conf["groups"]:
                    nk = "notice::" + g
                    if nk not in KS_SEEN:
                        # 群里最后一条已经是接管通知就别再发一遍
                        try:
                            hist = ks_call(bridge + "/read", {"group": g, "n": 30}, timeout=90)
                            texts = [(m.get("text") or "").strip()
                                     for m in (hist.get("messages") or [])]
                            if conf["notice"].strip() in texts:
                                KS_SEEN[nk] = time.time()
                                print(f"[快手] {g} 最新已是接管通知，跳过")
                                continue
                        except Exception:
                            pass
                        try:
                            ks_call(bridge + "/send", {"group": g, "text": conf["notice"]})
                            KS_SEEN[nk] = time.time()
                            print(f"[快手] 已在 {g} 发送接管通知")
                        except Exception as e:
                            print("[快手] 通知发送失败", g, e)
            for g in conf["groups"]:
                try:
                    msgs = fetch_group_msgs(bridge, g, conf)
                    ks["last_poll"] = time.time()
                    ks["last_error"] = ""
                    ui_mode = conf["source"] != "db"
                    primed = KS_SEEN.get("ids::" + g) or any(
                        k.startswith(g + "::") for k in KS_SEEN)
                    if not primed:
                        ids = [int(m.get("id") or 0) for m in msgs]
                        if ids and max(ids):
                            KS_SEEN["ids::" + g] = max(ids)
                        for m in msgs:
                            if (m.get("text") or "").strip() or m.get("kind"):
                                KS_SEEN[msg_key(g, m)] = time.time()
                        save_seen()
                        print(f"[快手] 已记录 {g} 的历史消息作为基线（之后只处理更新的）")
                        continue
                    hw = 0 if ui_mode else int(KS_SEEN.get("ids::" + g) or 0)
                    # 收集本轮所有新消息（都入档），最后一条才触发回复
                    fresh = []
                    for m in msgs:
                        try:
                            mid = int(m.get("id") or 0)
                        except Exception:
                            mid = 0
                        if mid and mid <= hw:
                            continue
                        if m.get("side") == "me":
                            continue
                        text = (m.get("text") or "").strip()
                        kind = m.get("kind")
                        if text and KS_SENT.get(g) and text == KS_SENT[g]:
                            continue
                        if not kind and not ks_useful(text):
                            continue
                        if kind and not conf["stickers"]:
                            continue
                        key = msg_key(g, m)
                        with KS_LOCK:
                            if key in KS_SEEN:
                                continue
                            KS_SEEN[key] = time.time()
                        fresh.append(m)
                    if not fresh:
                        continue

                    # 媒体逐条下载真实文件：视频就是 mp4 本体，不再拿封面图冒充
                    for m in fresh:
                        m["_line"] = (m.get("text") or "").strip()
                        kind = m.get("kind")
                        if not kind or not conf["stickers"]:
                            continue
                        try:
                            mid = m.get("id")
                            if not mid:
                                # 界面模式没有消息 id，用库里最新的那条媒体消息补上
                                d = ks_call(bridge + "/db", {"group": g, "limit": 6}, timeout=120) or {}
                                cand = [x for x in (d.get("messages") or [])
                                        if x.get("kind") and not x.get("me")]
                                mid = cand[-1].get("id") if cand else 0
                            got = ks_call(bridge + "/msgmedia", {"id": mid}, timeout=300) or {}
                            if got.get("kind") == "video" and got.get("file"):
                                desc = describe_video(got["file"])
                                m["_line"] = (f"[对方发来视频，时长 {got.get('duration')} 秒，"
                                              f"文件：{got['file']}] {desc}")
                            elif got.get("kind") == "image" and got.get("file"):
                                desc = describe_images(
                                    [got["file"]], "这是快手群聊里对方发的图片/表情，用一句话说明它表达的意思。")
                                word = "表情包" if kind == "sticker" else "图片"
                                m["_line"] = f"[对方发来{word}] 文件：{got['file']}；{desc}"
                            else:
                                raise RuntimeError(got.get("error") or "下载失败")
                            print(f"[快手] 媒体识别 {g}: {m['_line'][:70]}")
                        except Exception as e:
                            print("[快手] 媒体识别失败", m.get("id"), e)
                            m["_line"] = f"[对方发来{kind}]（下载或识别失败：{e}）"

                    # 记下已处理到的最新一条，下一轮只看更新的（界面模式没有 id，靠内容指纹去重）
                    ids_now = [int(x.get("id") or 0) for x in msgs]
                    if ids_now and max(ids_now):
                        KS_SEEN["ids::" + g] = max(ids_now)
                        save_seen()

                    # 除最后一条外，其余新消息全部写入群会话，保证模型知道群里所有记录
                    convo = group_session(g)
                    for m in fresh[:-1]:
                        if m["_line"]:
                            convo["messages"].append({"role": "user", "content": m["_line"],
                                                      "ts": time.time()})
                    write_session(convo)

                    m = fresh[-1]
                    text = m["_line"] or (m.get("text") or "").strip()
                    if not m.get("kind"):
                        prefix = conf["prefix"]
                        if prefix and not text.startswith(prefix):
                            # 本轮若有对方发的视频/表情，即使没带前缀也要回应
                            med = [x for x in fresh if x.get("kind")] if conf["stickers"] else []
                            if not med:
                                continue
                            m = med[-1]
                            text = m["_line"]
                        elif prefix:
                            text = text[len(prefix):].strip()
                            if not text:
                                continue
                        print(f"[快手] 收到 {g}: {text[:40]}")
                    try:
                        reply = ks_say(g, text)
                        ks["replied"] += 1
                        print(f"[快手] 回复 {g}: {reply[:40]}")
                    except Exception as e:
                        ks["last_error"] = f"{g}: {e}"
                        print("[快手] 回复失败", g, e)
                except Exception as e:
                    ks["last_error"] = f"{g}: {e}"
                    print("[快手] 轮询失败", g, e)
            # 三个独立定时器：分享视频 / 发感想 / 闲聊
            now = time.time()

            def _due(name, minutes):
                """到点返回 True，并顺手排下一次；停机太久就先重新排，不补发。"""
                nxt = float(KS_SEEN.get(name) or 0)
                if not nxt or now - nxt > minutes * 60:
                    KS_SEEN[name] = now + random.uniform(0.6, 1.0) * minutes * 60
                    return False
                if now >= nxt:
                    KS_SEEN[name] = now + random.uniform(0.7, 1.3) * minutes * 60
                    return True
                return False

            if conf["share_link"] and conf["share_min"] > 0 and _due("__share_next__", conf["share_min"]):
                g = random.choice(conf["groups"])
                ok = 0
                for _ in range(max(1, conf["share_count"])):
                    try:
                        ks_action("share", group=g)
                        ok += 1
                        time.sleep(3)
                    except Exception as e:
                        print("[分享] 失败", e)
                        break
                if ok:
                    convo = group_session(g)
                    convo["messages"].append({"role": "assistant",
                                              "content": f"[分享了一条视频]", "ts": time.time()})
                    write_session(convo)
                    print(f"[分享] 已分享 {ok} 条视频到 {g}")
                save_seen()

            if conf["think_on"] and conf["think_min"] > 0 and _due("__think_next__", conf["think_min"]):
                g = random.choice(conf["groups"])
                try:
                    line = ks_think(g, conf)
                    if line:
                        print(f"[感想] {g}: {line[:40]}")
                except Exception as e:
                    print("[感想] 失败", e)
                save_seen()

            if conf["idle_min"] > 0 and _due("__idle_next__", conf["idle_min"]):
                g = random.choice(conf["groups"])
                line = idle_talk(g, conf)
                if line:
                    try:
                        ks_call(bridge + "/send", {"group": g, "text": line})
                        KS_SENT[g] = line
                        convo = group_session(g)
                        convo["messages"].append({"role": "assistant", "content": line,
                                                  "ts": time.time()})
                        write_session(convo)
                        print(f"[活跃] 主动发言 {g}: {line[:30]}")
                    except Exception as e:
                        print("[活跃] 发送失败", e)
            save_seen()
        time.sleep(max(3, conf["poll"]))


# ---------------------------------------------------------------- 插件
PLUGIN_DIR = os.path.join(ROOT, "plugins")
PLUGIN_STATE = os.path.join(DATA, "plugins.json")
os.makedirs(PLUGIN_DIR, exist_ok=True)
PLUGIN_MODS = {}


def _plugin_state():
    if not os.path.exists(PLUGIN_STATE):
        return {}
    try:
        with open(PLUGIN_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_plugin_state(st):
    with open(PLUGIN_STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _plugin_files():
    return sorted(n for n in os.listdir(PLUGIN_DIR)
                  if n.endswith(".py") and not n.startswith("_"))


def _load_plugin(name):
    """按需加载插件模块；文件改过就重新加载。"""
    path = os.path.join(PLUGIN_DIR, name + ".py")
    mtime = os.path.getmtime(path)
    hit = PLUGIN_MODS.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    import importlib.util
    spec = importlib.util.spec_from_file_location("kefu_plugin_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    PLUGIN_MODS[name] = (mtime, mod)
    return mod


PLUGIN_TIMERS = []      # [(插件名, 间隔秒, 函数, 下次时间)]
PLUGIN_STATES = {}      # 插件名 -> 持久化字典


class PluginAPI:
    """插件能用的工具箱：发消息、读消息、配置、HTTP、定时、持久化。"""

    def __init__(self, name, group=""):
        self.name = name
        self.group = group

    def log(self, *args):
        print(f"[插件:{self.name}]", *args)

    def send(self, text, group=None):
        cfg = load_cfg()
        bridge = (cfg.get("ks_bridge") or "http://127.0.0.1:8765").rstrip("/")
        return ks_call(bridge + "/send", {"group": group or self.group, "text": text}, timeout=120)

    def read(self, group=None, n=20):
        cfg = load_cfg()
        bridge = (cfg.get("ks_bridge") or "http://127.0.0.1:8765").rstrip("/")
        return ks_call(bridge + "/read", {"group": group or self.group, "n": int(n)}, timeout=120)

    def cfg(self):
        return load_cfg()

    def save_cfg(self, patch):
        return save_cfg(patch)

    def http(self, url, data=None, headers=None, timeout=20, as_json=True):
        body = None
        hd = dict(headers or {})
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hd.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, headers=hd,
                                     method="POST" if body else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if as_json else raw

    @property
    def state(self):
        """插件自己的持久化字典，读写后调 save() 落盘。"""
        if self.name not in PLUGIN_STATES:
            p = os.path.join(DATA, "plugin_state", self.name + ".json")
            try:
                with open(p, "r", encoding="utf-8") as f:
                    PLUGIN_STATES[self.name] = json.load(f)
            except Exception:
                PLUGIN_STATES[self.name] = {}
        return PLUGIN_STATES[self.name]

    def save(self):
        d = os.path.join(DATA, "plugin_state")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self.name + ".json"), "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def every(self, seconds, func):
        """每 N 秒跑一次 func(api)。插件定时任务统一由后台线程调度。"""
        sec = max(1.0, float(seconds))
        PLUGIN_TIMERS.append([self.name, sec, func, time.time() + sec])


def plugin_ctx(name, group=""):
    return {"group": group, "cfg": load_cfg(), "api": PluginAPI(name, group)}


def _enabled_plugins():
    st = _plugin_state()
    return [fn[:-3] for fn in _plugin_files()
            if st.get(fn[:-3], {}).get("enabled", True)]


def plugin_start_all():
    for name in _enabled_plugins():
        try:
            mod = _load_plugin(name)
            fn = getattr(mod, "on_start", None)
            if callable(fn):
                fn(plugin_ctx(name))
        except Exception as e:
            print(f"[插件] {name} on_start 出错: {e}")


def plugin_stop_all():
    for name in _enabled_plugins():
        try:
            mod = _load_plugin(name)
            fn = getattr(mod, "on_stop", None)
            if callable(fn):
                fn(plugin_ctx(name))
        except Exception as e:
            print(f"[插件] {name} on_stop 出错: {e}")


def plugin_ticker():
    """后台线程：跑插件注册的定时任务。"""
    while True:
        time.sleep(1)
        now = time.time()
        for item in list(PLUGIN_TIMERS):
            name, sec, fn, nxt = item
            if now < nxt:
                continue
            item[3] = now + sec
            try:
                fn(PluginAPI(name))
            except Exception as e:
                print(f"[插件] {name} 定时任务出错: {e}")


def list_plugins():
    st = _plugin_state()
    out = []
    for fn in _plugin_files():
        name = fn[:-3]
        meta = {"name": name, "file": fn,
                "enabled": st.get(name, {}).get("enabled", True),
                "desc": "", "error": ""}
        try:
            mod = _load_plugin(name)
            meta["desc"] = getattr(mod, "DESC", "")
            meta["commands"] = list((getattr(mod, "COMMANDS", None) or {}).keys())
            meta["hooks"] = [h for h in ("handle", "on_before_send", "on_start", "on_tick", "on_stop")
                             if callable(getattr(mod, h, None))]
            if not meta["hooks"]:
                meta["error"] = "没有可用的钩子函数"
        except Exception as e:
            meta["error"] = f"加载失败：{e}"
        out.append(meta)
    return out


def install_plugin(source="", value="", name="", code=""):
    """从本地文件 / URL / 直接粘贴的代码安装插件。"""
    if code.strip():
        if not name:
            raise RuntimeError("粘贴代码时要给一个插件名")
        dst = os.path.join(PLUGIN_DIR, re.sub(r"[^\w\-]", "_", name) + ".py")
        with open(dst, "w", encoding="utf-8") as f:
            f.write(code)
    elif source == "file":
        src = value.strip().strip('"')
        if not os.path.isfile(src):
            raise RuntimeError("文件不存在：" + src)
        dst = os.path.join(PLUGIN_DIR, os.path.basename(src))
        with open(src, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())
    elif source == "url":
        url = value.strip()
        if not url.startswith(("http://", "https://")):
            raise RuntimeError("只支持 http/https 链接")
        with urllib.request.urlopen(url, timeout=30) as r:
            data = r.read()
        dst = os.path.join(PLUGIN_DIR, os.path.basename(urlparse(url).path) or "plugin.py")
        with open(dst, "wb") as f:
            f.write(data)
    else:
        raise RuntimeError("不知道从哪安装")
    # 装完默认启用
    pname = os.path.basename(dst)[:-3]
    st = _plugin_state()
    st.setdefault(pname, {})["enabled"] = True
    _save_plugin_state(st)
    PLUGIN_MODS.pop(pname, None)
    _load_plugin(pname)
    return pname


def remove_plugin(name):
    path = os.path.join(PLUGIN_DIR, re.sub(r"[^\w\-]", "_", name) + ".py")
    if not os.path.exists(path):
        raise RuntimeError("插件不存在")
    os.remove(path)
    PLUGIN_MODS.pop(name, None)
    st = _plugin_state()
    st.pop(name, None)
    _save_plugin_state(st)


def toggle_plugin(name, enabled):
    st = _plugin_state()
    st.setdefault(name, {})["enabled"] = bool(enabled)
    _save_plugin_state(st)
    return st[name]["enabled"]


def plugin_reply(text, group):
    """群里来消息时先过一遍插件：
    先看插件注册的指令（COMMANDS，例如「天气 北京」），再看通用 handle。
    谁返回了内容就由谁回复，返回空就继续交给下一个，都没有就交给模型。"""
    st = _plugin_state()
    t = (text or "").strip()
    for fn in _plugin_files():
        name = fn[:-3]
        if not st.get(name, {}).get("enabled", True):
            continue
        try:
            mod = _load_plugin(name)
            ctx = plugin_ctx(name, group)
            for key, func in (getattr(mod, "COMMANDS", None) or {}).items():
                if t == key or t.startswith(key + " ") or t.startswith(key + "："):
                    arg = t[len(key):].strip(" :：")
                    out = func(arg, ctx)
                    if isinstance(out, str) and out.strip():
                        return out.strip(), name
            fn_handle = getattr(mod, "handle", None)
            if callable(fn_handle):
                out = fn_handle(text, ctx)
                if isinstance(out, str) and out.strip():
                    return out.strip(), name
        except Exception as e:
            print(f"[插件] {name} 出错: {e}")
    return "", ""


def plugin_before_send(group, text):
    """发送前给插件一次改写机会：返回新文本替换，返回 False 拦截不发。"""
    st = _plugin_state()
    cur = text
    for fn in _plugin_files():
        name = fn[:-3]
        if not st.get(name, {}).get("enabled", True):
            continue
        try:
            mod = _load_plugin(name)
            fn_hook = getattr(mod, "on_before_send", None)
            if not callable(fn_hook):
                continue
            out = fn_hook(group, cur, plugin_ctx(name, group))
            if out is None:
                continue
            if out is False:
                return ""
            cur = str(out)
        except Exception as e:
            print(f"[插件] {name} on_before_send 出错: {e}")
    return cur


# ---------------------------------------------------------------- HTTP
CFG = load_cfg()


class Handler(BaseHTTPRequestHandler):
    server_version = "KefuBot/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")

    # ---- helpers
    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _raw(self, path, ctype, download=False):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if ctype.startswith("text/html"):
            self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", "inline")
        self.end_headers()
        self.wfile.write(data)

    def _raw_str(self, text, ctype):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._raw_str(
                    "<h3>已改为命令行界面</h3>"
                    "<p>在本目录运行 <code>python tui.py</code> 即可（方向键选择，回车确认）。</p>",
                    "text/html; charset=utf-8")
            if u.path == "/api/plugins":
                return self._json({"plugins": list_plugins(), "dir": PLUGIN_DIR})
            if u.path == "/api/config":
                cfg = load_cfg()
                key = cfg.get("api_key", "")
                return self._json({**cfg, "api_key": (key[:6] + "…" + key[-4:]) if key else "",
                                   "has_key": bool(key)})
            if u.path == "/api/sessions":
                return self._json({"sessions": list_sessions()})
            if u.path == "/api/session":
                c = read_session(q.get("id", [""])[0])
                return self._json(c) if c else self._json({"error": "not found"}, 404)
            if u.path == "/api/kuaishou":
                cfg = load_cfg()
                return self._json({**ks_conf(cfg), "status": KS_STAT,
                                   "state_file": os.path.exists(KS_STATE)})
            if u.path.startswith("/u/"):
                rel = u.path[3:].split("/")
                fp = os.path.join(UP_DIR, *rel)
                if not os.path.abspath(fp).startswith(os.path.abspath(UP_DIR)) or not os.path.exists(fp):
                    return self._json({"error": "not found"}, 404)
                ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
                return self._raw(fp, ctype)
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # ---- POST
    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/sessions":
                body = json.loads(self._body() or b"{}")
                c = new_session(body.get("title") or "新对话")
                return self._json(c)
            if u.path == "/api/config":
                patch = json.loads(self._body() or b"{}")
                if patch.get("api_key", "").startswith("****"):
                    patch.pop("api_key")
                cfg = save_cfg(patch)
                global CFG
                CFG = cfg
                return self._json({"ok": True, "model": cfg["model"]})
            if u.path == "/api/kuaishou":
                patch = json.loads(self._body() or b"{}")
                allow = {"ks_enabled", "ks_bridge", "ks_groups", "ks_prefix", "ks_poll",
                         "ks_stickers", "ks_notice", "ks_tools", "ks_source",
                         "ks_share_link", "ks_share_min", "ks_share_count",
                         "ks_think_min", "ks_think_on", "ks_think_prompt", "ks_idle_min"}
                cfg = save_cfg({k: v for k, v in patch.items() if k in allow})
                return self._json({"ok": True, **ks_conf(cfg)})
            if u.path == "/api/plugins":
                body = json.loads(self._body() or b"{}")
                if "enabled" in body:
                    v = toggle_plugin(body.get("name", ""), body["enabled"])
                elif body.get("install"):
                    v = install_plugin(source=body.get("source", ""),
                                       value=body.get("value", ""),
                                       name=body.get("name", ""),
                                       code=body.get("code", ""))
                else:
                    v = None
                return self._json({"ok": True, "result": v, "plugins": list_plugins()})
            if u.path == "/api/ks/action":
                body = json.loads(self._body() or b"{}")
                act = body.pop("action", "")
                return self._json(ks_action(act, **body))
            if u.path == "/api/upload":
                return self._upload()
            if u.path == "/api/chat":
                return self._chat(json.loads(self._body() or b"{}"))
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_DELETE(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/plugins":
                remove_plugin(q.get("name", [""])[0])
                return self._json({"ok": True, "plugins": list_plugins()})
            sid = q.get("id", [""])[0]
            if sid and os.path.exists(session_path(sid)):
                os.remove(session_path(sid))
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # ---- 上传
    def _upload(self):
        ctype, pdict = cgi.parse_header(self.headers.get("Content-Type", ""))
        if ctype != "multipart/form-data":
            return self._json({"error": "需要 multipart/form-data"}, 400)
        pdict["boundary"] = pdict["boundary"].encode()
        pdict["CONTENT-LENGTH"] = int(self.headers.get("Content-Length") or 0)
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]})
        item = form["file"]
        data = item.file.read()
        cfg = load_cfg()
        print(f"[upload] {item.filename} {human(len(data))}")
        info = handle_upload(item.filename, data, cfg)
        info["local"] = os.path.join(UP_DIR, info["id"], os.path.basename(
            urllib.parse.unquote(info["url"].split("/")[-1])))
        if info.get("frames"):
            info["frames"] = [f.replace(UP_DIR, "").replace("\\", "/").lstrip("/") for f in info["frames"]]
        return self._json(info)

    # ---- 对话
    def _chat(self, body):
        sid = body.get("session_id")
        text = (body.get("text") or "").strip()
        atts = body.get("attachments") or []
        cfg = load_cfg()
        convo = read_session(sid) if sid else None
        if convo is None:
            convo = new_session(text[:16] or "新对话")
            sid = convo["id"]

        # 附件补全成本地绝对路径，供后续多轮复用
        for a in atts:
            if not a.get("local") and a.get("url"):
                a["local"] = os.path.join(UP_DIR, a["id"],
                                          urllib.parse.unquote(a["url"].split("/")[-1]))
            if a.get("frames"):
                a["frames"] = [os.path.join(UP_DIR, f.lstrip("/").replace("/", os.sep))
                               for f in a["frames"]]

        if not text and not atts:
            return self._json({"error": "内容为空"}, 400)

        convo["messages"].append({"role": "user", "content": text,
                                  "attachments": atts, "ts": time.time()})
        if convo["title"] in ("新对话", "") and text:
            convo["title"] = text[:18]

        # 超长上下文：默认不压缩，全部保留（full_context=false 时才滚动摘要）
        limit = int(cfg.get("summarize_after", 24))
        if not cfg.get("full_context", True) and len(convo["messages"]) > limit:
            keep = int(cfg.get("keep_recent_messages", 16))
            old = convo["messages"][:-keep]
            convo["summary"] = summarize(cfg, convo, old)
            convo["messages"] = convo["messages"][-keep:]

        if cfg.get("ks_tools"):
            base = {"id": convo["id"], "summary": convo.get("summary", ""),
                    "messages": convo["messages"][:-1]}
            tstate = {"trace": []}
            mem = memory_context(convo.get("group", ""), text, cfg)
            reply = chat_in_group(cfg, base, text, atts, tstate, mem)
        else:
            mem = memory_context(convo.get("group", ""), text, cfg)
            reply = call_model(cfg, build_messages(cfg, convo, text, atts, mem))
            tstate = {"trace": []}

        # 面板里明确说“在群里发……”时，真的发到群里，而不是只回在网页上
        if SEND_INTENT_RE.search(text) and cfg.get("ks_tools") and reply.strip():
            groups = cfg.get("ks_groups") or []
            target = convo.get("group") or (groups[0] if groups else "")
            if target:
                lines = [l.strip() for l in reply.splitlines() if l.strip()][:3] or [reply.strip()]
                sent, err = [], ""
                for ln in lines:
                    try:
                        ks_action("send", group=target, text=ln)
                        sent.append(ln)
                    except Exception as e:
                        err = str(e)
                        break
                tstate.setdefault("trace", []).append(
                    {"tool": "send", "args": {"group": target, "text": lines}, "result": err or "ok"})
                reply = (f"已发到《{target}》：\n" + "\n".join(sent)) if not err else \
                        (f"发到《{target}》失败：{err}\n原内容：\n" + "\n".join(lines))

        convo["messages"].append({"role": "assistant", "content": reply, "ts": time.time()})
        write_session(convo)
        return self._json({"session_id": sid, "title": convo["title"],
                           "reply": reply,
                           "trace": tstate.get("trace", []),
                           "memory": {"summary_chars": len(convo.get("summary", "")),
                                      "messages": len(convo["messages"])}})


def reset_notices():
    """保留上次的接管标记，重启不再重复发通知。"""
    global KS_SEEN
    KS_SEEN = load_seen()


def ensure_bridge():
    """桥不单独开窗口：由平台拉起，跟着平台一起活、一起关。"""
    cfg = load_cfg()
    bridge = (cfg.get("ks_bridge") or "http://127.0.0.1:8765").rstrip("/")
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(bridge.rsplit(":", 1)[-1])), timeout=2):
            return  # 端口已经在监听，说明桥还活着
    except OSError:
        pass
    if time.time() - BRIDGE_SPAWNED[0] < 120:
        return  # 刚拉过，别再重复开进程
    BRIDGE_SPAWNED[0] = time.time()
    try:
        ks_call(bridge + "/status", timeout=4)
        return
    except Exception:
        pass
    script = os.path.join(os.path.dirname(ROOT), "kuaishou-bot", "kuaishou.py")
    if not os.path.exists(script):
        print("[bridge] 没找到 kuaishou.py，跳过")
        return
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW，不弹新窗口
    try:
        subprocess.Popen([sys.executable, "-u", script, "api", "--port", "8765"],
                         cwd=os.path.dirname(script),
                         creationflags=flags, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        print("[bridge] 后台启动中 ...")
    except Exception as e:
        print("[bridge] 启动失败", e)
        return
    for _ in range(15):
        time.sleep(2)
        try:
            ks_call(bridge + "/status", timeout=4)
            print("[bridge] 就绪")
            return
        except Exception:
            continue
    print("[bridge] 30 秒没起来，检查模拟器/adb")


def ensure_bridge_async():
    threading.Thread(target=ensure_bridge, daemon=True).start()


BRIDGE_SPAWNED = [0.0]


def main():
    cfg = load_cfg()
    port = int(cfg.get("port", 8800))
    ensure_bridge_async()
    reset_notices()
    threading.Thread(target=plugin_ticker, daemon=True).start()
    threading.Thread(target=plugin_start_all, daemon=True).start()
    atexit.register(plugin_stop_all)
    threading.Thread(target=ks_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"客服机器人已启动: http://127.0.0.1:{port}")
    print(f"模型: {cfg.get('model')}  视频上限: {cfg.get('max_file_mb')}MB")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
