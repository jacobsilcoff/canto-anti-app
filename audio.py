import asyncio
import io
import logging

import edge_tts

log = logging.getLogger(__name__)

# edge-tts talks to a free, unofficial Microsoft endpoint over a websocket. It
# fails transiently — throttling under a burst, DRM/clock-skew 403s, dropped
# sockets — and a single attempt turns that into silent no-audio for the
# learner. Retry before giving up; the calls are ~1s, so this is cheap.
_ATTEMPTS = 3
_BACKOFF = (0.4, 1.2)
_TIMEOUT_S = 20

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
    "te": "te-IN-ShrutiNeural",
    "ja": "ja-JP-NanamiNeural",
    "bn": "bn-IN-TanishaaNeural",
    "ur": "ur-PK-UzmaNeural",
    "ar": "ar-SA-ZariyahNeural",
    "sw": "sw-KE-ZuriNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "fa": "fa-IR-DilaraNeural",
    "tr": "tr-TR-EmelNeural",
    "nl": "nl-NL-ColetteNeural",
    "pl": "pl-PL-ZofiaNeural",
    "sv": "sv-SE-SofieNeural",
    "nb": "nb-NO-PernilleNeural",
    "ro": "ro-RO-AlinaNeural",
    "uk": "uk-UA-PolinaNeural",
    "el": "el-GR-AthinaNeural",
    "th": "th-TH-PremwadeeNeural",
    "he": "he-IL-HilaNeural",
    "ga": "ga-IE-OrlaNeural",
}


def voice_for(lang: str) -> str:
    return VOICES.get(lang, VOICES["yue"])


def locale_for(lang: str) -> str:
    """BCP-47 locale for a language, or "" if we don't speak it.

    Derived from the voice name ("zh-HK-HiuMaanNeural" → "zh-HK") so adding a
    language stays "an entry in LANG_INFO + a voice" — there is no second table
    to keep in step. The browser's SpeechRecognition wants exactly this tag, and
    the voice already encodes the variety we speak TO the learner, which is the
    one they should be speaking back. Unknown languages return "" rather than
    falling back to Cantonese: mis-set, the recognizer transcribes into the
    wrong language entirely, and no speaking drill beats a wrong one.
    """
    voice = VOICES.get(lang)
    if not voice:
        return ""
    parts = voice.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else parts[0]


async def _generate_once(text: str, lang: str) -> bytes:
    # A Communicate object's stream() can only be consumed once, so each attempt
    # needs a fresh one.
    communicate = edge_tts.Communicate(text, voice_for(lang))
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()


async def generate(text: str, lang: str = "yue") -> bytes:
    """Synthesise `text` to MP3 bytes, retrying transient upstream failures.

    Raises the last error if every attempt fails — callers must decide what a
    silent card looks like, and empty bytes would be indistinguishable from a
    successful synthesis of silence. Never returns b"": some edge-tts versions
    finish an empty stream without raising, which would otherwise be stored as
    the card's audio and play as nothing at all.
    """
    last = None
    for attempt in range(_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_BACKOFF[min(attempt - 1, len(_BACKOFF) - 1)])
        try:
            data = await asyncio.wait_for(_generate_once(text, lang), _TIMEOUT_S)
            if data:
                return data
            last = RuntimeError("edge-tts returned no audio")
        except asyncio.CancelledError:
            raise
        except Exception as e:            # noqa: BLE001 — upstream is a grab-bag
            last = e
        log.warning("TTS attempt %d/%d failed for lang=%s: %r",
                    attempt + 1, _ATTEMPTS, lang, last)
    raise last
