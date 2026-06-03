import io
import edge_tts

VOICES = {
    "yue": "zh-HK-HiuMaanNeural",
    "cmn": "zh-CN-XiaoxiaoNeural",
    "fr": "fr-FR-DeniseNeural",
    "es": "es-ES-ElviraNeural",
    "en": "en-US-AriaNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "tl": "fil-PH-BlessicaNeural",  # edge-tts exposes Tagalog as Filipino (fil-PH)
    "ms": "ms-MY-YasminNeural",
    "id": "id-ID-GadisNeural",
    "ko": "ko-KR-SunHiNeural",
    "hi": "hi-IN-SwaraNeural",
}


def voice_for(lang: str) -> str:
    return VOICES.get(lang, VOICES["yue"])


async def generate(text: str, lang: str = "yue") -> bytes:
    communicate = edge_tts.Communicate(text, voice_for(lang))
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()
