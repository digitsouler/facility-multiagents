"""语音报修入口：音频 → ASR 转写 → 意图闸门 → 维修类则构造 raw ticket 接入后续流程。

设计要点：
- 语音只需产出 `raw` 文本，下游 Intake 及整条 7-Agent 流程都不用改；
- 意图闸门（gate）是入口校验：维修类放行进 Intake，其余提示不支持/追问；
- 真实 ASR 未接入时，用 .txt 转写稿模拟，保证流程可端到端验证。
"""
import logging
from datetime import datetime

from .asr import transcribe
from .gate import route_intent

log = logging.getLogger("facilitymind.ingest.voice")


def handle_voice(audio_path: str, run_pipeline: bool = True, ticket_id: str = None) -> dict:
    """处理一条语音报修。

    - 转写得到文本
    - 意图闸门判定
    - 维修类：构造 raw ticket；run_pipeline=True 时直接跑完整 7-Agent 流程
    - 非维修：返回 reason，不进流程
    """
    text = transcribe(audio_path)
    decision = route_intent(text)
    result = {
        "audio": audio_path,
        "text": text,
        "scene": decision["scene"],
        "is_ticket": decision["is_ticket"],
        "reason": decision["reason"],
    }
    if not decision["is_ticket"]:
        result["ok"] = False
        return result

    raw_ticket = {
        "id": ticket_id or ("V-" + datetime.now().strftime("%m%d%H%M%S")),
        "raw": text,
        "reporter": "语音报修",
        "location_hint": "",
    }
    result["raw_ticket"] = raw_ticket
    result["ok"] = True

    if run_pipeline:
        from ..eval import run_one

        result["pipeline"] = run_one(raw_ticket)
    return result
