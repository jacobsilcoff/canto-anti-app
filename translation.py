import os
import json
import time
import asyncio
from google import genai
from google.genai.errors import ServerError

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


_MODEL = "gemini-2.5-flash-lite"


def _call(prompt: str) -> str:
    delays = [1, 3]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            time.sleep(delay)
        try:
            return _get_client().models.generate_content(model=_MODEL, contents=prompt).text.strip()
        except ServerError as e:
            if e.status_code == 503 and attempt < len(delays):
                continue
            raise


def _parse_json(text: str) -> dict:
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(text)


def _context_block(context: str) -> str:
    context = (context or "").strip()
    if not context:
        return ""
    return (
        "\nContext (use this to disambiguate the meaning, "
        "but do NOT include it in the translation output):\n"
        f"{context}\n"
    )


def _translate_en_to_yue(text: str, context: str = "") -> dict:
    prompt = (
        "Translate the following English text into Hong Kong Cantonese.\n"
        "Rules:\n"
        "- Use Traditional Chinese characters with authentic Cantonese vocabulary "
        "(食 not 吃, 唔 not 不, 係 not 是, 喺 not 在, 佢 not 他/她, etc.)\n"
        "- Provide jyutping romanisation for the Chinese output (e.g. nei5 hou2 aa3)\n"
        "Return ONLY valid JSON in this exact format, no other text:\n"
        '{"chinese": "...", "jyutping": "..."}\n'
        f"{_context_block(context)}\n"
        f"English: {text}"
    )
    return _parse_json(_call(prompt))


def _translate_yue_to_en(text: str, context: str = "") -> dict:
    prompt = (
        "Translate the following Hong Kong Cantonese text into natural English.\n"
        "Also provide the jyutping romanisation of the Cantonese input.\n"
        "Return ONLY valid JSON in this exact format, no other text:\n"
        '{"english": "...", "jyutping": "..."}\n'
        f"{_context_block(context)}\n"
        f"Cantonese: {text}"
    )
    return _parse_json(_call(prompt))


async def translate(text: str, source_lang: str, context: str = "") -> dict:
    if source_lang == "english":
        result = await asyncio.to_thread(_translate_en_to_yue, text, context)
        return {
            "english": text,
            "chinese": result["chinese"].strip(),
            "jyutping": result.get("jyutping", "").strip(),
        }
    else:
        result = await asyncio.to_thread(_translate_yue_to_en, text, context)
        return {
            "english": result["english"].strip(),
            "chinese": text,
            "jyutping": result.get("jyutping", "").strip(),
        }
