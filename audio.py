import io
import edge_tts

VOICE = "zh-HK-HiuMaanNeural"


async def generate(chinese_text: str) -> bytes:
    communicate = edge_tts.Communicate(chinese_text, VOICE)
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()
