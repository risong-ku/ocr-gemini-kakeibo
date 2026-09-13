"""Read process environment at startup, never during module import.

Local development must export its .env values before calling load_settings;
Lambda always supplies the process environment directly.
"""

import logging
import os
from dataclasses import dataclass, field

DEFAULT_TABLE_NAME = "kakeibo"
DEFAULT_AWS_REGION = "ap-northeast-1"
MIN_PASSPHRASE_LENGTH = 12
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    table_name: str
    bucket_name: str
    aws_region: str
    session_secret: str = field(repr=False)
    app_passcode: str = field(repr=False)
    gemini_api_key: str = field(repr=False)
    cookie_secure: bool = True


def required_env(name: str, *, allow_empty: bool = False) -> str:
    value = os.environ.get(name)
    if value is None:
        raise ValueError(f"{name} must be set")
    if not value and not allow_empty:
        raise ValueError(f"{name} must be set and nonempty")
    return value


def load_settings() -> Settings:
    """Load a fresh configuration, warning without rejecting short passphrases."""
    cookie_secure = os.environ.get("COOKIE_SECURE", "true").lower()
    if cookie_secure not in {"true", "false"}:
        raise ValueError("COOKIE_SECURE must be true or false")

    settings = Settings(
        table_name=os.environ.get("KAKEIBO_TABLE_NAME", DEFAULT_TABLE_NAME),
        bucket_name=required_env("KAKEIBO_BUCKET_NAME"),
        aws_region=os.environ.get("AWS_REGION", DEFAULT_AWS_REGION),
        session_secret=required_env("SESSION_SECRET"),
        app_passcode=required_env("APP_PASSCODE", allow_empty=True),
        gemini_api_key=required_env("GEMINI_API_KEY"),
        cookie_secure=cookie_secure == "true",
    )
    if len(settings.app_passcode) < MIN_PASSPHRASE_LENGTH:
        logger.warning(
            "APP_PASSCODE should contain at least %s characters", MIN_PASSPHRASE_LENGTH
        )
    return settings
