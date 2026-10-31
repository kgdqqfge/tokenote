"""Tokenote application configuration."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Tokenote"
VERSION = "1.2.0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
DATA_DIR = Path(os.environ.get("TOKENOTE_DATA_DIR", LOCAL_APP_DATA / APP_NAME))
DB_PATH = DATA_DIR / "tokenote.db"
DEFAULT_REFRESH_SECONDS = 60


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


