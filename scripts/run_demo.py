"""FacilityMind 一键本地 Demo 启动器（跨平台）。

启动 Web Dashboard（MCP server 会随 Dashboard 自动拉起），并自动打开浏览器。
用法：
    python scripts/run_demo.py            # 默认端口 8000
    python scripts/run_demo.py --port 9000
按 Ctrl+C 退出，会同时结束后台服务进程。
"""
import argparse
import os
import subprocess
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser(description="FacilityMind 本地 Demo 启动器")
    ap.add_argument("--port", type=int, default=8000, help="Dashboard 端口（默认 8000）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    cmd = [
        sys.executable, "-m", "uvicorn",
        "facilitymind.web.server:api",
        "--host", args.host,
        "--port", str(args.port),
    ]
    print(f"[demo] 启动 FacilityMind Dashboard → {url}")
    print(f"[demo] 命令：{' '.join(cmd)}")
    print("[demo] 按 Ctrl+C 退出")

    proc = subprocess.Popen(cmd, cwd=ROOT)
    try:
        # 等服务起来再开浏览器
        time.sleep(3)
        try:
            webbrowser.open(url)
        except Exception:
            print(f"[demo] 浏览器打开失败，请手动访问 {url}")
        proc.wait()
    except KeyboardInterrupt:
        print("\n[demo] 正在关闭服务…")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
