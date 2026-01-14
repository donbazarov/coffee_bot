import os
import json
from dataclasses import dataclass
from pathlib import Path

_CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "credentials.json"

def _load_token():
    if _CREDENTIALS_PATH.exists():
        with _CREDENTIALS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        token = data.get("bot_token")
        if token:
            return token
    return None
        
@dataclass
class BotConfig:
    token: str = _load_token()
        
    # ИСПОЛЬЗУЕМ SQLITE вместо PostgreSQL
    database_url: str = "sqlite:///coffee_quality.db"