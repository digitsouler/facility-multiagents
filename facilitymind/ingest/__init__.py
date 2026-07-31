"""语音/文本报修接入层：ASR 转写 + 意图闸门 + 接入后续流程。

使用方式：
    from facilitymind.ingest import handle_voice, route_intent, transcribe
    res = handle_voice("samples/maintenance_elevator.txt")  # 维修类会跑完整 7-Agent 流程
"""
from .voice import handle_voice
from .gate import route_intent
from .asr import transcribe

__all__ = ["handle_voice", "route_intent", "transcribe"]
