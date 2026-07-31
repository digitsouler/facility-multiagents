"""演示语音报修接入。

默认（无参数）：用 samples/*.txt 模拟 ASR 转写稿，跑意图闸门 + （维修类）完整流程。
传入音频路径：直接用真实 ASR（FunASR）转写，再走同一流程。

用法（从项目根目录）：
    python voice_demo.py                      # 跑 .txt 样本
    python voice_demo.py 报修录音.wav         # 真实音频 + FunASR
    ASR_LANG=yue python voice_demo.py 粤语.wav   # 粤语模型
"""
import logging
import os
import sys

from facilitymind.ingest import handle_voice

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")


def _run_audio(audio_path: str):
    # 给定音频时强制走真实 ASR；除非用户已显式设置 ASR_BACKEND
    os.environ.setdefault("ASR_BACKEND", "funasr")
    print(f"\n========== 真实音频：{audio_path} ==========")
    print(f"[配置] ASR_BACKEND={os.environ.get('ASR_BACKEND')} "
          f"ASR_LANG={os.environ.get('ASR_LANG', 'zh')}")
    res = handle_voice(audio_path, run_pipeline=True, ticket_id="V-AUDIO")
    print(f"转写文本：{res['text']}")
    print(f"意图闸门：scene={res['scene']} is_ticket={res['is_ticket']} reason={res['reason']}")
    if res.get("ok"):
        p = res["pipeline"]
        print(
            f"→ 已接入后续流程：类型={p['type']} 紧急度={p['urgency']} "
            f"完成={p['completed']} QA={p['qa_passed']} 成本=¥{p['cost']:.0f} 步骤={p['steps']}"
        )
    else:
        print("→ 未进入流程（被意图闸门拦截）")


def _run_samples():
    for name in ["maintenance_elevator", "complaint", "chitchat"]:
        path = os.path.join(SAMPLES, name + ".txt")
        print(f"\n========== 样本：{name} ==========")
        run_full = name == "maintenance_elevator"
        res = handle_voice(path, run_pipeline=run_full, ticket_id=f"V-{name.upper()}")
        print(f"文本：{res['text']}")
        print(f"意图闸门：scene={res['scene']} is_ticket={res['is_ticket']} reason={res['reason']}")
        if res.get("ok"):
            p = res["pipeline"]
            print(
                f"→ 已接入后续流程：类型={p['type']} 紧急度={p['urgency']} "
                f"完成={p['completed']} QA={p['qa_passed']} 成本=¥{p['cost']:.0f} 步骤={p['steps']}"
            )
        else:
            print("→ 未进入流程（被意图闸门拦截）")


def main():
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        _run_audio(sys.argv[1])
    else:
        _run_samples()


if __name__ == "__main__":
    main()
