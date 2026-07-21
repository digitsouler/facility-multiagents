"""Web Dashboard 子包：在现有 LangGraph 引擎外套一层 FastAPI + 前端。

不改动任何 Agent 代码，仅通过 engine.stream() 把流水线事件实时推给浏览器。
"""
