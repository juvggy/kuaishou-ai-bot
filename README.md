# 快手 AI 群聊机器人（ADB + 大模型）

用 ADB 操控安卓模拟器里的快手，让大模型在群聊里像真人一样聊天。
不依赖 AstrBot、不注入快手、不破解接口，全程走模拟器界面 + 本地消息库。

## 结构

```
kuaishou-ai-bot/
├─ kefu-bot/            平台：HTTP 服务 + 调度 + 大模型 + TUI
│  ├─ app.py            后端（默认 127.0.0.1:8800）
│  ├─ tui.py            命令行界面（方向键选菜单）
│  ├─ start.bat         启动后端并进入 TUI
│  ├─ config.example.json  配置样例（复制成 config.json 再填 Key）
│  └─ plugins/          插件目录，放 .py 文件即生效
└─ kuaishou-bot/        桥：ADB 操控层（默认 127.0.0.1:8765）
   ├─ kuaishou.py       读消息 / 发消息 / 下载媒体 / 分享视频 / 刷推荐
   ├─ start.bat         单独调试用的菜单
   └─ ADBKeyboard.apk   中文输入用（发送文字必需）
```

调用链：`tui.py → app.py(8800) → kuaishou.py(8765) → adb → MuMu 模拟器`

## 环境要求

- Windows 10/11，Python 3.10+
- MuMu 模拟器（或其它可开 ADB 的模拟器），已登录快手
- ADB 在 PATH 里
- ffmpeg 在 PATH 里（只用到视频功能时需要）

## 安装

1. 安装依赖：

   ```bat
   pip install requests
   ```

2. 把本目录放到任意位置，进入 `kefu-bot`，把配置样例复制一份：

   ```bat
   copy config.example.json config.json
   ```

3. 打开 `config.json`，填 `api_key`（智谱开放平台），按需改 `ks_groups`。

4. 模拟器里安装 ADB 输入法（只发中文时需要）：

   ```bat
   cd ..\kuaishou-bot
   adb install -r ADBKeyboard.apk
   ```

5. 启动：

   ```bat
   cd ..\kefu-bot
   start.bat
   ```

   后端会在后台起来，同时打开命令行界面。

## 命令行界面（TUI）

用 `↑ ↓` 选择，回车确认，`Esc` 返回：

| 菜单 | 作用 |
| --- | --- |
| 运行状态 | 轮询间隔、上次读取、已回复数、最近错误、模型、各项开关 |
| 群聊与回复 | 自动回复开关、读消息方式（界面/数据库）、轮询间隔、群列表、@前缀、媒体识别、自动分享、发感想、闲聊 |
| 模型与提示词 | 改模型名、API Key、上下文模式、直接改人设提示词 |
| 插件管理 | 列出插件、启用/停用、从文件或链接安装、粘贴代码新建、删除 |
| 手动发消息到群里 | 选群 → 输入内容 → 发送 |

## 插件

`kefu-bot/plugins/` 下每个 `.py` 就是一个插件，保存即生效，不用重启。
插件是主进程里的原生 Python 模块，没有沙箱——可以 import 任何库、读写磁盘、发网络请求、
改配置、直接调快手桥，所以能做到“什么都能改”。

### 钩子

按需实现，全部可省略：

| 钩子 | 何时调用 | 返回值 |
| --- | --- | --- |
| `COMMANDS = {"天气": fn}` | 群消息以指令开头时（`天气 北京`） | 回复文本，空则继续 |
| `handle(text, ctx)` | 每条新消息 | 回复文本；空 = 交给下一个插件 / 大模型 |
| `on_before_send(group, text, ctx)` | 机器人每次发消息前 | `None` 不改 / 字符串替换 / `False` 拦截 |
| `on_start(ctx)` | 启动、或插件被启用时 | 无 |
| `on_stop(ctx)` | 进程退出时 | 无 |

模块级常量：`DESC`（一句话说明，TUI 里显示）、`AUTHOR`（可选）。

### 插件 API

`ctx["api"]` 是给插件的工具箱：

| 方法 | 作用 |
| --- | --- |
| `api.send(text, group=None)` | 往群里发消息（默认当前群） |
| `api.read(group=None, n=20)` | 读群里最新消息 |
| `api.cfg()` / `api.save_cfg(patch)` | 读/改机器人配置 |
| `api.http(url, data=None, headers=None)` | 发 HTTP 请求，返回 JSON |
| `api.every(秒, fn)` | 注册定时任务，`fn(api)` |
| `api.state` / `api.save()` | 插件自己的持久化字典（存 `data/plugin_state/`） |
| `api.log(...)` | 打日志 |

`ctx` 里还有 `group`（当前群名）和 `cfg`（当前配置快照）。

### 例子

```python
DESC = "群里发 ping 就回 pong"


def handle(text, ctx):
    if (text or "").strip().lower() == "ping":
        return "pong"      # 返回字符串 = 由插件回复，不再走模型
    return ""              # 返回空 = 交给大模型
```

更多示例见 `plugins/example_tools.py`（指令、定时、发送前改写、计数持久化）。
插件按文件名顺序执行，谁先返回内容谁生效；插件抛异常只记日志，不影响主流程。

## 配置说明（config.json）

| 键 | 说明 |
| --- | --- |
| `api_key` / `base_url` / `model` / `vision_model` | 大模型接入；`vision_model` 参与实际对话 |
| `full_context` / `context_mode` | `full` = 每次带全部聊天记录；`retrieval` = 检索 + 摘要压缩 |
| `ks_enabled` | 自动回复总开关 |
| `ks_groups` | 要接管的群名列表 |
| `ks_prefix` | 只回带此前缀的消息；留空 = 群里任何消息都回 |
| `ks_poll` | 轮询间隔（秒） |
| `ks_source` | `ui` 读界面 / `db` 读本地消息库 |
| `ks_stickers` | 是否识别视频/图片/表情（关闭后媒体消息直接跳过） |
| `ks_share_link` / `ks_share_min` / `ks_share_count` | 自动在推荐页分享视频，及间隔/条数 |
| `ks_think_on` / `ks_think_min` | 定时根据群聊内容发一句感想 |
| `ks_idle_min` | 没人说话时主动发言的间隔，`0` = 关闭 |

TUI 里改了会立刻写回 `config.json`，下次轮询即生效。

## 已知限制

- 依赖界面结构：快手版本大改后，`kuaishou.py` 里的资源 id / 坐标可能要跟着调。
- 一次只能驱动一个模拟器会话，分享视频会短暂占用界面，期间暂停轮询。
- 界面读取在视频播放、动画高频刷新时可能拿不到快照，已做重试与降级，不会中断轮询。
- 群聊记录、下载的媒体都落在本地 `data/`、`media/`，已在 `.gitignore` 里排除，别上传。

## 免责声明

仅供个人学习与自动化技术研究。请遵守快手用户协议与法律法规，不要用于骚扰、刷量、
诈骗等用途；账号风险自负。
