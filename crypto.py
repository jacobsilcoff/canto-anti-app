"""Symmetric encryption for secrets stored at rest (e.g. per-user Gemini API keys).

The DB only ever holds ciphertext; the key lives outside the DB so a leaked
database/backup is useless on its own. In production set API_KEY_ENC_KEY (a
Fernet key) as an env var. Locally, a key is generated and persisted next to
the DB so encrypted values survive restarts.
"""
import os
from cryptography.fernet import Fernet

_fernet: Fernet | None = None


def _load_key() -> bytes:
    env = os.environ.get("API_KEY_ENC_KEY")
    if env:
        return env.strip().encode()
    # Local/dev fallback: generate and persist a key beside the DB.
    db_path = os.environ.get("DB_PATH", "data/cards.db")
    data_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    key_path = os.path.join(data_dir, ".enc_key")
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    os.makedirs(data_dir, exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(key)
    os.chmod(key_path, 0o600)
    return key


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get().decrypt(token.encode()).decode()
