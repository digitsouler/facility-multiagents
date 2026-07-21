"""Web Dashboard 启动入口。

用法：
  python -m facilitymind.web                 # 默认 http://127.0.0.1:8000
  python -m facilitymind.web --port 9000     # 指定端口
  python -m facilitymind.web --host 0.0.0.0  # 对外暴露（局域网演示）

无 LLM Key 时引擎自动走规则模式，开箱即跑；配置 LLM_API_KEY 后自动切换为 LLM 推理。
"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="FacilityMind Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    print(f"FacilityMind Dashboard → http://{args.host}:{args.port}")
    uvicorn.run(
        "facilitymind.web.server:api",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
