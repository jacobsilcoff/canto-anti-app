"""Seed a small starter deck for new users so their first study session isn't empty."""

import audio
import db

# (source_text, target_text, romanization)
_WORDS: dict[str, list[tuple[str, str, str]]] = {
    "yue": [
        ("hello", "你好", "nei5 hou2"),
        ("thank you", "唔該", "m4 goi1"),
        ("yes", "係", "hai6"),
        ("no", "唔係", "m4 hai6"),
        ("I / me", "我", "ngo5"),
        ("you", "你", "nei5"),
        ("good", "好", "hou2"),
        ("eat", "食", "sik6"),
        ("drink", "飲", "jam2"),
        ("water", "水", "seoi2"),
        ("one", "一", "jat1"),
        ("person", "人", "jan4"),
    ],
    "cmn": [
        ("hello", "你好", "nǐ hǎo"),
        ("thank you", "谢谢", "xiè xie"),
        ("please", "请", "qǐng"),
        ("yes", "是", "shì"),
        ("no", "不是", "bù shì"),
        ("I / me", "我", "wǒ"),
        ("you", "你", "nǐ"),
        ("good", "好", "hǎo"),
        ("eat", "吃", "chī"),
        ("drink", "喝", "hē"),
        ("water", "水", "shuǐ"),
        ("one", "一", "yī"),
    ],
    "fr": [
        ("hello", "Bonjour", ""),
        ("thank you", "Merci", ""),
        ("please", "S'il vous plaît", ""),
        ("yes", "Oui", ""),
        ("no", "Non", ""),
        ("I / me", "Je", ""),
        ("you (informal)", "Tu", ""),
        ("good", "Bien", ""),
        ("eat", "Manger", ""),
        ("drink", "Boire", ""),
        ("water", "Eau", ""),
        ("one", "Un", ""),
    ],
    "es": [
        ("hello", "Hola", ""),
        ("thank you", "Gracias", ""),
        ("please", "Por favor", ""),
        ("yes", "Sí", ""),
        ("no", "No", ""),
        ("I / me", "Yo", ""),
        ("you (informal)", "Tú", ""),
        ("good", "Bueno", ""),
        ("eat", "Comer", ""),
        ("drink", "Beber", ""),
        ("water", "Agua", ""),
        ("one", "Uno", ""),
    ],
    "de": [
        ("hello", "Hallo", ""),
        ("thank you", "Danke", ""),
        ("please", "Bitte", ""),
        ("yes", "Ja", ""),
        ("no", "Nein", ""),
        ("I / me", "Ich", ""),
        ("you (informal)", "Du", ""),
        ("good", "Gut", ""),
        ("eat", "Essen", ""),
        ("drink", "Trinken", ""),
        ("water", "Wasser", ""),
        ("one", "Eins", ""),
    ],
}


async def seed(user_id: int, lang: str) -> None:
    """Create starter cards for user if they have no cards yet. Best-effort."""
    words = _WORDS.get(lang)
    if not words:
        return
    if await db.count_cards(user_id) > 0:
        return
    label_id = await db.get_or_create_label(user_id, "🌱 Starter")
    for source, target, roman in words:
        try:
            audio_data = await audio.generate(target, lang)
            await db.create_card(
                user_id=user_id,
                source_text=source,
                target_text=target,
                romanization=roman,
                target_lang=lang,
                audio_data=audio_data,
                priority=3,
                label_ids=[label_id],
            )
        except Exception:
            pass
