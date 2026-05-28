import io
import edge_tts

VOICES = {
    "yue": "zh-HK-HiuMaanNeural",
    "cmn": "zh-CN-XiaoxiaoNeural",
    "fr": "fr-FR-DeniseNeural",
    "es": "es-ES-ElviraNeural",
    "en": "en-US-AriaNeural",
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
