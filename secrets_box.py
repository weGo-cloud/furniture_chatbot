"""Encrypts secrets (e.g. WhatsApp tokens) before they touch SQLite, so DB backups don't leak them.
Generate a key once:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import settings


def _fernet():
    from cryptography.fernet import Fernet
    if not settings.SECRETS_KEY:
        raise ValueError("SECRETS_KEY is not set, so secrets cannot be stored. See .env.example.")
    return Fernet(settings.SECRETS_KEY.encode())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("Stored secret cannot be decrypted (wrong SECRETS_KEY?)")
