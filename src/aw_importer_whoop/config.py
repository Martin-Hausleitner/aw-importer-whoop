from __future__ import annotations

from pathlib import Path
from platformdirs import user_config_dir

APP_NAME = "aw-importer-whoop"
DEFAULT_INTERVAL_SECONDS = 900
OVERLAP_SECONDS = 6 * 60 * 60
WHOOP_BASE_URL = "https://api.prod.whoop.com/developer"
WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
REDIRECT_URI = "http://127.0.0.1:8765/callback"
SCOPES = "offline read:recovery read:cycles read:sleep read:workout read:profile"
AW_BASE_URL = "http://127.0.0.1:5600/api/0"
DATA_TYPES = ("sleep", "workout", "cycle", "recovery")


def config_dir() -> Path:
    p = Path(user_config_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path() -> Path:
    return config_dir() / "state.json"


def token_path() -> Path:
    return config_dir() / "tokens.json"
