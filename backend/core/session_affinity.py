from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from backend.core.database import AsyncJsonDB


@dataclass(slots=True)
class SessionAffinityRecord:
    session_key: str
    surface: str
    account_email: str
    uploaded_files: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "surface": self.surface,
            "account_email": self.account_email,
            "uploaded_files": self.uploaded_files,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }


class SessionAffinityStore:
    def __init__(self, db: AsyncJsonDB):
        self.db = db
        self.records: dict[str, SessionAffinityRecord] = {}

    async def load(self):
        data = await self.db.load()
        self.records = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or not item.get("session_key"):
                    continue
                rec = SessionAffinityRecord(**item)
                self.records[rec.session_key] = rec

    async def save(self):
        await self.db.save([record.to_dict() for record in self.records.values()])

    async def get(self, session_key: str) -> SessionAffinityRecord | None:
        if not self.records:
            await self.load()
        record = self.records.get(session_key)
        if record and record.expires_at and record.expires_at < time.time():
            self.records.pop(session_key, None)
            await self.save()
            return None
        return record

    async def bind_account(self, session_key: str, surface: str, account_email: str, ttl_seconds: int) -> SessionAffinityRecord:
        now = time.time()
        record = self.records.get(session_key)
        if record is None:
            record = SessionAffinityRecord(session_key=session_key, surface=surface, account_email=account_email)
        record.surface = surface
        record.account_email = account_email
        record.updated_at = now
        record.expires_at = now + max(60, ttl_seconds)
        self.records[session_key] = record
        await self.save()
        return record

    async def add_uploaded_file(self, session_key: str, file_meta: dict[str, Any]) -> None:
        record = await self.get(session_key)
        if record is None:
            return
        record.uploaded_files.append(file_meta)
        record.updated_at = time.time()
        self.records[session_key] = record
        await self.save()

    async def clear(self, session_key: str) -> None:
        self.records.pop(session_key, None)
        await self.save()

    async def cleanup_expired(self) -> list[SessionAffinityRecord]:
        now = time.time()
        expired_keys = [key for key, record in self.records.items() if record.expires_at and record.expires_at < now]
        expired_records = [self.records[key] for key in expired_keys]
        for key in expired_keys:
            self.records.pop(key, None)
        if expired_keys:
            await self.save()
        return expired_records
