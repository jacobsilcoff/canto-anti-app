import os
import json
import asyncio
from groq import Groq

_client: Groq | None = None

SYSTEM_PROMPT = """\
You are a Cantonese (廣東話) language expert specializing in colloquial Hong Kong Cantonese.

Return ONLY a valid JSON object — no markdown, no explanation — with exactly these fields:
- "english": the English text
- "chinese": Traditional Chinese characters using authentic Cantonese vocabulary and grammar (not Mandarin)
- "jyutping": Jyutping romanization with tone numbers, space-separated syllables

Rules for Chinese:
- Use Cantonese-specific characters: 係/唔/喺/咁/呢/囉/喎/㗎/啩/咋/啲/嘅 etc.
- Traditional characters only — never Simplified
- Natural conversational register as spoken in Hong Kong

Example output:
{"english": "Where are you going?", "chinese": "你去邊度呀？", "jyutping": "nei5 heoi3 bin1 dou6 aa3 ?"}\
"""


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


async def translate(text: str, source_lang: str) -> dict:
    if source_lang == "english":
        user_msg = f"Translate this English text to colloquial Hong Kong Cantonese:\n\n{text}"
    else:
        user_msg = f"This is Cantonese (characters or romanization). Provide the English translation, canonical Traditional Chinese characters, and Jyutping:\n\n{text}"

    def _call():
        return _get_client().chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )

    response = await asyncio.to_thread(_call)
    result = json.loads(response.choices[0].message.content)
    return {
        "english": result["english"].strip(),
        "chinese": result["chinese"].strip(),
        "jyutping": result["jyutping"].strip(),
    }
