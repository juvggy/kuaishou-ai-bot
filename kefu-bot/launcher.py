"""单文件启动入口：桥（8765）+ 平台（8800）+ 命令行界面，全在一个进程里。

开发时：python launcher.py
打包时：pyinstaller --onefile --console --name KuaishouBot launcher.py
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "kuaishou-bot"))

# 告诉 app.py：桥在本进程里跑，不要再拉起子进程
os.environ["KS_EMBEDDED"] = "1"

import app          # noqa: E402
import kuaishou     # noqa: E402
import tui          # noqa: E402


def start_bridge():
    """模拟器没开就一直等，起来了自动接管。"""
    while True:
        try:
            dev = kuaishou.Device()
            print(f"[桥] 已连接 {dev.serial}  {dev.w}x{dev.h}")
            kuaishou.serve(dev, 8765)
            return
        except BaseException as e:
            print(f"[桥] 等模拟器：{e}（5 秒后重试）")
        time.sleep(5)


def main():
    headless = "--headless" in sys.argv
    threading.Thread(target=start_bridge, daemon=True).start()
    threading.Thread(target=app.main, daemon=True).start()

    cfg = app.load_cfg()
    port = int(cfg.get("port", 8800))
    tui.BASE = f"http://127.0.0.1:{port}"

    for _ in range(60):
        try:
            tui.api("/api/config", timeout=2)
            break
        except Exception:
            time.sleep(0.5)

    if headless:
        print("[启动] 无界面模式，Ctrl+C 退出")
        while True:
            time.sleep(3600)

    print("[启动] 程序已经运行在后台，这个窗口是操作界面（关窗口 = 停止机器人）")
    time.sleep(0.8)
    tui.main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
