"""语音转写（ASR）抽象层。

- 当前为可离线测试的 stub：传入 .txt 直接当转写结果（演示/测试用）。
- 配置 ASR_BACKEND=funasr 时走真实本地 ASR（FunASR Paraformer，支持热词矫正）。
- 其余流程（意图闸门、后续 Agent）完全不变。
"""
import logging
import os

log = logging.getLogger("facilitymind.ingest.asr")

# 语言 → 默认 FunASR 模型；粤语模型可经 ASR_MODEL 覆盖具体 id
_LANG_MODEL = {
    "zh": "paraformer-zh",
    "yue": "iic/speech_paraformer_cantonese-large_asr_nat",
}

# 设施领域专有词（ASR 常听错，作为热词提升识别率）
_DOMAIN_GLOSSARY = [
    "光幕", "门机控制器", "轿厢", "厅门", "导轨",
    "风机盘管", "冷媒", "滤网", "空开", "跳闸",
    "喷淋", "烟感", "消火栓", "道闸", "门禁",
    "充电桩", "充电枪", "充电桩E03", "配电模块", "过温保护",
    "渗水", "爆管", "打压",
]


def _domain_hotwords() -> str:
    """合并知识库故障词 + 设施专有词，组成 FunASR 热词串。"""
    terms: list[str] = []
    try:
        from ..knowledge import TYPE_KEYWORDS
        for kws in TYPE_KEYWORDS.values():
            terms.extend(kws)
    except Exception as e:  # 知识库不可用时退化为仅用专有词
        log.warning("[ASR] 加载 TYPE_KEYWORDS 失败，热词仅含专有词: %s", e)
    terms.extend(_DOMAIN_GLOSSARY)
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out)


def transcribe(audio_path: str) -> str:
    """把音频/文本转成文本。

    - 传入 .txt：直接当转写稿返回（演示/测试用）。
    - 音频 + 同名 .txt 存在：读该 .txt（模拟 ASR 输出）。
    - 配置 ASR_BACKEND 且非 stub：调用对应真实 ASR。
    """
    if audio_path.endswith(".txt"):
        with open(audio_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    stem = os.path.splitext(audio_path)[0]
    sidecar = stem + ".txt"
    if os.path.exists(sidecar):
        with open(sidecar, "r", encoding="utf-8") as f:
            return f.read().strip()
    backend = os.environ.get("ASR_BACKEND", "stub").lower()
    if backend == "stub":
        raise RuntimeError(
            f"[ASR] stub 模式无法转写音频 {audio_path}：请放置同名 .txt 转写稿，"
            "或设置 ASR_BACKEND=funasr 接入真实本地 ASR。"
        )
    return _transcribe_real(audio_path, backend)


_FUN_MODEL = None  # 懒加载单例，避免重复下载模型


def _get_fun_model():
    global _FUN_MODEL
    if _FUN_MODEL is not None:
        return _FUN_MODEL
    try:
        from funasr import AutoModel
    except ImportError as e:
        raise RuntimeError(
            "[ASR] 未安装 funasr，请先 `pip install funasr modelscope` "
            "（会自动拉取 torch）。"
        ) from e

    lang = os.environ.get("ASR_LANG", "zh").lower()
    model_id = os.environ.get("ASR_MODEL") or _LANG_MODEL.get(lang, "paraformer-zh")
    revision = os.environ.get("ASR_MODEL_REVISION", "v2.0.4")
    device = os.environ.get("ASR_DEVICE", "cpu")
    log.info("[ASR][funasr] 加载模型 %s (revision=%s, device=%s)", model_id, revision, device)
    _FUN_MODEL = AutoModel(
        model=model_id,
        model_revision=revision,
        vad_model="fsmn-vad",
        vad_model_revision=revision,
        punc_model="ct-punc",
        punc_model_revision=revision,
        disable_update=True,
        device=device,
    )
    return _FUN_MODEL


def _transcribe_funasr(audio_path: str) -> str:
    model = _get_fun_model()
    hotword = _domain_hotwords()
    log.info("[ASR][funasr] 转写 %s（热词=%d 个领域词）", audio_path, len(hotword.split()))
    res = model.generate(
        input=audio_path,
        batch_size_s=300,
        hotword=hotword,
        sentencepiece_model="",
    )
    text = (res[0].get("text") or "").strip()
    if not text:
        raise RuntimeError(f"[ASR][funasr] 未识别出文本: {audio_path}")
    log.info("[ASR][funasr] 转写结果: %s", text)
    return text


def _transcribe_real(audio_path: str, backend: str) -> str:
    if backend in ("funasr", "funasr-zh", "funasr-yue", "paraformer"):
        return _transcribe_funasr(audio_path)
    raise NotImplementedError(f"真实 ASR 后端 {backend} 尚未实现。")
