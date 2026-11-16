"""API keys besides the main one: each app or phone gets its own, revocable, with scopes and a daily cap.

The main key (API_KEY, kept in the OS keychain by the app) can do everything. Extra keys live in
keys.json as sha256 hashes, so the file alone never lets anyone in; a key is shown once, when made.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Scope = Literal["local", "cloud"]  # local: models on this computer; cloud: providers that may spend credits


class KeyFields(BaseModel):
    """What the dashboard sets on a key."""

    name: str = Field(min_length=1, max_length=64)
    scopes: list[Scope] = Field(default_factory=lambda: ["local"], min_length=1)
    daily_cap: int | None = Field(default=None, ge=1, le=100_000)  # generations per 24 hours


class Key(KeyFields):
    id: str
    created: int = 0
    hash: str = ""

    @property
    def owner(self) -> bool:
        return self.id == "main"


MAIN = Key(id="main", name="Main key", scopes=["local", "cloud"])


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Keys:
    def __init__(self, path: Path | None, main_key: str) -> None:
        self.path = path
        self.main = main_key.strip()
        self.keys: dict[str, Key] = {}
        if path is not None and path.is_file():
            # A broken keys.json fails the host's start, like a broken catalog: better than every key quietly gone.
            self.keys = {k.id: k for k in (Key.model_validate(k) for k in json.loads(path.read_text("utf-8"))["keys"])}

    def find(self, token: str | None) -> Key | None:
        if not token:
            return None
        if self.main and hmac.compare_digest(token.encode("utf-8"), self.main.encode("utf-8")):
            return MAIN
        digest = _hash(token)
        return next((k for k in self.keys.values() if hmac.compare_digest(k.hash, digest)), None)

    def create(self, fields: KeyFields) -> tuple[Key, str]:
        token = "glint_" + secrets.token_urlsafe(32)
        key = Key(id=uuid.uuid4().hex[:12], created=int(time.time()), hash=_hash(token), **fields.model_dump())
        self.keys[key.id] = key
        self._save()
        return key, token

    def update(self, key_id: str, fields: KeyFields) -> Key | None:
        if key_id not in self.keys:
            return None
        self.keys[key_id] = self.keys[key_id].model_copy(update=fields.model_dump())
        self._save()
        return self.keys[key_id]

    def delete(self, key_id: str) -> bool:
        if self.keys.pop(key_id, None) is None:
            return False
        self._save()
        return True

    def _save(self) -> None:
        if self.path is None:
            return  # a host run on its own keeps extra keys in memory only
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"keys": [k.model_dump() for k in self.keys.values()]}, indent=2), "utf-8")
        tmp.replace(self.path)
