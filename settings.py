import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ENV = os.getenv("ENV", "development")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "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")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
# Optional second model, used automatically when the primary fails or is rate-limited.
LLM_FALLBACK_BASE_URL = os.getenv("LLM_FALLBACK_BASE_URL", "")
LLM_FALLBACK_API_KEY = os.getenv("LLM_FALLBACK_API_KEY", "")
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "gpt-4o-mini")

SUPER_ADMIN_KEY = os.getenv("SUPER_ADMIN_KEY", "_ZQuaL8604oWew03v4Z84dqEBcaisEbf9IocGtvJC4w")
SECRETS_KEY = os.getenv("SECRETS_KEY", "")  # Fernet key: encrypts stored WhatsApp tokens

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v22.0")

RETENTION_MESSAGE_DAYS = int(os.getenv("RETENTION_MESSAGE_DAYS", "90"))
RETENTION_LOG_DAYS = int(os.getenv("RETENTION_LOG_DAYS", "30"))


def production_problems() -> list[str]:
    """Checked at startup when ENV=production. The app refuses to boot if this is non-empty."""
    problems = []
    if len(SUPER_ADMIN_KEY) < 24 or SUPER_ADMIN_KEY.lower().startswith("change-me"):
        problems.append("SUPER_ADMIN_KEY must be a random string of 24+ characters")
    if not LLM_API_KEY:
        problems.append("LLM_API_KEY is not set")
    if SECRETS_KEY:
        try:
            from cryptography.fernet import Fernet
            Fernet(SECRETS_KEY.encode())
        except Exception:
            problems.append("SECRETS_KEY is not a valid Fernet key")
    return problems
