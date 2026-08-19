#!/usr/bin/env python3
"""
Session Manager Pro — single-file Telegram bot.

Authorized use only: operate accounts you own.
Secrets stay in `.env`. Session material is Fernet-encrypted in MongoDB.

Run locally:
    pip install -r requirements.txt
    python bot.py

Render Web Service:
    Start command: python bot.py
    After deploy, set KEEP_ALIVE_URL=https://YOUR-APP.onrender.com
    (or rely on RENDER_EXTERNAL_URL). The bot pings /health every 45s.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient, functions, types
from telethon.crypto import AuthKey
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    FreshResetAuthorisationForbiddenError,
    FrozenMethodInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.tl.functions.auth import LogOutRequest, ResetAuthorizationsRequest
from telethon.tl.functions.messages import DeleteHistoryRequest

# opentele raises BaseException("err") on Python 3.13+ (not Exception).
# Keep the bot alive even if TData conversion is unavailable.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*=false")
HAS_OPENTELE = False
_OPENTELE_ERROR: BaseException | None = None
API = None
UseCurrentSession = None
TDesktop = None
try:
    from opentele.api import API, UseCurrentSession
    from opentele.td import TDesktop

    HAS_OPENTELE = True
except BaseException as _opentele_exc:  # noqa: BLE001
    if isinstance(_opentele_exc, (KeyboardInterrupt, SystemExit)):
        raise
    HAS_OPENTELE = False
    _OPENTELE_ERROR = _opentele_exc

# ═══════════════════════════════════════════════════════════════════
# Config (all credentials live here — no .env file)
# ═══════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent

API_ID = 38174429
API_HASH = "45f03a04bfd3ce9d12c877b4295cf785"
BOT_TOKEN = "8622821649:AAGkkq6I8wjaglclsfR-Xm3afVb2BC24bzw"
OWNER_ID = 7929802589
MONGO_URI = "mongodb+srv://new-user_31raman:yxdU3mA0iM945h7n@cluster0.5iam4ce.mongodb.net/?appName=Cluster0"
MONGO_DB = "session_manager_pro"
ENC_KEY = "HDUCGwlhZbtZuzskpWxE_4e3FzuwYzu6MFRl2OPKv4s="
WORKDIR = ROOT / "data"
TMP_DIR = WORKDIR / "tmp"
BACKUP_DIR = WORKDIR / "backups"
CHECK_INTERVAL = 300
LOG_LEVEL = "INFO"

DC_IPV4 = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}
DC_PORT = 443
TELEGRAM_SERVICE_ID = 777000
SPAMBOT = "SpamBot"
PER_PAGE = 8
MAX_UPLOAD = 80 * 1024 * 1024
CONCURRENCY = 6
HOST_CAP = 500
IST = ZoneInfo("Asia/Kolkata")
BOT_USERNAME = "sessionmanagerpromaxbot"
EXTRA_ADMINS = [738363992]
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("PORT") or os.environ.get("WEB_PORT") or "10000")
KEEP_ALIVE_URL = (os.environ.get("KEEP_ALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "https://imflirter.onrender.com").strip()
KEEP_ALIVE_INTERVAL = int(os.environ.get("KEEP_ALIVE_INTERVAL") or "45")
PUBLIC_URL = KEEP_ALIVE_URL.rstrip("/")
WEBHOOK_SECRET = "smp" + ENC_KEY[:16].replace("_", "x").replace("-", "x")

PHONE_RE = re.compile(r"^\+?\d{7,15}$")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
OTP_RE = re.compile(r"\b(\d{5,6})\b")
_PYRO_FMT = ">BI?256sQ?"
_PYRO_OLD = ">B?256sI?"
_PYRO_OLD64 = ">B?256sQ?"
_PYRO_FMT64 = ">BI?256sQI?"

STATUS_EMOJI = {"active": "✅", "dead": "💀", "banned": "🚫", "frozen": "❄️", "unknown": "❓"}
SRP_NOTE = (
    "Two-step passwords go through Telegram's official SRP (salted hash). "
    "The cloud password is never stored."
)
FRESH_RESET = (
    "Telegram blocked a global reset because this login is under 24 hours old "
    "(FRESH_RESET_AUTHORISATION_FORBIDDEN). Wait 24h, or log out devices one-by-one."
)

WORKDIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(WORKDIR / "bot.log"), encoding="utf-8"),
    ],
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("smp")
log.info("Python %s", sys.version.replace("\n", " "))
if HAS_OPENTELE:
    log.info("opentele ready (TData import/export enabled)")
else:
    log.warning("opentele unavailable — TData import/export disabled: %s", _OPENTELE_ERROR)

fernet = Fernet(ENC_KEY.encode() if isinstance(ENC_KEY, str) else ENC_KEY)
locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
global_lock = asyncio.Lock()


# ═══════════════════════════════════════════════════════════════════
# Tiny helpers
# ═══════════════════════════════════════════════════════════════════

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt) -> str:
    if not dt:
        return "—"
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(dt)


def h(v: Any) -> str:
    return html_lib.escape("" if v is None else str(v))


def full_phone(phone: str | None) -> str:
    if not phone:
        return "—"
    digits = re.sub(r"\D", "", str(phone))
    if not digits:
        return str(phone)
    return "+" + digits


def mask_phone(phone: str | None) -> str:
    """Full number — workspace is private to owner/admins."""
    return full_phone(phone)


def acc_username(acc: dict) -> str:
    u = (acc.get("username") or "").strip().lstrip("@")
    return f"@{u}" if u else "—"


def estimate_reg_date(user_id) -> str:
    """Telegram does not expose official signup date. Estimate from user id."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return "—"
    if uid <= 0:
        return "—"
    from datetime import date, timedelta
    marks = [
        (1, date(2013, 8, 1)),
        (76_000, date(2013, 10, 1)),
        (1_000_000, date(2013, 12, 1)),
        (2_768_409, date(2014, 3, 1)),
        (7_679_610, date(2014, 8, 1)),
        (17_000_000, date(2015, 2, 1)),
        (45_000_000, date(2015, 9, 1)),
        (90_000_000, date(2016, 3, 1)),
        (150_000_000, date(2016, 9, 1)),
        (220_000_000, date(2017, 3, 1)),
        (310_000_000, date(2017, 10, 1)),
        (400_000_000, date(2018, 4, 1)),
        (500_000_000, date(2018, 10, 1)),
        (620_000_000, date(2019, 3, 1)),
        (750_000_000, date(2019, 9, 1)),
        (900_000_000, date(2020, 2, 1)),
        (1_100_000_000, date(2020, 7, 1)),
        (1_400_000_000, date(2020, 12, 1)),
        (1_700_000_000, date(2021, 4, 1)),
        (2_000_000_000, date(2021, 8, 1)),
        (2_400_000_000, date(2021, 12, 1)),
        (3_000_000_000, date(2022, 4, 1)),
        (4_000_000_000, date(2022, 9, 1)),
        (5_000_000_000, date(2023, 2, 1)),
        (5_500_000_000, date(2023, 7, 1)),
        (6_000_000_000, date(2023, 11, 1)),
        (6_400_000_000, date(2024, 3, 1)),
        (6_800_000_000, date(2024, 7, 1)),
        (7_200_000_000, date(2024, 11, 1)),
        (7_600_000_000, date(2025, 3, 1)),
        (8_000_000_000, date(2025, 8, 1)),
        (8_400_000_000, date(2026, 1, 1)),
        (8_800_000_000, date(2026, 6, 1)),
    ]
    if uid >= marks[-1][0]:
        return marks[-1][1].strftime("%b %Y") + " (est.)"
    prev_id, prev_d = marks[0]
    for mid, md in marks[1:]:
        if uid <= mid:
            span = max(mid - prev_id, 1)
            frac = max(0.0, min(1.0, (uid - prev_id) / span))
            est = prev_d + timedelta(days=int((md - prev_d).days * frac))
            return est.strftime("%b %Y") + " (est.)"
        prev_id, prev_d = mid, md
    return "—"


def acc_label(acc: dict, limit: int = 60) -> str:
    flag = STATUS_EMOJI.get(acc.get("status"), "❓")
    phone = full_phone(acc.get("phone"))
    name = acc.get("first_name") or acc.get("account_id")
    uname = acc_username(acc)
    bits = [flag]
    if phone != "—":
        bits.append(phone)
    bits.append(str(name))
    if uname != "—":
        bits.append(uname)
    text = " ".join(bits)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def norm_phone(raw: str) -> str:
    raw = raw.strip().replace(" ", "").replace("-", "")
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if not raw.startswith("+") and raw.isdigit():
        raw = "+" + raw
    return raw


def extract_hex_dc(raw: str) -> tuple[str, int | None]:
    text = raw.strip().replace("0x", "").replace("0X", "")
    dc = None
    hex_part = text
    for sep in ("|", ",", " ", ":", ";"):
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            if len(parts) >= 2:
                a, b = parts[0], parts[1]
                if a.isdigit() and 1 <= int(a) <= 5:
                    dc, hex_part = int(a), b
                elif b.isdigit() and 1 <= int(b) <= 5:
                    hex_part, dc = a, int(b)
            break
    hex_part = re.sub(r"[^0-9a-fA-F]", "", hex_part)
    return hex_part, dc


def is_hex_key(s: str) -> bool:
    return bool(HEX_RE.match(s)) and len(s) == 512


def extract_otp(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"(?:code|otp|парол)\D{0,12}(\d{5,6})", text, re.I)
    if m:
        return m.group(1)
    m = OTP_RE.search(text)
    return m.group(1) if m else None


def split_html(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, buf, size = [], [], 0
    for line in text.split("\n"):
        extra = len(line) + 1
        if buf and size + extra > limit:
            chunks.append("\n".join(buf))
            buf, size = [line], extra
        else:
            buf.append(line)
            size += extra
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def enc(s: str | None) -> str | None:
    if not s:
        return None
    return fernet.encrypt(s.encode()).decode()


def dec(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return fernet.decrypt(s.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Decrypt failed — is SESSION_ENC_KEY unchanged?") from exc


def find_key_datas(root: Path) -> Path | None:
    for p in (root / "key_datas", root / "tdata" / "key_datas"):
        if p.is_file():
            return p
    for m in root.rglob("key_datas"):
        if m.is_file():
            return m
    return None


def safe_unzip(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            total += info.file_size
            if total > 200_000_000:
                raise ValueError("Archive too large.")
        zf.extractall(dest)


# ═══════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════

class DB:
    def __init__(self) -> None:
        self.client: AsyncIOMotorClient | None = None
        self.db = None

    async def connect(self) -> None:
        self.client = AsyncIOMotorClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=20000)
        self.db = self.client[MONGO_DB]
        await self.client.admin.command("ping")
        await self.accounts.create_index("account_id", unique=True)
        await self.accounts.create_index("user_id")
        await self.admins.create_index("user_id", unique=True)
        await self.settings.create_index("key", unique=True)
        await self.admins.update_one(
            {"user_id": OWNER_ID},
            {
                "$set": {"role": "owner", "updated_at": utcnow()},
                "$setOnInsert": {"user_id": OWNER_ID, "added_by": OWNER_ID, "added_at": utcnow()},
            },
            upsert=True,
        )
        for extra in EXTRA_ADMINS:
            if int(extra) == OWNER_ID:
                continue
            await self.admins.update_one(
                {"user_id": int(extra)},
                {
                    "$setOnInsert": {
                        "user_id": int(extra),
                        "role": "admin",
                        "added_by": OWNER_ID,
                        "added_at": utcnow(),
                    }
                },
                upsert=True,
            )
        for k, v in {
            "alerts_logout": True,
            "alerts_ban": True,
            "check_interval": CHECK_INTERVAL,
            "monitor_enabled": True,
            "otp_read": 0,
            "add_ok": 0,
            "add_fail": 0,
            "accounts_deleted": 0,
        }.items():
            await self.settings.update_one({"key": k}, {"$setOnInsert": {"key": k, "value": v}}, upsert=True)
        log.info("MongoDB connected (%s)", MONGO_DB)

    async def close(self) -> None:
        if self.client:
            self.client.close()

    @property
    def accounts(self):
        return self.db["accounts"]

    @property
    def admins(self):
        return self.db["admins"]

    @property
    def settings(self):
        return self.db["settings"]

    @property
    def events(self):
        return self.db["events"]

    async def is_admin(self, uid: int) -> bool:
        if int(uid) == OWNER_ID:
            return True
        return await self.admins.find_one({"user_id": int(uid)}, {"_id": 1}) is not None

    async def is_owner(self, uid: int) -> bool:
        return int(uid) == OWNER_ID

    async def get_admins(self) -> list[dict]:
        return await self.admins.find({}).sort("role", 1).to_list(200)

    async def add_admin(self, uid: int, by: int) -> bool:
        if int(uid) == OWNER_ID:
            return False
        r = await self.admins.update_one(
            {"user_id": int(uid)},
            {"$setOnInsert": {"user_id": int(uid), "role": "admin", "added_by": int(by), "added_at": utcnow()}},
            upsert=True,
        )
        return bool(r.upserted_id)

    async def remove_admin(self, uid: int) -> bool:
        if int(uid) == OWNER_ID:
            return False
        r = await self.admins.delete_one({"user_id": int(uid), "role": {"$ne": "owner"}})
        return r.deleted_count > 0

    async def get_setting(self, key: str, default=None):
        doc = await self.settings.find_one({"key": key})
        return default if doc is None else doc.get("value", default)

    async def set_setting(self, key: str, value) -> None:
        await self.settings.update_one({"key": key}, {"$set": {"key": key, "value": value, "updated_at": utcnow()}}, upsert=True)

    async def inc(self, key: str, n: int = 1) -> int:
        doc = await self.settings.find_one_and_update(
            {"key": key},
            {"$inc": {"value": int(n)}, "$set": {"updated_at": utcnow()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        try:
            return int(doc.get("value") or 0)
        except (TypeError, ValueError):
            return 0

    async def toggle(self, key: str) -> bool:
        nxt = not bool(await self.get_setting(key, True))
        await self.set_setting(key, nxt)
        return nxt

    async def all_settings(self) -> dict:
        out = {}
        async for d in self.settings.find({}):
            out[d["key"]] = d.get("value")
        return out

    async def count_accounts(self) -> int:
        return await self.accounts.count_documents({})

    async def list_accounts(self, skip=0, limit=8) -> list[dict]:
        return await self.accounts.find({}).sort("added_at", -1).skip(skip).limit(limit).to_list(limit)

    async def all_accounts(self) -> list[dict]:
        return await self.accounts.find({}).sort("added_at", -1).to_list(10_000)

    async def get_account(self, aid: str) -> dict | None:
        return await self.accounts.find_one({"account_id": aid})

    async def get_by_tg(self, tg_id: int) -> dict | None:
        return await self.accounts.find_one({"user_id": int(tg_id)})

    async def insert_account(self, payload: dict, added_by: int) -> dict:
        now = utcnow()
        doc = {
            "account_id": payload.get("account_id") or secrets.token_hex(4),
            "user_id": payload.get("user_id"),
            "phone": payload.get("phone"),
            "username": payload.get("username"),
            "first_name": payload.get("first_name"),
            "last_name": payload.get("last_name"),
            "dc_id": payload.get("dc_id"),
            "api_id": payload.get("api_id") or API_ID,
            "auth_key_enc": enc(payload.get("auth_key_hex")),
            "telethon_string_enc": enc(payload.get("telethon_string")),
            "pyrogram_string_enc": enc(payload.get("pyrogram_string")),
            "status": payload.get("status") or "unknown",
            "last_check": payload.get("last_check"),
            "last_error": None,
            "spam_status": payload.get("spam_status"),
            "spam_checked_at": None,
            "twofa_hint": payload.get("twofa_hint"),
            "has_2fa": bool(payload.get("has_2fa", False)),
            "notes": payload.get("notes") or "",
            "source": payload.get("source") or "unknown",
            "added_by": int(added_by),
            "added_at": now,
            "updated_at": now,
        }
        if doc["user_id"]:
            old = await self.get_by_tg(doc["user_id"])
            if old:
                doc["account_id"] = old["account_id"]
                doc["added_at"] = old.get("added_at", now)
                await self.accounts.replace_one({"account_id": old["account_id"]}, doc)
                return doc
        await self.accounts.insert_one(doc)
        return doc

    async def update_account(self, aid: str, updates: dict) -> dict | None:
        updates = dict(updates)
        updates["updated_at"] = utcnow()
        return await self.accounts.find_one_and_update({"account_id": aid}, {"$set": updates}, return_document=ReturnDocument.AFTER)

    async def delete_account(self, aid: str) -> bool:
        return (await self.accounts.delete_one({"account_id": aid})).deleted_count > 0

    async def delete_all(self) -> int:
        return (await self.accounts.delete_many({})).deleted_count

    def secrets(self, acc: dict) -> dict:
        out = dict(acc)
        out["auth_key_hex"] = dec(acc.get("auth_key_enc"))
        out["telethon_string"] = dec(acc.get("telethon_string_enc"))
        out["pyrogram_string"] = dec(acc.get("pyrogram_string_enc"))
        return out

    async def log_event(self, kind, message, account_id=None, extra=None) -> None:
        await self.events.insert_one({"kind": kind, "message": message, "account_id": account_id, "extra": extra or {}, "created_at": utcnow()})

    async def recent_events(self, limit: int = 20) -> list[dict]:
        return await self.events.find({}).sort("created_at", -1).limit(limit).to_list(limit)

    async def export_workspace(self) -> dict:
        def dump(docs):
            out = []
            for d in docs:
                item = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items() if k != "_id"}
                out.append(item)
            return out

        return {
            "format": "session-manager-pro-v1",
            "exported_at": utcnow().isoformat(),
            "mongo_db": MONGO_DB,
            "accounts": dump(await self.accounts.find({}, {"_id": 0}).to_list(20_000)),
            "admins": dump(await self.admins.find({}, {"_id": 0}).to_list(500)),
            "settings": dump(await self.settings.find({}, {"_id": 0}).to_list(200)),
        }

    async def import_workspace(self, payload: dict) -> dict:
        if payload.get("format") != "session-manager-pro-v1":
            raise ValueError("Not a Session Manager Pro v1 backup.")
        stats = {"accounts": 0, "admins": 0, "settings": 0}
        for acc in payload.get("accounts") or []:
            if acc.get("account_id"):
                await self.accounts.update_one({"account_id": acc["account_id"]}, {"$set": acc}, upsert=True)
                stats["accounts"] += 1
        for adm in payload.get("admins") or []:
            if adm.get("user_id"):
                await self.admins.update_one({"user_id": int(adm["user_id"])}, {"$set": adm}, upsert=True)
                stats["admins"] += 1
        for st in payload.get("settings") or []:
            if st.get("key"):
                await self.settings.update_one({"key": st["key"]}, {"$set": st}, upsert=True)
                stats["settings"] += 1
        await self.admins.update_one({"user_id": OWNER_ID}, {"$set": {"role": "owner"}}, upsert=True)
        return stats


db = DB()


# ═══════════════════════════════════════════════════════════════════
# Session converters
# ═══════════════════════════════════════════════════════════════════

@dataclass
class SessionParts:
    dc_id: int
    auth_key: bytes
    user_id: int | None = None
    is_bot: bool = False
    api_id: int = 0
    test_mode: bool = False
    server_address: str | None = None
    port: int = DC_PORT
    source: str = "unknown"

    def __post_init__(self):
        if not self.api_id:
            self.api_id = API_ID

    @property
    def auth_key_hex(self) -> str:
        return self.auth_key.hex()

    def telethon_string(self) -> str:
        ip = self.server_address or DC_IPV4.get(self.dc_id) or DC_IPV4[2]
        ss = StringSession()
        ss.set_dc(int(self.dc_id), ip, int(self.port or DC_PORT))
        ss.auth_key = AuthKey(self.auth_key)
        return ss.save()

    def pyrogram_string(self) -> str:
        packed = struct.pack(
            _PYRO_FMT, int(self.dc_id), int(self.api_id or API_ID), bool(self.test_mode),
            self.auth_key, int(self.user_id or 0), bool(self.is_bot),
        )
        return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def _b64pad(s: str) -> bytes:
    s = s.strip().replace("\n", "").replace(" ", "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_telethon_string(session: str) -> SessionParts:
    ss = StringSession(session.strip())
    if not ss.auth_key or not ss.dc_id:
        raise ValueError("Not a valid Telethon StringSession.")
    return SessionParts(dc_id=int(ss.dc_id), auth_key=ss.auth_key.key,
                        server_address=ss.server_address, port=int(ss.port or DC_PORT),
                        source="telethon_string")


def parse_pyrogram_string(session: str) -> SessionParts:
    data = _b64pad(session)
    for fmt, kind in ((_PYRO_FMT, "pyro"), (_PYRO_FMT64, "alt"), (_PYRO_OLD64, "o64"), (_PYRO_OLD, "o32")):
        if len(data) != struct.calcsize(fmt):
            continue
        u = struct.unpack(fmt, data)
        if kind == "pyro":
            dc, api, test, key, uid, bot = u
        elif kind == "alt":
            dc, api, test, key, uid, bot, _ = u
        else:
            dc, test, key, uid, bot = u
            api = API_ID
        if 1 <= int(dc) <= 5 and len(key) == 256:
            return SessionParts(dc_id=int(dc), auth_key=bytes(key), user_id=int(uid) or None,
                                is_bot=bool(bot), api_id=int(api or API_ID), test_mode=bool(test),
                                source="pyrogram_string")
    raise ValueError("Not a valid Pyrogram session string.")


def parse_hex_dc(hex_key: str, dc_id: int) -> SessionParts:
    cleaned = "".join(c for c in hex_key if c in "0123456789abcdefABCDEF")
    if len(cleaned) != 512:
        raise ValueError(f"auth_key hex must be 512 chars, got {len(cleaned)}.")
    if dc_id not in DC_IPV4:
        raise ValueError("DC id must be 1–5.")
    return SessionParts(dc_id=int(dc_id), auth_key=bytes.fromhex(cleaned),
                        server_address=DC_IPV4[int(dc_id)], source="hex_dc")


def parse_session_file(path: Path) -> SessionParts:
    conn = sqlite3.connect(str(path))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in tables:
            raise ValueError("Not a Telegram session file.")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        row = conn.execute("SELECT * FROM sessions").fetchone()
        if not row:
            raise ValueError("Empty session file.")
        data = dict(zip(cols, row))
        key = data.get("auth_key")
        if not key:
            raise ValueError("No auth_key in file.")
        if "server_address" in cols:
            return SessionParts(dc_id=int(data["dc_id"]), auth_key=bytes(key),
                                server_address=data.get("server_address"),
                                port=int(data.get("port") or DC_PORT), source="telethon_file")
        return SessionParts(
            dc_id=int(data["dc_id"]), auth_key=bytes(key),
            user_id=int(data["user_id"]) if data.get("user_id") else None,
            is_bot=bool(data.get("is_bot")),
            api_id=int(data["api_id"]) if data.get("api_id") else API_ID,
            test_mode=bool(data.get("test_mode")), source="pyrogram_file",
        )
    finally:
        conn.close()


def write_pyro_file(path: Path, parts: SessionParts) -> Path:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, api_id INTEGER, test_mode INTEGER,
                auth_key BLOB, date INTEGER NOT NULL, user_id INTEGER, is_bot INTEGER);
            CREATE TABLE peers (id INTEGER PRIMARY KEY, access_hash INTEGER, type INTEGER NOT NULL,
                username TEXT, phone_number TEXT, last_update_on INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE version (number INTEGER PRIMARY KEY);
            """
        )
        conn.execute("INSERT INTO version VALUES (3)")
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
            (int(parts.dc_id), int(parts.api_id or API_ID), int(parts.test_mode),
             parts.auth_key, int(time.time()), int(parts.user_id or 0) or None, int(parts.is_bot)),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def write_tele_file(path: Path, parts: SessionParts) -> Path:
    from telethon.sessions import SQLiteSession

    out = path if path.suffix == ".session" else Path(str(path) + ".session")
    name = str(out.with_suffix(""))
    if out.exists():
        out.unlink()
    ip = parts.server_address or DC_IPV4.get(parts.dc_id) or DC_IPV4[2]
    sess = SQLiteSession(name)
    sess.set_dc(int(parts.dc_id), ip, int(parts.port or DC_PORT))
    sess.auth_key = AuthKey(parts.auth_key)
    sess.save()
    sess.close()
    return out


def parse_any(raw: str) -> SessionParts:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty input.")
    hx, dc = extract_hex_dc(text)
    if is_hex_key(hx) and dc:
        return parse_hex_dc(hx, dc)
    if is_hex_key(hx) and not dc:
        raise ValueError("HEX_NEEDS_DC")
    errs = []
    for fn, lab in ((parse_telethon_string, "telethon"), (parse_pyrogram_string, "pyrogram")):
        try:
            return fn(text)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{lab}: {e}")
    raise ValueError("Could not parse. " + " | ".join(errs))


# ═══════════════════════════════════════════════════════════════════
# Telethon clients + ops
# ═══════════════════════════════════════════════════════════════════

_DEV = dict(device_model="Session Manager Pro", system_version="Linux", app_version="1.0",
            lang_code="en", system_lang_code="en")


def classify_error(exc: BaseException) -> str | None:
    if isinstance(exc, (UserDeactivatedBanError, UserDeactivatedError)):
        return "banned"
    if isinstance(exc, (AuthKeyUnregisteredError, SessionRevokedError, AuthKeyDuplicatedError)):
        return "dead"
    if isinstance(exc, FrozenMethodInvalidError):
        return "frozen"
    name = type(exc).__name__
    if "Deactivated" in name or "Banned" in name:
        return "banned"
    if "AuthKeyUnregistered" in name or "SessionRevoked" in name:
        return "dead"
    if "Frozen" in name:
        return "frozen"
    return None


def make_user_client(session=None, api_id: int | None = None) -> TelegramClient:
    """Build a Telethon user client.

    opentele patches TelegramClient.__init__(session, api=...). A positional
    API_ID (int) then lands in `api` and later send_code_request blows up with
    "bytes or str expected, not int". Always pass api_id/api_hash by name.
    """
    if session is None:
        session = StringSession()
    elif isinstance(session, str):
        session = StringSession(session)
    aid = int(api_id or API_ID)
    ahash = str(API_HASH)
    client = TelegramClient(session, api_id=aid, api_hash=ahash, **_DEV)
    client.api_id = aid
    client.api_hash = ahash
    return client


def tg_from_string(s: str, api_id: int | None = None) -> TelegramClient:
    return make_user_client(s, api_id)


@asynccontextmanager
async def opened(client: TelegramClient, timeout: int = 45) -> AsyncIterator[TelegramClient]:
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        yield client
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass


async def inspect_client(client: TelegramClient) -> dict:
    if not await client.is_user_authorized():
        raise PermissionError("SESSION_UNAUTHORIZED")
    me = await client.get_me()
    ss = client.session
    key = ss.auth_key.key if ss.auth_key else None
    if not key:
        raise RuntimeError("No auth_key.")
    parts = SessionParts(dc_id=int(ss.dc_id), auth_key=key, user_id=int(me.id),
                         is_bot=bool(getattr(me, "bot", False)),
                         server_address=getattr(ss, "server_address", None),
                         port=int(getattr(ss, "port", None) or DC_PORT), source="live")
    return {
        "user_id": int(me.id), "phone": getattr(me, "phone", None), "username": me.username,
        "first_name": me.first_name, "last_name": me.last_name, "dc_id": parts.dc_id,
        "auth_key_hex": parts.auth_key_hex, "telethon_string": parts.telethon_string(),
        "pyrogram_string": parts.pyrogram_string(), "parts": parts, "me": me,
    }


async def inspect_parts(parts: SessionParts) -> dict:
    c = make_user_client(parts.telethon_string(), parts.api_id)
    async with opened(c):
        info = await inspect_client(c)
    info["source"] = parts.source
    return info


async def inspect_text(raw: str, dc_hint: int | None = None) -> dict:
    try:
        parts = parse_any(raw)
    except ValueError as e:
        if str(e) == "HEX_NEEDS_DC":
            if not dc_hint:
                raise
            hx, _ = extract_hex_dc(raw)
            parts = parse_hex_dc(hx, dc_hint)
        else:
            raise
    return await inspect_parts(parts)


async def zip_to_parts(zip_path: Path, passcode: str | None = None) -> list[SessionParts]:
    if not HAS_OPENTELE:
        raise RuntimeError(f"TData needs opentele+PyQt5 ({_OPENTELE_ERROR})")
    work = TMP_DIR / f"tdata_{zip_path.stem}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        safe_unzip(zip_path, work)
        key = find_key_datas(work)
        if not key:
            raise RuntimeError("No key_datas — not a Telegram Desktop tdata zip.")
        tdesk = TDesktop(str(key.parent), passcode=passcode or "")
        if not tdesk.isLoaded():
            raise RuntimeError("tdata loaded 0 accounts. Send the local passcode if it is locked.")
        accounts = list(getattr(tdesk, "accounts", []) or []) or [tdesk]
        out: list[SessionParts] = []
        tmp = TMP_DIR / "tdata_conv"
        tmp.mkdir(parents=True, exist_ok=True)
        for i, acc in enumerate(accounts):
            sp = tmp / f"td_{i}"
            for leftover in sp.parent.glob(sp.name + "*"):
                leftover.unlink(missing_ok=True)
            target = acc if hasattr(acc, "ToTelethon") else tdesk
            client = await target.ToTelethon(session=str(sp), flag=UseCurrentSession, api=API.TelegramDesktop)
            try:
                if not client.is_connected():
                    await client.connect()
                ss = client.session
                if not ss.auth_key:
                    raise RuntimeError("Converted client has no auth_key.")
                out.append(SessionParts(dc_id=int(ss.dc_id), auth_key=ss.auth_key.key,
                                        server_address=getattr(ss, "server_address", None),
                                        port=int(getattr(ss, "port", None) or DC_PORT), source="tdata"))
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                for leftover in sp.parent.glob(sp.name + "*"):
                    leftover.unlink(missing_ok=True)
        if not out:
            raise RuntimeError("No accounts converted.")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def account_to_tdata_zip(acc: dict) -> Path:
    if not HAS_OPENTELE:
        raise RuntimeError(f"TData export needs opentele+PyQt5 ({_OPENTELE_ERROR})")
    from opentele.tl import TelegramClient as OTClient

    sec = db.secrets(acc)
    if not sec.get("telethon_string"):
        raise RuntimeError("No session stored.")
    work = TMP_DIR / f"tdout_{acc['account_id']}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    tdata_dir = work / "tdata"
    client = OTClient(StringSession(sec["telethon_string"]), api=API.TelegramDesktop)
    try:
        if not client.is_connected():
            await client.connect()
        tdesk = await client.ToTDesktop(flag=UseCurrentSession)
        tdesk.SaveTData(str(tdata_dir))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    zip_path = TMP_DIR / f"{acc['account_id']}_tdata.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in tdata_dir.rglob("*"):
            if f.is_file():
                zf.write(f, str(f.relative_to(work)))
    shutil.rmtree(work, ignore_errors=True)
    return zip_path


async def check_spam(client) -> dict:
    try:
        await client.send_message(SPAMBOT, "/start")
    except FloodWaitError as e:
        await asyncio.sleep(min(int(e.seconds) + 1, 30))
        await client.send_message(SPAMBOT, "/start")
    await asyncio.sleep(1.3)
    msgs = await client.get_messages(SPAMBOT, limit=5)
    text = next((m.message for m in msgs if m and m.message), "")
    low = text.lower()
    if "no limits" in low or "good news" in low or "free as a bird" in low:
        flag = "clean"
    elif "limited" in low or "spam" in low or "restrict" in low:
        flag = "limited"
    else:
        flag = "unknown"
    return {"flag": flag, "text": text, "checked_at": utcnow()}


async def fetch_service(client, limit=12) -> list[dict]:
    out = []
    for msg in await client.get_messages(TELEGRAM_SERVICE_ID, limit=limit):
        if not msg:
            continue
        body = msg.message or ""
        out.append({"id": msg.id, "date": msg.date, "text": body, "otp": extract_otp(body)})
    return out


async def twofa_status(client) -> dict:
    pwd = await client(functions.account.GetPasswordRequest())
    return {"has_password": bool(pwd.has_password), "hint": pwd.hint or "",
            "has_recovery": bool(getattr(pwd, "has_recovery", False))}


async def list_devices(client) -> list[dict]:
    result = await client(GetAuthorizationsRequest())
    out = []
    for i, a in enumerate(result.authorizations):
        out.append({
            "index": i, "hash": int(a.hash), "current": bool(a.current),
            "device_model": a.device_model or "—", "platform": a.platform or "—",
            "system_version": a.system_version or "", "app_name": a.app_name or "—",
            "app_version": a.app_version or "", "ip": a.ip or "—",
            "country": a.country or "—", "region": a.region or "",
            "date_active": a.date_active, "date_created": a.date_created,
        })
    return out


def render_device(d: dict) -> str:
    n = d.get("index", 0) + 1
    star = " 🔹 CURRENT" if d.get("current") else ""
    loc = ", ".join(p for p in (d.get("country"), d.get("region")) if p and p != "—")
    return (
        f"<b>{n}.</b> {h(d.get('app_name'))} {h(d.get('app_version'))}{star}\n"
        f"    📱 {h(d.get('device_model'))} · {h(d.get('platform'))} {h(d.get('system_version'))}\n"
        f"    🌐 {h(d.get('ip'))} · {h(loc or '—')}\n"
        f"    ⏱ last: {h(iso(d.get('date_active')))}"
    )


async def kill_session(client) -> None:
    try:
        await client(LogOutRequest())
    except Exception:
        try:
            await client.log_out()
        except Exception:
            pass


async def terminate_others(client) -> dict:
    try:
        ok = await client(ResetAuthorizationsRequest())
        return {"ok": bool(ok), "error": None, "hint": None}
    except FreshResetAuthorisationForbiddenError:
        return {"ok": False, "error": "fresh_reset", "hint": FRESH_RESET}
    except Exception as e:  # noqa: BLE001
        if "FreshReset" in type(e).__name__ or "FRESH_RESET" in str(e):
            return {"ok": False, "error": "fresh_reset", "hint": FRESH_RESET}
        return {"ok": False, "error": type(e).__name__, "hint": str(e)}


def _is_service(entity) -> bool:
    return getattr(entity, "id", None) in {TELEGRAM_SERVICE_ID, 42777}


async def clear_dms(client, progress=None) -> dict:
    deleted = skipped = errors = 0
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if not isinstance(ent, types.User) or ent.bot or ent.is_self or _is_service(ent):
            skipped += 1
            continue
        try:
            await client.delete_dialog(ent, revoke=True)
            deleted += 1
        except Exception:
            errors += 1
        if progress and deleted % 8 == 0:
            await progress(deleted, errors)
        await asyncio.sleep(0.08)
    return {"deleted": deleted, "skipped": skipped, "errors": errors}


async def nuclear(client, progress=None) -> dict:
    stats = {"left_groups": 0, "cleared_bots": 0, "cleared_dms": 0, "errors": 0}
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        try:
            if isinstance(ent, types.User):
                if ent.is_self or _is_service(ent):
                    continue
                await client(DeleteHistoryRequest(peer=ent, max_id=0, just_clear=False, revoke=True))
                stats["cleared_bots" if ent.bot else "cleared_dms"] += 1
            else:
                try:
                    await client.delete_dialog(ent)
                except Exception:
                    try:
                        await client(functions.channels.LeaveChannelRequest(ent))
                    except Exception:
                        await client(DeleteHistoryRequest(peer=ent, max_id=0, just_clear=False, revoke=True))
                stats["left_groups"] += 1
        except Exception:
            stats["errors"] += 1
        total = stats["left_groups"] + stats["cleared_bots"] + stats["cleared_dms"]
        if progress and total % 6 == 0:
            await progress(stats)
        await asyncio.sleep(0.10)
    return stats


# ═══════════════════════════════════════════════════════════════════
# FSM + keyboards + cards
# ═══════════════════════════════════════════════════════════════════

@dataclass
class State:
    name: str
    data: dict = field(default_factory=dict)


class FSM:
    def __init__(self):
        self._s: dict[int, State] = {}
        self.live: dict[int, Any] = {}

    def get(self, uid: int) -> State | None:
        return self._s.get(int(uid))

    def set(self, uid: int, name: str, **data) -> State:
        cur = self._s.get(int(uid))
        merged = dict(cur.data) if cur else {}
        merged.update(data)
        st = State(name, merged)
        self._s[int(uid)] = st
        return st

    def clear(self, uid: int) -> None:
        self._s.pop(int(uid), None)
        live = self.live.pop(int(uid), None)
        if live:
            c = live.get("client") if isinstance(live, dict) else live
            if c is not None:
                try:
                    asyncio.get_running_loop().create_task(_disc(c))
                except Exception:
                    pass


async def _disc(c):
    try:
        if getattr(c, "is_connected", lambda: False)():
            await c.disconnect()
    except Exception:
        pass


fsm = FSM()


def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def main_menu(_owner=False):
    return InlineKeyboardMarkup([
        [B("Add Account+ 💎", "n:m"), B("My Account 📂", "a:l:0")],
        [B("Get Otp 🔐", "o:m"), B("Spam Checker 🛡", "p:m")],
        [B("TData 📦", "h:td"), B(".session file 📁", "h:sf")],
        [B("HEX + DC 🔑", "h:hx"), B("Security / 2FA 🔒", "s:m")],
        [B("Remove accounts 🗑", "h:rm"), B("Cleanup suite ✨", "k:m")],
        [B("String key convert 🧬", "h:sk"), B("Alert settings 🔔", "l:m")],
        [B("Download DB 💾", "d:dl"), B("Logs 📜", "e:logs")],
        [B("Reset DB ♻️", "a:wipe1"), B("Manage access 👑", "u:m")],
        [B("Health check 💚", "l:now"), B("Restore backup 📤", "d:up")],
        [B("Close ❌", "m:x")],
    ])


def back_main():
    return InlineKeyboardMarkup([[B("⬅️ Main menu", "m:main")]])


def add_menu():
    return InlineKeyboardMarkup([
        [B("📞 Phone + OTP", "n:ph"), B("🔑 Hex + DC", "n:hx")],
        [B("📁 .session file", "n:sf"), B("📄 String session", "n:st")],
        [B("📚 .txt bulk import", "n:tx"), B("📦 TData / TDF zip", "n:td")],
        [B("⬅️ Main menu", "m:main")],
    ])


def hub_kb(rows, back="m:main"):
    data = list(rows)
    data.append([B("Dashboard 🏠", back)])
    return InlineKeyboardMarkup(data)


def cancel_kb():
    return InlineKeyboardMarkup([[B("❌ Cancel", "m:cancel")]])


def accounts_kb(accounts, page, total, per):
    rows = []
    for acc in accounts:
        rows.append([B(acc_label(acc), f"a:v:{acc['account_id']}")])
    nav = []
    if page > 0:
        nav.append(B("⬅️", f"a:l:{page-1}"))
    if (page + 1) * per < total:
        nav.append(B("➡️", f"a:l:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([B("🗑 Remove ALL", "a:wipe1"), B("🔄 Refresh", f"a:l:{page}")])
    rows.append([B("Refresh dates 📅", "a:dnaA")])
    rows.append([B("⬅️ Main menu", "m:main")])
    return InlineKeyboardMarkup(rows)


def card_kb(i: str):
    return InlineKeyboardMarkup([
        [B("📩 OTP", f"o:1:{i}"), B("📊 Spam", f"p:1:{i}"), B("💓 Ping", f"a:p:{i}")],
        [B("🛡 2FA", f"s:2:{i}"), B("📱 Devices", f"s:dv:{i}")],
        [B("☠️ Kill session", f"s:k1:{i}"), B("🌍 Terminate others", f"s:t1:{i}")],
        [B("🧹 Clear DMs", f"k:d1:{i}"), B("☢️ Nuclear cleanup", f"k:n1:{i}")],
        [B("📄 String", f"c:ex:s:{i}"), B("🔑 Hex", f"c:ex:h:{i}"), B("📁 File", f"c:ex:f:{i}")],
        [B("📅 Refresh date", f"a:dna:{i}"), B("🗑 Remove", f"a:d1:{i}")],
        [B("⬅️ Accounts", "a:l:0")],
    ])


def confirm_kb(yes, no="a:l:0"):
    return InlineKeyboardMarkup([[B("✅ Confirm", yes), B("❌ Cancel", no)]])


def conv_menu():
    return InlineKeyboardMarkup([
        [B("📝 Paste any session → all formats", "c:paste")],
        [B("📁 Upload .session → all formats", "c:file")],
        [B("📦 TData zip → session / hex / string", "c:td")],
        [B("⬅️ Main menu", "m:main")],
    ])


def twofa_kb(i):
    return InlineKeyboardMarkup([
        [B("🟢 Enable 2FA", f"s:2e:{i}"), B("🔴 Disable 2FA", f"s:2d:{i}")],
        [B("✏️ Change password", f"s:2c:{i}")],
        [B("⬅️ Account", f"a:v:{i}")],
    ])


def devices_kb(aid, devices):
    rows = []
    for d in devices:
        if d.get("current"):
            continue
        rows.append([B(f"🚪 Logout #{d['index']+1} {str(d.get('app_name') or 'app')[:18]}", f"s:dl:{aid}:{d['index']}")])
    rows.append([B("🔄 Refresh", f"s:dv:{aid}"), B("⬅️ Account", f"a:v:{aid}")])
    return InlineKeyboardMarkup(rows)


def alerts_kb(s):
    def m(on):
        return "🟢 ON" if on else "🔴 OFF"
    return InlineKeyboardMarkup([
        [B(f"Logout / drop alerts — {m(bool(s.get('alerts_logout', True)))}", "l:tg:alerts_logout")],
        [B(f"Ban / deletion alerts — {m(bool(s.get('alerts_ban', True)))}", "l:tg:alerts_ban")],
        [B(f"Monitor loop — {m(bool(s.get('monitor_enabled', True)))}", "l:tg:monitor_enabled")],
        [B(f"⏱ Interval: {s.get('check_interval', 300)}s", "l:iv")],
        [B("▶ Run health check now", "l:now")],
        [B("⬅️ Main menu", "m:main")],
    ])


def db_kb(owner):
    rows = [[B("⬇️ Download backup (.json)", "d:dl")], [B("⬆️ Restore backup (.json)", "d:up")]]
    if owner:
        rows.append([B("🗑 Wipe ALL accounts", "a:wipe1")])
    rows.append([B("⬅️ Main menu", "m:main")])
    return InlineKeyboardMarkup(rows)


def admin_kb(owner):
    rows = []
    if owner:
        rows.append([B("➕ Add admin", "u:add"), B("➖ Remove admin", "u:del")])
    rows.append([B("🔄 Refresh", "u:m"), B("⬅️ Main menu", "m:main")])
    return InlineKeyboardMarkup(rows)


async def dashboard_stats() -> dict:
    """Live Mongo snapshot used by /start. Recalculated every time the dashboard is opened."""
    start_today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    hosted, online, dead, with_pw, spam, clean, logout, added_today, deleted, otp_read, add_fail, add_ok = await asyncio.gather(
        db.count_accounts(),
        db.accounts.count_documents({"status": "active"}),
        db.accounts.count_documents({"status": {"$in": ["dead", "banned"]}}),
        db.accounts.count_documents({"has_2fa": True}),
        db.accounts.count_documents({"spam_status": "limited"}),
        db.accounts.count_documents({"spam_status": "clean"}),
        db.accounts.count_documents({"status": "dead"}),
        db.accounts.count_documents({"added_at": {"$gte": start_today}}),
        db.get_setting("accounts_deleted", 0),
        db.get_setting("otp_read", 0),
        db.get_setting("add_fail", 0),
        db.get_setting("add_ok", 0),
    )
    return {
        "hosted": hosted,
        "online": online,
        "dead": dead,
        "with_pw": with_pw,
        "without_pw": max(hosted - with_pw, 0),
        "spam": spam,
        "clean": clean,
        "logout": logout,
        "deleted": int(deleted or 0),
        "otp_read": int(otp_read or 0),
        "add_fail": int(add_fail or 0),
        "add_ok": int(add_ok or 0),
        "added_today": added_today,
        "updated": datetime.now(IST).strftime("%d %b %Y · %I:%M %p"),
    }


async def start_text(username: str | None = None) -> str:
    s = await dashboard_stats()
    uname = username or BOT_USERNAME
    return (
        f"╰_╯ <b>@{h(uname)}</b> Session DASHBOARD v2.0 ❞\n\n"
        "📊 <b>Server Analytics (MongoDB):</b>\n"
        f"• Hosted Accounts: <b>{s['hosted']}</b> / {HOST_CAP}\n"
        f"• Online Accounts: <b>{s['online']}</b> 🟢\n"
        f"• Dead Sessions: <b>{s['dead']}</b> 🔴\n\n"
        "⚙️ <b>Session Configuration:</b>\n"
        f"• Accounts with Password: <b>{s['with_pw']}</b>\n"
        f"• Account without Password: <b>{s['without_pw']}</b>\n"
        f"• Spammed Account: <b>{s['spam']}</b>\n"
        f"• Non-Spam Account: <b>{s['clean']}</b>\n"
        f"• Account logout: <b>{s['logout']}</b>\n"
        f"• Accounts Deleted: <b>{s['deleted']}</b>\n\n"
        "📈 <b>Global Statistics:</b>\n"
        f"• Total otp readed: <b>{s['otp_read']}</b>\n"
        f"• Failed account adding: <b>{s['add_fail']}</b>\n"
        f"• Successful account added: <b>{s['add_ok']}</b>\n"
        f"• Account added today: <b>{s['added_today']}</b>\n\n"
        f"<i>Updated {h(s['updated'])} IST</i>\n\n"
        "╰_╯ Choose an action below ❞"
    )


def help_text() -> str:
    return (
        "<b>Feature map</b>\n\n"
        "📱 Accounts · ➕ Add (phone/hex/file/string/txt/tdata)\n"
        "🔄 Converters · 🛡 2FA / kill / terminate / devices\n"
        "🧹 Clear DMs / nuclear · 📊 @SpamBot · 📩 777000 OTP\n"
        "🔔 Alerts · 💾 encrypted JSON backup · 👑 admins\n\n"
        f"<i>{SRP_NOTE}</i>"
    )


def account_card(acc: dict) -> str:
    emoji = STATUS_EMOJI.get(acc.get("status"), "❓")
    uname = acc_username(acc)
    full = " ".join(p for p in (acc.get("first_name"), acc.get("last_name")) if p) or "—"
    phone = full_phone(acc.get("phone"))
    stored = (acc.get("reg_date") or "").strip()
    registered = stored if stored else estimate_reg_date(acc.get("user_id"))
    if stored:
        registered = f"{stored} · @TGDNAbot"
    return (
        f"{emoji} <b>{h(full)}</b>\n"
        f"ID: <code>{h(acc.get('account_id'))}</code>\n"
        f"Telegram: <code>{acc.get('user_id') or '—'}</code>\n"
        f"Username: <code>{h(uname)}</code>\n"
        f"Phone: <code>{h(phone)}</code>\n"
        f"Registered: <code>{h(registered)}</code>\n"
        f"DC: <code>{acc.get('dc_id') or '—'}</code>\nStatus: <code>{h(acc.get('status'))}</code>\n"
        f"Spam: <code>{h(acc.get('spam_status') or '—')}</code>\n"
        f"Source: <code>{h(acc.get('source') or '—')}</code>\n"
        f"Added by: <code>{acc.get('added_by')}</code> · {h(iso(acc.get('added_at')))}\n"
        f"Last check: {h(iso(acc.get('last_check')))}\n"
        f"Last error: <code>{h((acc.get('last_error') or '—')[:180])}</code>"
    )


def conv_card(parts: SessionParts) -> str:
    return (
        f"🔄 <b>Converted</b> ({h(parts.source)})\n\n"
        f"DC: <code>{parts.dc_id}</code>\n"
        f"User id: <code>{parts.user_id or 'unknown until login'}</code>\n\n"
        f"<b>Hex key</b>\n<code>{parts.auth_key_hex}</code>\n\n"
        f"<b>Telethon string</b>\n<code>{h(parts.telethon_string())}</code>\n\n"
        f"<b>Pyrogram string</b>\n<code>{h(parts.pyrogram_string())}</code>"
    )



# ═══════════════════════════════════════════════════════════════════
# Bot API (Render-safe receive path — pyrogram MTProto updates drop here)
# ═══════════════════════════════════════════════════════════════════

def _markup_json(markup) -> str | None:
    if markup is None:
        return None
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return None
    out = []
    for row in rows:
        out.append([{"text": b.text, "callback_data": getattr(b, "callback_data", "")} for b in row])
    return json.dumps({"inline_keyboard": out})


def botapi_call(method: str, **params) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    clean = {k: v for k, v in params.items() if v is not None}
    data = urllib.parse.urlencode(clean).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        log.warning("Bot API %s failed %s %s", method, exc.code, body[:300])
        raise RuntimeError(f"Telegram API {method}: {body[:200]}") from exc


async def botapi_async(method: str, **params) -> dict:
    return await asyncio.to_thread(botapi_call, method, **params)


async def botapi_send(chat_id: int, text: str, reply_markup=None) -> dict:
    return await botapi_async(
        "sendMessage",
        chat_id=int(chat_id),
        text=text,
        parse_mode="HTML",
        reply_markup=_markup_json(reply_markup),
        disable_web_page_preview="true",
    )


def botapi_download_sync(file_id: str, dest: Path) -> Path:
    if not file_id:
        raise RuntimeError("No file_id on document.")
    info = botapi_call("getFile", file_id=file_id)
    rel = ((info.get("result") or {}).get("file_path")) or ""
    if not rel:
        raise RuntimeError("Telegram getFile returned no path.")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{rel}"
    urllib.request.urlretrieve(url, str(dest))
    return dest


async def botapi_download(file_id: str | None, dest: Path) -> Path:
    return await asyncio.to_thread(botapi_download_sync, file_id or "", Path(dest))


def botapi_send_document_sync(chat_id: int, path: str | Path, caption: str | None = None) -> dict:
    path = Path(path)
    crlf = chr(13) + chr(10)
    boundary = secrets.token_hex(16)
    filename = path.name.replace('"', "_")
    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}{crlf}Content-Disposition: form-data; name=\"{name}\"{crlf}{crlf}{value}{crlf}"
        ).encode()
    chunks = [field("chat_id", str(int(chat_id))), field("parse_mode", "HTML")]
    if caption:
        chunks.append(field("caption", caption))
    header = (
        f"--{boundary}{crlf}Content-Disposition: form-data; name=\"document\"; "
        f"filename=\"{filename}\"{crlf}Content-Type: application/octet-stream{crlf}{crlf}"
    ).encode()
    chunks.append(header + path.read_bytes() + crlf.encode() + f"--{boundary}--{crlf}".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


async def botapi_send_document(chat_id: int, path: str | Path, caption: str | None = None) -> dict:
    return await asyncio.to_thread(botapi_send_document_sync, chat_id, path, caption)


class _ApiUser:
    def __init__(self, d: dict):
        self.id = int(d.get("id") or 0)
        self.first_name = d.get("first_name")
        self.last_name = d.get("last_name")
        self.username = d.get("username")
        self.is_self = False
        self.is_bot = bool(d.get("is_bot"))


class _ApiChat:
    def __init__(self, d: dict):
        self.id = int(d.get("id") or 0)
        self.type = d.get("type") or "private"


class _ApiDoc:
    def __init__(self, d: dict):
        self.file_id = d.get("file_id")
        self.file_unique_id = d.get("file_unique_id") or "file"
        self.file_name = d.get("file_name")
        self.file_size = d.get("file_size")


class ApiMessage:
    def __init__(self, d: dict):
        self._raw = d
        self.id = int(d.get("message_id") or 0)
        self.from_user = _ApiUser(d["from"]) if d.get("from") else None
        self.chat = _ApiChat(d.get("chat") or {})
        self.text = d.get("text")
        self.caption = d.get("caption")
        self.document = _ApiDoc(d["document"]) if d.get("document") else None
        self.outgoing = False

    async def reply_text(self, text, reply_markup=None):
        res = await botapi_send(self.chat.id, text, reply_markup)
        mid = ((res.get("result") or {}).get("message_id"))
        if mid:
            return ApiMessage({"message_id": mid, "chat": {"id": self.chat.id}, "from": {"id": 0, "is_bot": True}})
        return self

    async def reply_document(self, path, caption=None):
        await botapi_send_document(self.chat.id, path, caption)
        return self

    async def edit_text(self, text, reply_markup=None):
        await botapi_async(
            "editMessageText",
            chat_id=self.chat.id,
            message_id=self.id,
            text=text,
            parse_mode="HTML",
            reply_markup=_markup_json(reply_markup),
            disable_web_page_preview="true",
        )
        return self

    async def delete(self):
        try:
            await botapi_async("deleteMessage", chat_id=self.chat.id, message_id=self.id)
        except Exception:
            pass


class ApiCallback:
    def __init__(self, d: dict):
        self.id = str(d.get("id") or "")
        self.from_user = _ApiUser(d.get("from") or {})
        self.data = d.get("data") or ""
        self.message = ApiMessage(d["message"]) if d.get("message") else None

    async def answer(self, text="", show_alert=False):
        try:
            await botapi_async(
                "answerCallbackQuery",
                callback_query_id=self.id,
                text=text or "",
                show_alert="true" if show_alert else "false",
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# Auth / broadcast / persist
# ═══════════════════════════════════════════════════════════════════

def _is_callback(event) -> bool:
    return hasattr(event, "data") and hasattr(event, "answer")


def actor_id(event) -> int | None:
    user = getattr(event, "from_user", None)
    if user is not None and not getattr(user, "is_self", False) and not getattr(user, "is_bot", False):
        return int(user.id)
    if getattr(event, "chat", None) is not None:
        return int(event.chat.id)
    msg = getattr(event, "message", None)
    if msg is not None and getattr(msg, "chat", None) is not None:
        return int(msg.chat.id)
    return None


async def ensure_admin(event) -> bool:
    if (getattr(event, "outgoing", False) or (getattr(event, "from_user", None) and event.from_user.is_self)) and not _is_callback(event):
        return False
    uid = actor_id(event)
    if uid is None:
        return False
    if await db.is_admin(uid):
        return True
    if _is_callback(event):
        await event.answer(f"Private bot. Your id: {uid}", show_alert=True)
    else:
        await event.reply_text(f"⛔️ This bot is private.\nYour Telegram user id: <code>{uid}</code>")
    return False


async def broadcast(bot, text: str, exclude: int | None = None) -> None:
    for adm in await db.get_admins():
        uid = int(adm["user_id"])
        if exclude is not None and uid == int(exclude):
            continue
        try:
            await botapi_send(uid, text)
        except Exception:
            pass


async def persist(bot: Client, event, info: dict, source: str, quiet=False, has_2fa=False) -> dict:
    user = event.from_user
    doc = await db.insert_account({
        "user_id": info["user_id"], "phone": info.get("phone"), "username": info.get("username"),
        "first_name": info.get("first_name"), "last_name": info.get("last_name"),
        "dc_id": info.get("dc_id"), "api_id": API_ID,
        "auth_key_hex": info["auth_key_hex"], "telethon_string": info["telethon_string"],
        "pyrogram_string": info["pyrogram_string"], "status": "active", "source": source,
        "has_2fa": bool(has_2fa or info.get("has_2fa")),
    }, added_by=user.id)
    await db.inc("add_ok", 1)
    if not quiet:
        label = info.get("first_name") or info.get("phone") or info.get("user_id")
        chat = event.chat.id if getattr(event, "chat", None) is not None else event.message.chat.id
        await botapi_send(chat, f"✅ Stored <b>{h(label)}</b> (<code>{info['user_id']}</code>)\n"
                          f"Workspace id: <code>{doc['account_id']}</code> · DC {info.get('dc_id')}")
        await broadcast(bot, f"🔄 {h(user.first_name)} added an account via {source}\n"
                        f"<b>{h(label)}</b> · <code>{doc['account_id']}</code>", exclude=user.id)
    chat_id = None
    try:
        chat_id = event.chat.id if getattr(event, "chat", None) is not None else event.message.chat.id
    except Exception:
        chat_id = None
    asyncio.create_task(_dna_after_add(doc, None if quiet else chat_id))
    return doc


async def download_doc(bot: Client, message: Message, suffix: str) -> Path | None:
    doc = message.document
    if not doc:
        await message.reply_text("Please send a file.")
        return None
    if doc.file_size and doc.file_size > MAX_UPLOAD:
        await message.reply_text("File is too large.")
        return None
    dest = TMP_DIR / f"{message.from_user.id}_{doc.file_unique_id}{suffix}"
    await botapi_download(getattr(doc, "file_id", None), dest)
    return dest


async def client_for(aid: str):
    acc = await db.get_account(aid)
    if not acc:
        raise RuntimeError("Account not found.")
    sec = db.secrets(acc)
    if not sec.get("telethon_string"):
        raise RuntimeError("No session stored.")
    return acc, tg_from_string(sec["telethon_string"], acc.get("api_id"))


TGDNA = "TGDNAbot"
_DNA_CREATED = re.compile(r"Created:\s*([0-9]{4}-[0-9]{2}(?:-[0-9]{2})?)", re.I)
_DNA_USER = re.compile(r"Username:\s*(@?[A-Za-z0-9_]{3,}|—|-|None)", re.I)
_DNA_PREM = re.compile(r"Premium:\s*([^\n]+)", re.I)
_DNA_DC = re.compile(r"DC:\s*(\d+)", re.I)


def parse_tgdna(text: str) -> dict:
    out = {"reg_date": None, "username": None, "premium": None, "dc_id": None, "raw": text or ""}
    if not text:
        return out
    m = _DNA_CREATED.search(text)
    if m:
        out["reg_date"] = m.group(1)
    m = _DNA_USER.search(text)
    if m:
        u = m.group(1).strip()
        if u and u.lower() not in {"—", "-", "none", "n/a"}:
            out["username"] = u.lstrip("@")
    m = _DNA_PREM.search(text)
    if m:
        out["premium"] = m.group(1).strip()[:40]
    m = _DNA_DC.search(text)
    if m:
        out["dc_id"] = int(m.group(1))
    return out


async def _tgdna_wait(client, min_id: int, timeout: float = 12) -> str:
    deadline = time.time() + timeout
    best = ""
    while time.time() < deadline:
        await asyncio.sleep(0.4)
        msgs = await client.get_messages(TGDNA, limit=8)
        for msg in msgs:
            if not msg or not getattr(msg, "message", None):
                continue
            if getattr(msg, "out", False):
                continue
            if getattr(msg, "id", 0) <= min_id:
                continue
            best = msg.message
            if "Created:" in best or "Created :" in best:
                return best
    return best


async def tgdna_query_self(client, user_id: int) -> dict:
    """This user account itself /start @TGDNAbot — not a shared worker."""
    try:
        last = await client.get_messages(TGDNA, limit=1)
        min_id = int(last[0].id) if last and last[0] else 0
    except Exception:
        min_id = 0
    try:
        await client.send_message(TGDNA, "/start")
    except FloodWaitError as e:
        await asyncio.sleep(min(int(e.seconds) + 1, 25))
        await client.send_message(TGDNA, "/start")
    text = await _tgdna_wait(client, min_id, 10)
    parsed = parse_tgdna(text)
    if parsed.get("reg_date"):
        return parsed
    # Some builds only return the card after you send your own id.
    try:
        last = await client.get_messages(TGDNA, limit=1)
        min_id = int(last[0].id) if last and last[0] else min_id
    except Exception:
        pass
    await client.send_message(TGDNA, str(int(user_id)))
    text = await _tgdna_wait(client, min_id, 10)
    return parse_tgdna(text)


async def apply_dna(acc: dict, parsed: dict) -> dict:
    updates = {}
    if parsed.get("reg_date"):
        updates["reg_date"] = parsed["reg_date"]
        updates["reg_source"] = "tgdna"
        updates["reg_checked_at"] = utcnow()
    if parsed.get("username") and not acc.get("username"):
        updates["username"] = parsed["username"]
    if parsed.get("premium"):
        updates["premium"] = parsed["premium"]
    if parsed.get("dc_id") and not acc.get("dc_id"):
        updates["dc_id"] = parsed["dc_id"]
    if updates:
        await db.update_account(acc["account_id"], updates)
        acc.update(updates)
    return acc


async def dna_refresh_one(acc: dict) -> dict:
    if not acc.get("user_id"):
        raise RuntimeError("No Telegram user id on this account.")
    _, tg = await client_for(acc["account_id"])
    async with opened(tg, timeout=40):
        parsed = await tgdna_query_self(tg, int(acc["user_id"]))
    if not parsed.get("reg_date"):
        raise RuntimeError("This account /start @TGDNAbot but no Created date came back.")
    await apply_dna(acc, parsed)
    return parsed


async def dna_refresh_all(progress=None) -> dict:
    """Every stored account opens @TGDNAbot from ITS OWN session."""
    accs = await db.all_accounts()
    stats = {"ok": 0, "fail": 0, "total": len(accs)}
    sem = asyncio.Semaphore(2)

    async def one(i, acc):
        try:
            async with sem:
                parsed = await dna_refresh_one(acc)
            if parsed.get("reg_date"):
                stats["ok"] += 1
            else:
                stats["fail"] += 1
        except Exception:
            log.exception("dna self-start %s", acc.get("account_id"))
            stats["fail"] += 1
        if progress:
            await progress(i, stats)

    for i, acc in enumerate(accs, 1):
        await one(i, acc)
        await asyncio.sleep(0.25)
    return stats


_dna_inflight: set[str] = set()
_dna_scan_lock = asyncio.Lock()


async def dna_autofill_missing() -> dict:
    """No user command — each account missing a date /start @TGDNAbot itself."""
    stats = {"ok": 0, "fail": 0, "skip": 0}
    async with _dna_scan_lock:
        accs = await db.all_accounts()
        pending = [a for a in accs if a.get("user_id") and not (a.get("reg_date") or "").strip()]
        stats["skip"] = len(accs) - len(pending)
        for acc in pending:
            aid = acc["account_id"]
            if aid in _dna_inflight:
                continue
            _dna_inflight.add(aid)
            try:
                parsed = await dna_refresh_one(acc)
                if parsed.get("reg_date"):
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
            except Exception as exc:
                stats["fail"] += 1
                log.warning("auto dna %s: %s", aid, exc)
            finally:
                _dna_inflight.discard(aid)
            await asyncio.sleep(0.3)
    log.info("auto DNA fill %s", stats)
    return stats


async def _dna_after_add(doc: dict, chat_id: int | None) -> None:
    aid = doc.get("account_id")
    if not aid:
        return
    if aid in _dna_inflight:
        return
    _dna_inflight.add(aid)
    try:
        parsed = await dna_refresh_one(doc)
        if chat_id and parsed.get("reg_date"):
            extra = f" · @{parsed['username']}" if parsed.get("username") else ""
            await botapi_send(chat_id, f"📅 Registered: <code>{h(parsed['reg_date'])}</code>{h(extra)}")
    except Exception as exc:
        log.warning("dna after add %s: %s", aid, exc)
    finally:
        _dna_inflight.discard(aid)


async def _dna_loop() -> None:
    await asyncio.sleep(12)
    while True:
        try:
            await dna_autofill_missing()
        except Exception:
            log.exception("dna loop")
        await asyncio.sleep(180)




# ═══════════════════════════════════════════════════════════════════
# Monitor
# ═══════════════════════════════════════════════════════════════════

class Monitor:
    def __init__(self, bot: Client):
        self.bot = bot
        self._task = None
        self._stop = asyncio.Event()
        self._busy = False

    def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _loop(self):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                if await db.get_setting("monitor_enabled", True):
                    await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor pass")
            interval = max(60, min(int(await db.get_setting("check_interval", CHECK_INTERVAL) or 300), 86400))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> dict:
        if self._busy:
            return {"skipped": True}
        self._busy = True
        summary = {"checked": 0, "active": 0, "dead": 0, "banned": 0, "frozen": 0, "errors": 0}
        try:
            accs = await db.all_accounts()
            sem = asyncio.Semaphore(CONCURRENCY)

            async def one(acc):
                async with sem:
                    await self._check(acc, summary)

            await asyncio.gather(*(one(a) for a in accs), return_exceptions=True)
            log.info("Monitor %s", summary)
            return summary
        finally:
            self._busy = False

    async def _check(self, acc, summary):
        summary["checked"] += 1
        prev = acc.get("status")
        sec = db.secrets(acc)
        if not sec.get("telethon_string"):
            await db.update_account(acc["account_id"], {"last_error": "missing session", "status": "dead"})
            summary["dead"] = summary.get("dead", 0) + 1
            return
        new, err = "active", None
        try:
            c = tg_from_string(sec["telethon_string"], acc.get("api_id"))
            async with opened(c, timeout=25):
                if not await c.is_user_authorized():
                    new, err = "dead", "session unauthorized"
                else:
                    await c.get_me()
        except Exception as e:  # noqa: BLE001
            new = classify_error(e) or "dead"
            err = f"{type(e).__name__}: {e}"
        await db.update_account(acc["account_id"], {"status": new, "last_check": utcnow(), "last_error": err})
        summary[new] = summary.get(new, 0) + 1
        if new == prev:
            return
        if new == "dead" and not await db.get_setting("alerts_logout", True):
            return
        if new in {"banned", "frozen"} and not await db.get_setting("alerts_ban", True):
            return
        label = {"dead": "🚪 Session dropped", "banned": "🚫 Banned / deactivated",
                 "frozen": "❄️ Frozen", "active": "✅ Back online"}.get(new, new)
        text = (f"⚠️ <b>{label}</b>\n\nID: <code>{h(acc['account_id'])}</code>\n"
                f"User: <code>{acc.get('user_id')}</code> · {h(acc.get('first_name'))}\n"
                f"Phone: <code>{h(mask_phone(acc.get('phone')))}</code>\n"
                f"<code>{h(prev)}</code> → <code>{h(new)}</code>")
        if err:
            text += f"\nError: <code>{h(err)[:200]}</code>"
        await db.log_event("status_change", f"{acc['account_id']} {prev}->{new}", acc["account_id"])
        await broadcast(self.bot, text)


# ═══════════════════════════════════════════════════════════════════
# Bot + handlers
# ═══════════════════════════════════════════════════════════════════

IN = filters.private
app = Client(
    name="bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(WORKDIR),
    parse_mode=ParseMode.HTML,
    no_updates=True,
    in_memory=True,
)
monitor: Monitor | None = None


@app.on_message(IN & filters.command(["id"]))
async def cmd_id(_, m: Message):
    uid = m.from_user.id if m.from_user else m.chat.id
    await m.reply_text(f"Your Telegram user id: <code>{uid}</code>")


@app.on_message(IN & filters.command(["start", "menu"]))
async def cmd_start(_, m: Message):
    uid = m.from_user.id if m.from_user else m.chat.id
    log.info("/start from %s", uid)
    try:
        if not await db.is_admin(uid):
            await m.reply_text(f"⛔️ This bot is private.\nYour user id is <code>{uid}</code>.")
            return
        fsm.clear(uid)
        await m.reply_text(await start_text(BOT_USERNAME), reply_markup=main_menu(await db.is_owner(uid)))
    except Exception as e:  # noqa: BLE001
        log.exception("cmd_start")
        try:
            await m.reply_text(f"❌ Dashboard error: <code>{h(e)}</code>")
        except Exception:
            await botapi_send(uid, f"❌ Dashboard error: <code>{h(e)}</code>")


@app.on_message(IN & filters.command(["help"]))
async def cmd_help(_, m: Message):
    if await ensure_admin(m):
        await m.reply_text(help_text(), reply_markup=back_main())


@app.on_message(IN & filters.command(["cancel"]))
async def cmd_cancel(_, m: Message):
    if not await ensure_admin(m):
        return
    uid = m.from_user.id
    fsm.clear(uid)
    await m.reply_text("Cancelled.", reply_markup=main_menu(await db.is_owner(uid)))


@app.on_callback_query(filters.regex(r"^m:(main|help|x|cancel|dash)$"))
async def cb_root(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    fsm.clear(cq.from_user.id)
    act = cq.data.split(":")[1]
    if act == "help":
        await cq.message.edit_text(help_text(), reply_markup=back_main())
    elif act == "x":
        await cq.message.delete()
    else:
        text = await start_text(BOT_USERNAME)
        try:
            await cq.message.edit_text(text, reply_markup=main_menu(await db.is_owner(cq.from_user.id)))
        except Exception:
            pass
    await cq.answer("Updated" if act == "dash" else "")


# ── accounts ────────────────────────────────────────────────────────

async def render_list(cq: CallbackQuery, page: int):
    total = await db.count_accounts()
    page = max(0, page)
    accs = await db.list_accounts(skip=page * PER_PAGE, limit=PER_PAGE)
    text = ("📱 <b>Accounts</b>\n\nWorkspace is empty. Use <b>Add Account</b>."
            if total == 0 else f"📱 <b>Accounts</b> — {total} stored · page {page+1}")
    await cq.message.edit_text(text, reply_markup=accounts_kb(accs, page, total, PER_PAGE))


@app.on_callback_query(filters.regex(r"^a:l:(\d+)$"))
async def cb_list(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        await render_list(cq, int(cq.data.split(":")[2]))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^a:v:([0-9a-f]+)$"))
async def cb_view(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    acc = await db.get_account(cq.data.split(":")[2])
    if not acc:
        return await cq.answer("Gone.", show_alert=True)
    await cq.message.edit_text(account_card(acc), reply_markup=card_kb(acc["account_id"]))
    await cq.answer()


@app.on_callback_query(filters.regex(r"^a:dna:([0-9a-f]+)$"))
async def cb_dna_one(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    acc = await db.get_account(cq.data.split(":")[2])
    if not acc:
        return await cq.answer("Gone.", show_alert=True)
    await cq.answer("Asking @TGDNAbot…")
    try:
        parsed = await dna_refresh_one(acc)
        acc = await db.get_account(acc["account_id"])
        await cq.message.edit_text(account_card(acc), reply_markup=card_kb(acc["account_id"]))
        await cq.message.reply_text(f"📅 @TGDNAbot · <code>{h(parsed.get('reg_date'))}</code>")
    except Exception as e:
        await cq.message.reply_text(f"❌ Date refresh failed: <code>{h(e)}</code>")


@app.on_callback_query(filters.regex(r"^a:dnaA$"))
async def cb_dna_all(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    accs = await db.all_accounts()
    if not accs:
        return await cq.answer("No accounts.", show_alert=True)
    await cq.answer("Refreshing dates…")
    status = await cq.message.reply_text(f"📅 0/{len(accs)} via @TGDNAbot")

    async def prog(i, st):
        try:
            await status.edit_text(f"📅 {i}/{st['total']} · ok {st['ok']} · fail {st['fail']}")
        except Exception:
            pass

    stats = await dna_refresh_all(prog)
    await status.edit_text(f"📅 Done. Updated <b>{stats['ok']}</b> · failed {stats['fail']}")
    await render_list(cq, 0)


@app.on_callback_query(filters.regex(r"^a:p:([0-9a-f]+)$"))
async def cb_ping(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid = cq.data.split(":")[2]
    acc = await db.get_account(aid)
    if not acc:
        return await cq.answer("Missing", show_alert=True)
    await cq.answer("Pinging…")
    async with locks[aid]:
        try:
            _, c = await client_for(aid)
            async with opened(c):
                me = await c.get_me()
            await db.update_account(aid, {"status": "active", "last_check": utcnow(), "last_error": None,
                                          "username": me.username, "first_name": me.first_name, "phone": me.phone})
            await cq.message.reply_text(f"✅ Alive: <b>{h(me.first_name)}</b> (<code>{me.id}</code>)")
        except Exception as e:  # noqa: BLE001
            st = classify_error(e) or "dead"
            await db.update_account(aid, {"status": st, "last_check": utcnow(), "last_error": str(e)})
            await cq.message.reply_text(f"❌ Ping failed ({h(st)}): <code>{h(e)}</code>")


@app.on_callback_query(filters.regex(r"^a:d1:([0-9a-f]+)$"))
async def cb_del_ask(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        aid = cq.data.split(":")[2]
        await cq.message.edit_text("🗑 Remove this account from the workspace? (does not log out of Telegram)",
                                   reply_markup=confirm_kb(f"a:dx:{aid}", f"a:v:{aid}"))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^a:dx:([0-9a-f]+)$"))
async def cb_del_do(c: Client, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid = cq.data.split(":")[2]
    acc = await db.get_account(aid)
    ok = await db.delete_account(aid)
    await cq.answer("Removed" if ok else "Gone")
    await render_list(cq, 0)
    if ok and acc:
        await db.inc("accounts_deleted", 1)
        await broadcast(c, f"🔄 {h(cq.from_user.first_name)} removed <code>{aid}</code>", exclude=cq.from_user.id)


@app.on_callback_query(filters.regex(r"^a:wipe1$"))
async def cb_wipe_ask(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        fsm.set(cq.from_user.id, "confirm_wipe")
        await cq.message.edit_text("☢️ Remove ALL stored accounts? Tap Confirm or type <code>WIPE ALL</code>.",
                                   reply_markup=confirm_kb("a:wipeX", "d:m"))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^a:wipeX$"))
async def cb_wipe_do(c: Client, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    fsm.clear(cq.from_user.id)
    async with global_lock:
        n = await db.delete_all()
    if n:
        await db.inc("accounts_deleted", n)
    await cq.message.edit_text(f"🗑 Removed <b>{n}</b> account(s).",
                               reply_markup=db_kb(await db.is_owner(cq.from_user.id)))
    await cq.answer("Wiped")
    await broadcast(c, f"🔄 {h(cq.from_user.first_name)} wiped the store ({n}).", exclude=cq.from_user.id)


# ── add ─────────────────────────────────────────────────────────────

PICK_TITLES = {
    "hex": "Pick an account to export HEX + DC",
    "str": "Pick an account to export string key",
    "file": "Pick an account to export .session",
    "tdata": "Pick an account to export TData",
    "rm": "Pick an account to remove",
    "spam": "Pick an account to spam-check",
    "otp": "Pick an account to read OTP",
    "cln": "Pick an account for cleanup",
}


async def render_pick(cq: CallbackQuery, mode: str, page: int) -> None:
    total = await db.count_accounts()
    page = max(0, page)
    accs = await db.list_accounts(skip=page * PER_PAGE, limit=PER_PAGE)
    rows = []
    for acc in accs:
        flag = STATUS_EMOJI.get(acc.get("status"), "❓")
        rows.append([B(acc_label(acc), f"pg:{mode}:{acc['account_id']}")])
    nav = []
    if page > 0:
        nav.append(B("⬅️", f"pk:{mode}:{page-1}"))
    if (page + 1) * PER_PAGE < total:
        nav.append(B("➡️", f"pk:{mode}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([B("Dashboard 🏠", "m:main")])
    text = f"{PICK_TITLES.get(mode, 'Pick an account')}\n{total} stored"
    if total == 0:
        text = "No accounts stored yet. Use Add Account+ first."
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))


@app.on_callback_query(filters.regex(r"^h:(td|sf|hx|sk|rm)$"))
async def cb_hub(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    kind = cq.data.split(":")[1]
    menus = {
        "td": ("TData 📦", [[B("TData → login 📥", "n:td"), B("Account → TData 📤", "pk:tdata:0")]]),
        "sf": (".session file 📁", [[B("File → login 📥", "n:sf"), B("Account → .session 📤", "pk:file:0")]]),
        "hx": ("HEX + DC 🔑", [[B("Hex → login 📥", "n:hx"), B("Account → Hex 📤", "pk:hex:0")]]),
        "sk": ("String key convert 🧬", [[B("String → login 📥", "n:st"), B("Account → string 📤", "pk:str:0")]]),
        "rm": ("Remove accounts 🗑", [[B("Remove one 🗑", "pk:rm:0"), B("Remove all ☢️", "a:wipe1")]]),
    }
    title, rows = menus[kind]
    await cq.message.edit_text(title, reply_markup=hub_kb(rows))
    await cq.answer()


@app.on_callback_query(filters.regex(r"^pk:(hex|str|file|tdata|rm|spam|otp|cln):(\d+)$"))
async def cb_pick(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        parts = cq.data.split(":")
        await render_pick(cq, parts[1], int(parts[2]))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^pg:(hex|str|file|tdata|rm|spam|otp|cln):([0-9a-f]+)$"))
async def cb_pick_go(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    mode, aid = cq.data.split(":")[1], cq.data.split(":")[2]
    acc = await db.get_account(aid)
    if not acc:
        return await cq.answer("Gone.", show_alert=True)
    if mode == "hex":
        cq.data = f"c:ex:h:{aid}"
        return await cb_export(_, cq)
    if mode == "str":
        cq.data = f"c:ex:s:{aid}"
        return await cb_export(_, cq)
    if mode == "file":
        cq.data = f"c:ex:f:{aid}"
        return await cb_export(_, cq)
    if mode == "tdata":
        await cq.answer("Building TData…")
        try:
            zpath = await account_to_tdata_zip(acc)
            await cq.message.reply_document(str(zpath), caption=f"TData zip for {acc.get('first_name') or aid}")
            zpath.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            await cq.message.reply_text(f"❌ {h(e)}")
        return
    if mode == "rm":
        cq.data = f"a:d1:{aid}"
        return await cb_del_ask(_, cq)
    if mode == "spam":
        cq.data = f"p:1:{aid}"
        return await cb_spam1(_, cq)
    if mode == "otp":
        cq.data = f"o:1:{aid}"
        return await cb_otp1(_, cq)
    if mode == "cln":
        await cq.message.edit_text(
            f"✨ Cleanup · {h(acc.get('first_name') or aid)}",
            reply_markup=InlineKeyboardMarkup([
                [B("Clear DMs 🧹", f"k:d1:{aid}"), B("Nuclear ☢️", f"k:n1:{aid}")],
                [B("Dashboard 🏠", "m:main")],
            ]),
        )
        await cq.answer()


@app.on_callback_query(filters.regex(r"^n:m$"))
async def cb_add(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        fsm.clear(cq.from_user.id)
        await cq.message.edit_text("➕ <b>Add account</b> — only ingest sessions you own.", reply_markup=add_menu())
        await cq.answer()


@app.on_callback_query(filters.regex(r"^n:(ph|hx|st|sf|tx|td)$"))
async def cb_add_kind(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    kind = cq.data.split(":")[1]
    prompts = {
        "ph": ("add_phone", "📞 Send the phone in international format, e.g. <code>+919876543210</code>"),
        "hx": ("add_hex", "🔑 Send the 512-char auth_key hex. You may include DC: <code>2:aabb…</code>"),
        "st": ("add_string", "📄 Paste a Telethon or Pyrogram string session."),
        "sf": ("wait_session", "📁 Upload a <code>.session</code> SQLite file."),
        "tx": ("wait_txt", "📚 Upload a <code>.txt</code> — one string or <code>hex|dc</code> per line."),
        "td": ("wait_tdata", "📦 Upload a Telegram Desktop tdata zip (must contain <code>key_datas</code>)."),
    }
    state, text = prompts[kind]
    fsm.set(cq.from_user.id, state)
    await cq.message.edit_text(text, reply_markup=cancel_kb())
    await cq.answer()


# ── converters / export ─────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^c:m$"))
async def cb_conv(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        fsm.clear(cq.from_user.id)
        await cq.message.edit_text("🔄 <b>Converters</b>\n.session ↔ string ↔ hex+DC · TData → login.",
                                   reply_markup=conv_menu())
        await cq.answer()


@app.on_callback_query(filters.regex(r"^c:(paste|file|td)$"))
async def cb_conv_kind(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    kind = cq.data.split(":")[1]
    if kind == "paste":
        fsm.set(cq.from_user.id, "conv_paste")
        await cq.message.edit_text("Paste a Telethon string, Pyrogram string, or <code>hex|dc</code>.",
                                   reply_markup=cancel_kb())
    elif kind == "file":
        fsm.set(cq.from_user.id, "conv_file")
        await cq.message.edit_text("Upload a <code>.session</code> file.", reply_markup=cancel_kb())
    else:
        fsm.set(cq.from_user.id, "wait_tdata", convert_only=True)
        await cq.message.edit_text("Upload a tdata zip (convert only, not stored).", reply_markup=cancel_kb())
    await cq.answer()


@app.on_callback_query(filters.regex(r"^c:ex:([shf]):([0-9a-f]+)$"))
async def cb_export(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    kind, aid = cq.data.split(":")[2], cq.data.split(":")[3]
    acc = await db.get_account(aid)
    if not acc:
        return await cq.answer("Missing", show_alert=True)
    sec = db.secrets(acc)
    await cq.answer()
    warn = "⚠️ Treat this as a password."
    if kind == "s":
        await cq.message.reply_text(f"{warn}\n\n<b>Telethon</b>\n<code>{h(sec.get('telethon_string'))}</code>\n\n"
                                    f"<b>Pyrogram</b>\n<code>{h(sec.get('pyrogram_string'))}</code>")
    elif kind == "h":
        await cq.message.reply_text(f"{warn}\n\nDC <code>{acc.get('dc_id')}</code>\n<code>{h(sec.get('auth_key_hex'))}</code>")
    else:
        hx = sec.get("auth_key_hex")
        if not hx:
            return await cq.message.reply_text("No auth_key.")
        parts = SessionParts(dc_id=int(acc.get("dc_id") or 2), auth_key=bytes.fromhex(hx),
                             user_id=acc.get("user_id"), api_id=acc.get("api_id") or API_ID)
        pyro, tele = TMP_DIR / f"{aid}_pyro.session", TMP_DIR / f"{aid}_tele.session"
        write_pyro_file(pyro, parts)
        write_tele_file(tele, parts)
        try:
            await cq.message.reply_document(str(pyro), caption=f"{warn}\nPyrogram .session")
            await cq.message.reply_document(str(tele), caption="Telethon .session")
        finally:
            pyro.unlink(missing_ok=True)
            tele.unlink(missing_ok=True)


# ── security ────────────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^s:m$"))
async def cb_sec(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    accs = await db.list_accounts(limit=12)
    rows = [[B("Open an account card for 2FA / devices / kill", "s:m")]]
    for a in accs:
        rows.append([B(f"🛡 {a.get('first_name') or a['account_id']}", f"a:v:{a['account_id']}")])
    rows.append([B("⬅️ Main menu", "m:main")])
    await cq.message.edit_text(f"🛡 <b>Security Center</b>\n\n<i>{SRP_NOTE}</i>", reply_markup=InlineKeyboardMarkup(rows))
    await cq.answer()


@app.on_callback_query(filters.regex(r"^s:2:([0-9a-f]+)$"))
async def cb_2fa(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid = cq.data.split(":")[2]
    try:
        _, c = await client_for(aid)
        async with opened(c):
            st = await twofa_status(c)
    except Exception as e:  # noqa: BLE001
        return await cq.answer(str(e)[:180], show_alert=True)
    flag = "enabled" if st["has_password"] else "disabled"
    await db.update_account(aid, {"has_2fa": bool(st["has_password"]), "twofa_hint": st.get("hint") or ""})
    await cq.message.edit_text(
        f"🛡 <b>2FA</b> <code>{aid}</code>\nCloud password: <b>{flag}</b>\n"
        f"Hint: <code>{h(st.get('hint') or '—')}</code>\nRecovery: {'yes' if st.get('has_recovery') else 'no'}\n\n"
        f"<i>{SRP_NOTE}</i>", reply_markup=twofa_kb(aid))
    await cq.answer()


@app.on_callback_query(filters.regex(r"^s:2e:([0-9a-f]+)$"))
async def cb_2e(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        fsm.set(cq.from_user.id, "2fa_en_new", account_id=cq.data.split(":")[2])
        await cq.message.reply_text("Send the <b>new</b> cloud password.", reply_markup=cancel_kb())
        await cq.answer()


@app.on_callback_query(filters.regex(r"^s:2d:([0-9a-f]+)$"))
async def cb_2d(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        fsm.set(cq.from_user.id, "2fa_dis", account_id=cq.data.split(":")[2])
        await cq.message.reply_text("Send the <b>current</b> cloud password to disable 2FA.", reply_markup=cancel_kb())
        await cq.answer()


@app.on_callback_query(filters.regex(r"^s:2c:([0-9a-f]+)$"))
async def cb_2c(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        fsm.set(cq.from_user.id, "2fa_ch_cur", account_id=cq.data.split(":")[2])
        await cq.message.reply_text("Send the <b>current</b> cloud password.", reply_markup=cancel_kb())
        await cq.answer()


@app.on_callback_query(filters.regex(r"^s:k1:([0-9a-f]+)$"))
async def cb_kill_ask(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        aid = cq.data.split(":")[2]
        await cq.message.edit_text("☠️ Kill Session invalidates the stored token. Confirm?",
                                   reply_markup=confirm_kb(f"s:kx:{aid}", f"a:v:{aid}"))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^s:kx:([0-9a-f]+)$"))
async def cb_kill_do(c: Client, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid = cq.data.split(":")[2]
    await cq.answer("Logging out…")
    async with locks[aid]:
        try:
            _, tg = await client_for(aid)
            async with opened(tg):
                await kill_session(tg)
            await db.update_account(aid, {"status": "dead", "last_error": "killed by admin", "last_check": utcnow()})
            await cq.message.edit_text("☠️ Session killed.", reply_markup=card_kb(aid))
            await broadcast(c, f"🔄 {h(cq.from_user.first_name)} killed <code>{aid}</code>", exclude=cq.from_user.id)
        except Exception as e:  # noqa: BLE001
            await db.update_account(aid, {"status": classify_error(e) or "dead", "last_error": str(e), "last_check": utcnow()})
            await cq.message.edit_text(f"Kill result: <code>{h(e)}</code>", reply_markup=card_kb(aid))


@app.on_callback_query(filters.regex(r"^s:t1:([0-9a-f]+)$"))
async def cb_term_ask(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        aid = cq.data.split(":")[2]
        await cq.message.edit_text(
            "🌍 <b>Terminate Others</b> drops every secondary device.\nTelegram refuses this if the session is &lt; 24h old.",
            reply_markup=confirm_kb(f"s:tx:{aid}", f"a:v:{aid}"))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^s:tx:([0-9a-f]+)$"))
async def cb_term_do(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid = cq.data.split(":")[2]
    await cq.answer("Resetting…")
    async with locks[aid]:
        try:
            _, tg = await client_for(aid)
            async with opened(tg):
                result = await terminate_others(tg)
        except Exception as e:  # noqa: BLE001
            return await cq.message.edit_text(f"❌ <code>{h(e)}</code>", reply_markup=card_kb(aid))
    if result["ok"]:
        text = "✅ All other devices logged out. This session stayed alive."
    elif result.get("error") == "fresh_reset":
        text = f"⏳ {result['hint']}"
    else:
        text = f"❌ {h(result.get('hint') or result.get('error'))}"
    await cq.message.edit_text(text, reply_markup=card_kb(aid))


@app.on_callback_query(filters.regex(r"^s:dv:([0-9a-f]+)$"))
async def cb_devices(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid = cq.data.split(":")[2]
    try:
        _, tg = await client_for(aid)
        async with opened(tg):
            devices = await list_devices(tg)
    except Exception as e:  # noqa: BLE001
        return await cq.answer(str(e)[:180], show_alert=True)
    fsm.set(cq.from_user.id, "devices", account_id=aid, hashes=[d["hash"] for d in devices])
    body = "📱 <b>Active devices</b>\n\n" + "\n\n".join(render_device(d) for d in devices)
    chunks = split_html(body)
    await cq.message.edit_text(chunks[0], reply_markup=devices_kb(aid, devices))
    for extra in chunks[1:]:
        await cq.message.reply_text(extra)
    await cq.answer()


@app.on_callback_query(filters.regex(r"^s:dl:([0-9a-f]+):(\d+)$"))
async def cb_dev_out(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid, idx = cq.data.split(":")[2], int(cq.data.split(":")[3])
    st = fsm.get(cq.from_user.id)
    hashes = (st.data if st else {}).get("hashes") or []
    if idx >= len(hashes):
        return await cq.answer("Refresh the list.", show_alert=True)
    try:
        _, tg = await client_for(aid)
        async with opened(tg):
            if hashes[idx]:
                await tg(ResetAuthorizationRequest(hash=int(hashes[idx])))
            devices = await list_devices(tg)
    except Exception as e:  # noqa: BLE001
        return await cq.answer(str(e)[:180], show_alert=True)
    fsm.set(cq.from_user.id, "devices", account_id=aid, hashes=[d["hash"] for d in devices])
    body = "✅ Device logged out.\n\n📱 <b>Active devices</b>\n\n" + "\n\n".join(render_device(d) for d in devices)
    await cq.message.edit_text(body, reply_markup=devices_kb(aid, devices))
    await cq.answer()


# ── cleanup ─────────────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^k:m$"))
async def cb_cln(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        await cq.message.edit_text(
            "✨ Cleanup suite\nClear DMs or nuclear reset on a stored account.",
            reply_markup=hub_kb([[B("Pick account ✨", "pk:cln:0")]]),
        )
        await cq.answer()


@app.on_callback_query(filters.regex(r"^k:d1:([0-9a-f]+)$"))
async def cb_dm_ask(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        aid = cq.data.split(":")[2]
        await cq.message.edit_text("🧹 Delete all private DMs? Irreversible.",
                                   reply_markup=confirm_kb(f"k:dx:{aid}", f"a:v:{aid}"))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^k:dx:([0-9a-f]+)$"))
async def cb_dm_do(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid = cq.data.split(":")[2]
    await cq.answer("Clearing…")
    status = await cq.message.edit_text("🧹 Working…")

    async def prog(d, e):
        try:
            await status.edit_text(f"🧹 Deleted {d} · errors {e}")
        except Exception:
            pass

    async with locks[aid]:
        try:
            _, tg = await client_for(aid)
            async with opened(tg, timeout=120):
                r = await clear_dms(tg, prog)
        except Exception as e:  # noqa: BLE001
            return await status.edit_text(f"❌ {h(e)}", reply_markup=card_kb(aid))
    await status.edit_text(f"✅ DMs cleared. Deleted <b>{r['deleted']}</b> · skipped {r['skipped']} · err {r['errors']}",
                           reply_markup=card_kb(aid))


@app.on_callback_query(filters.regex(r"^k:n1:([0-9a-f]+)$"))
async def cb_nu_ask(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        aid = cq.data.split(":")[2]
        await cq.message.edit_text("☢️ Nuclear cleanup — leave groups, wipe bots + DMs. Confirm?",
                                   reply_markup=confirm_kb(f"k:nx:{aid}", f"a:v:{aid}"))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^k:nx:([0-9a-f]+)$"))
async def cb_nu_do(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    aid = cq.data.split(":")[2]
    await cq.answer("Started")
    status = await cq.message.edit_text("☢️ Running…")

    async def prog(s):
        try:
            await status.edit_text(f"☢️ g {s['left_groups']} · b {s['cleared_bots']} · dm {s['cleared_dms']} · e {s['errors']}")
        except Exception:
            pass

    async with locks[aid]:
        try:
            _, tg = await client_for(aid)
            async with opened(tg, timeout=180):
                r = await nuclear(tg, prog)
        except Exception as e:  # noqa: BLE001
            return await status.edit_text(f"❌ {h(e)}", reply_markup=card_kb(aid))
    await status.edit_text(
        f"✅ Nuclear done.\nGroups: <b>{r['left_groups']}</b>\nBots: <b>{r['cleared_bots']}</b>\n"
        f"DMs: <b>{r['cleared_dms']}</b>\nErrors: {r['errors']}", reply_markup=card_kb(aid))


# ── spam / otp ──────────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^p:m$"))
async def cb_spam(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        await cq.message.edit_text(
            "🛡 Spam Checker",
            reply_markup=hub_kb([
                [B("All accounts 🌐", "p:all"), B("Each account 👤", "pk:spam:0")],
            ]),
        )
        await cq.answer()


async def spam_one(acc):
    async with locks[acc["account_id"]]:
        _, c = await client_for(acc["account_id"])
        async with opened(c):
            r = await check_spam(c)
    await db.update_account(acc["account_id"], {"spam_status": r["flag"], "spam_checked_at": r["checked_at"], "last_check": utcnow()})
    return r


@app.on_callback_query(filters.regex(r"^p:1:([0-9a-f]+)$"))
async def cb_spam1(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    acc = await db.get_account(cq.data.split(":")[2])
    if not acc:
        return await cq.answer("Missing", show_alert=True)
    await cq.answer("Asking @SpamBot…")
    try:
        r = await spam_one(acc)
        await cq.message.reply_text(f"📊 <code>{h(r['flag'])}</code>\n\n{h(r['text'][:1500])}", reply_markup=card_kb(acc["account_id"]))
    except Exception as e:  # noqa: BLE001
        await cq.message.reply_text(f"❌ <code>{h(e)}</code>")


@app.on_callback_query(filters.regex(r"^p:all$"))
async def cb_spam_all(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    accs = await db.all_accounts()
    if not accs:
        return await cq.answer("No accounts.", show_alert=True)
    await cq.answer("Queue started")
    status = await cq.message.edit_text(f"📊 0/{len(accs)}")
    rows, sem = [], asyncio.Semaphore(2)

    async def one(acc):
        async with sem:
            try:
                r = await spam_one(acc)
                rows.append((acc, r["flag"], r["text"], None))
            except Exception as e:  # noqa: BLE001
                rows.append((acc, "error", "", str(e)))
            try:
                await status.edit_text(f"📊 {len(rows)}/{len(accs)}")
            except Exception:
                pass
            await asyncio.sleep(0.7)

    await asyncio.gather(*(one(a) for a in accs))
    clean = sum(1 for _, f, __, ___ in rows if f == "clean")
    limited = sum(1 for _, f, __, ___ in rows if f == "limited")
    errors = sum(1 for _, f, __, ___ in rows if f == "error")
    lines = [f"SPAM CHECK  {iso(utcnow())}", f"total={len(rows)} clean={clean} limited={limited} errors={errors}", ""]
    for acc, flag, text, err in rows:
        lines.append(f"[{flag:8}] {acc['account_id']}  {mask_phone(acc.get('phone'))}  {(err or text or '')[:160].replace(chr(10),' ')}")
    report = TMP_DIR / f"spam_{cq.from_user.id}.txt"
    report.write_text("\n".join(lines), encoding="utf-8")
    await status.edit_text(f"📊 Done. Clean <b>{clean}</b> · Limited <b>{limited}</b> · Errors <b>{errors}</b>",
                           reply_markup=InlineKeyboardMarkup([[B("⬅️ Spam", "p:m")]]))
    await cq.message.reply_document(str(report), caption="Spam report")
    report.unlink(missing_ok=True)


@app.on_callback_query(filters.regex(r"^o:m$"))
async def cb_otp(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        await cq.message.edit_text(
            "🔐 Get Otp",
            reply_markup=hub_kb([
                [B("All accounts 🌐", "o:all"), B("Each account 👤", "pk:otp:0")],
            ]),
        )
        await cq.answer()


async def otp_one(acc):
    async with locks[acc["account_id"]]:
        _, c = await client_for(acc["account_id"])
        async with opened(c):
            inbox = await fetch_service(c, 8)
    latest = next((i for i in inbox if i.get("otp")), inbox[0] if inbox else {"otp": None, "text": "", "date": None})
    found = sum(1 for i in inbox if i.get("otp"))
    if found:
        await db.inc("otp_read", found)
    return latest, inbox


@app.on_callback_query(filters.regex(r"^o:1:([0-9a-f]+)$"))
async def cb_otp1(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    acc = await db.get_account(cq.data.split(":")[2])
    if not acc:
        return await cq.answer("Missing", show_alert=True)
    await cq.answer("Fetching 777000…")
    try:
        latest, inbox = await otp_one(acc)
    except Exception as e:  # noqa: BLE001
        return await cq.message.reply_text(f"❌ <code>{h(e)}</code>")
    chunks = []
    for item in inbox:
        otp = f" · code <code>{h(item['otp'])}</code>" if item.get("otp") else ""
        chunks.append(f"• {h(iso(item.get('date')))}{otp}\n<code>{h((item.get('text') or '')[:280])}</code>")
    body = (f"📩 <b>{h(acc.get('first_name') or acc['account_id'])}</b> · {h(mask_phone(acc.get('phone')))}\n"
            f"Latest code: <code>{h(latest.get('otp') or 'not found')}</code>\n\n" +
            ("\n\n".join(chunks) if chunks else "<i>No service messages.</i>"))
    await cq.message.reply_text(body, reply_markup=card_kb(acc["account_id"]))


@app.on_callback_query(filters.regex(r"^o:all$"))
async def cb_otp_all(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    accs = await db.all_accounts()
    if not accs:
        return await cq.answer("No accounts.", show_alert=True)
    await cq.answer("Fetching…")
    status = await cq.message.edit_text(f"📩 0/{len(accs)}")
    lines = ["📩 <b>OTP sweep</b>\n"]
    for i, acc in enumerate(accs, 1):
        try:
            latest, _ = await otp_one(acc)
            lines.append(f"• <code>{acc['account_id']}</code> {h(mask_phone(acc.get('phone')))} → <code>{h(latest.get('otp') or '—')}</code>")
        except Exception as e:  # noqa: BLE001
            lines.append(f"• <code>{acc['account_id']}</code> ❌ {h(e)[:80]}")
        if i % 2 == 0 or i == len(accs):
            try:
                await status.edit_text(f"📩 {i}/{len(accs)}")
            except Exception:
                pass
        await asyncio.sleep(0.2)
    chunks = split_html("\n".join(lines))
    await status.edit_text(chunks[0], reply_markup=InlineKeyboardMarkup([[B("⬅️ OTP", "o:m")]]))
    for extra in chunks[1:]:
        await cq.message.reply_text(extra)


# ── alerts / db / admins ────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^l:m$"))
async def cb_alerts(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        await cq.message.edit_text("🔔 <b>Alert Settings</b> — background monitor pushes logout / ban notices.",
                                   reply_markup=alerts_kb(await db.all_settings()))
        await cq.answer()


@app.on_callback_query(filters.regex(r"^l:tg:(alerts_logout|alerts_ban|monitor_enabled)$"))
async def cb_tog(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        key = cq.data.split(":")[2]
        nxt = await db.toggle(key)
        await cq.answer(f"{key} → {'ON' if nxt else 'OFF'}")
        await cq.message.edit_text("🔔 <b>Alert Settings</b>", reply_markup=alerts_kb(await db.all_settings()))


@app.on_callback_query(filters.regex(r"^l:iv$"))
async def cb_iv(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        fsm.set(cq.from_user.id, "alert_interval")
        await cq.message.reply_text("Send interval in seconds (60–86400).", reply_markup=cancel_kb())
        await cq.answer()


@app.on_callback_query(filters.regex(r"^l:now$"))
async def cb_now(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    await cq.answer("Running…")
    if monitor is None:
        return await cq.message.reply_text("Monitor not attached.")
    r = await monitor.run_once()
    await cq.message.reply_text(f"💓 Pass complete.\n<code>{h(r)}</code>")


@app.on_callback_query(filters.regex(r"^d:m$"))
async def cb_db(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    n = await db.count_accounts()
    adm = await db.get_admins()
    s = await db.all_settings()
    await cq.message.edit_text(
        f"💾 <b>Database</b>\n\nAccounts: <b>{n}</b>\nAdmins: <b>{len(adm)}</b>\n"
        f"Monitor: <code>{s.get('monitor_enabled')}</code> every {s.get('check_interval')}s\n\n"
        "Backups stay Fernet-encrypted. Same SESSION_ENC_KEY required to restore.",
        reply_markup=db_kb(await db.is_owner(cq.from_user.id)))
    await cq.answer()


@app.on_callback_query(filters.regex(r"^d:dl$"))
async def cb_dl(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    await cq.answer("Building…")
    payload = await db.export_workspace()
    path = BACKUP_DIR / f"workspace_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    await cq.message.reply_document(str(path), caption="Encrypted workspace backup.")


@app.on_callback_query(filters.regex(r"^e:logs$"))
async def cb_logs(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    evs = await db.recent_events(20)
    if not evs:
        body = "📋 <b>Logs</b>\n\nNo events yet. Adds, deletes, OTP reads and health changes will show up here."
    else:
        lines = ["📋 <b>Recent workspace logs</b>\n"]
        for e in evs:
            lines.append(f"• {h(iso(e.get('created_at')))} · <code>{h(e.get('kind'))}</code>\n  {h(e.get('message'))}")
        body = "\n".join(lines)
    chunks = split_html(body)
    await cq.message.edit_text(
        chunks[0],
        reply_markup=InlineKeyboardMarkup([[B("🔄 Refresh", "e:logs"), B("⬅️ Dashboard", "m:main")]]),
    )
    for extra in chunks[1:]:
        await cq.message.reply_text(extra)
    await cq.answer()


@app.on_callback_query(filters.regex(r"^d:up$"))
async def cb_up(_, cq: CallbackQuery):
    if await ensure_admin(cq):
        fsm.set(cq.from_user.id, "wait_backup")
        await cq.message.edit_text("⬆️ Send a workspace_*.json produced by this bot.", reply_markup=cancel_kb())
        await cq.answer()


@app.on_callback_query(filters.regex(r"^u:m$"))
async def cb_adm(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    lines = ["👑 <b>Admins</b> — one shared workspace.\n"]
    for a in await db.get_admins():
        icon = "⭐" if a.get("role") == "owner" else "👤"
        lines.append(f"{icon} <code>{a['user_id']}</code> · {h(a.get('role'))} · {h(iso(a.get('added_at')))}")
    await cq.message.edit_text("\n".join(lines), reply_markup=admin_kb(await db.is_owner(cq.from_user.id)))
    await cq.answer()


@app.on_callback_query(filters.regex(r"^u:add$"))
async def cb_adm_add(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    if not await db.is_owner(cq.from_user.id):
        return await cq.answer("Owner only.", show_alert=True)
    fsm.set(cq.from_user.id, "admin_add")
    await cq.message.reply_text("Send the numeric Telegram user id to promote.", reply_markup=cancel_kb())
    await cq.answer()


@app.on_callback_query(filters.regex(r"^u:del$"))
async def cb_adm_del(_, cq: CallbackQuery):
    if not await ensure_admin(cq):
        return
    if not await db.is_owner(cq.from_user.id):
        return await cq.answer("Owner only.", show_alert=True)
    fsm.set(cq.from_user.id, "admin_del")
    await cq.message.reply_text("Send the numeric user id to demote.", reply_markup=cancel_kb())
    await cq.answer()


# ── FSM text / files ────────────────────────────────────────────────

@app.on_message(IN & filters.text & ~filters.command(["start", "menu", "help", "cancel", "id"]))
async def on_text(client: Client, m: Message):
    if not await ensure_admin(m):
        return
    st = fsm.get(m.from_user.id)
    if st is None:
        return
    name, data = st.name, st.data
    txt = (m.text or "").strip()

    if name == "add_phone":
        if not PHONE_RE.match(norm_phone(txt)):
            return await m.reply_text("That does not look like a phone. Try <code>+91…</code>")
        phone = norm_phone(txt)
        tg = make_user_client()
        try:
            await tg.connect()
            sent = await tg.send_code_request(str(phone))
        except PhoneNumberInvalidError:
            try:
                await tg.disconnect()
            except Exception:
                pass
            return await m.reply_text("Telegram rejected that number.")
        except Exception as e:  # noqa: BLE001
            log.exception("send_code_request %s api_hash=%s", phone, type(getattr(tg, "api_hash", None)))
            try:
                await tg.disconnect()
            except Exception:
                pass
            return await m.reply_text(f"Could not send code: <code>{h(e)}</code>")
        fsm.live[m.from_user.id] = {"client": tg, "phone": phone, "hash": sent.phone_code_hash}
        fsm.set(m.from_user.id, "add_otp", phone=phone)
        return await m.reply_text(f"OTP sent to <code>{h(phone)}</code>. Send the digits.", reply_markup=cancel_kb())

    if name == "add_otp":
        live = fsm.live.get(m.from_user.id) or {}
        tg = live.get("client")
        if not tg:
            fsm.clear(m.from_user.id)
            return await m.reply_text("Wizard expired. Start again.")
        code = "".join(ch for ch in txt if ch.isdigit())
        try:
            await tg.sign_in(live["phone"], code, phone_code_hash=live.get("hash"))
        except SessionPasswordNeededError:
            fsm.set(m.from_user.id, "add_2fa")
            return await m.reply_text("2FA is on. Send the cloud password.", reply_markup=cancel_kb())
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            return await m.reply_text("Code invalid/expired. Try again or /cancel.")
        except Exception as e:  # noqa: BLE001
            return await m.reply_text(f"Sign-in failed: <code>{h(e)}</code>")
        return await _finish_login(client, m, tg, has_2fa=False)

    if name == "add_2fa":
        live = fsm.live.get(m.from_user.id) or {}
        tg = live.get("client")
        if not tg:
            fsm.clear(m.from_user.id)
            return await m.reply_text("Wizard expired.")
        try:
            await tg.sign_in(password=txt)
        except Exception as e:  # noqa: BLE001
            return await m.reply_text(f"2FA rejected: <code>{h(e)}</code>")
        return await _finish_login(client, m, tg, has_2fa=True)

    if name == "add_hex":
        hx, dc = extract_hex_dc(txt)
        if not is_hex_key(hx):
            return await m.reply_text("Need a 512-character hex auth_key.")
        if dc is None:
            fsm.set(m.from_user.id, "add_hex_dc", hex_key=hx)
            return await m.reply_text("Send the DC id (1–5).", reply_markup=cancel_kb())
        return await _import_text(client, m, f"{hx}|{dc}", "hex_dc")

    if name == "add_hex_dc":
        try:
            dc = int(txt)
        except ValueError:
            return await m.reply_text("DC must be 1–5.")
        return await _import_text(client, m, f"{data.get('hex_key')}|{dc}", "hex_dc")

    if name == "add_string":
        return await _import_text(client, m, txt, "string")

    if name == "conv_paste":
        try:
            parts = parse_any(txt)
        except ValueError as e:
            if str(e) == "HEX_NEEDS_DC":
                return await m.reply_text("Include the DC, e.g. <code>2:hex…</code>")
            return await m.reply_text(f"❌ {h(e)}")
        await m.reply_text(conv_card(parts))
        fsm.clear(m.from_user.id)
        return

    if name == "wait_tdata_pass":
        zp = Path(data.get("zip_path", ""))
        if not zp.is_file():
            fsm.clear(m.from_user.id)
            return await m.reply_text("Zip gone. Upload again.")
        return await _run_tdata(client, m, zp, txt, convert_only=bool(data.get("convert_only")))

    if name == "alert_interval":
        try:
            val = max(60, min(int(txt), 86400))
        except ValueError:
            return await m.reply_text("Send an integer.")
        await db.set_setting("check_interval", val)
        fsm.clear(m.from_user.id)
        return await m.reply_text(f"✅ Interval set to <b>{val}</b>s.")

    if name == "admin_add":
        if not await db.is_owner(m.from_user.id):
            fsm.clear(m.from_user.id)
            return
        try:
            uid = int(txt)
        except ValueError:
            return await m.reply_text("Need a numeric id.")
        added = await db.add_admin(uid, m.from_user.id)
        fsm.clear(m.from_user.id)
        await m.reply_text("✅ Admin added." if added else "Already an admin (or owner).")
        try:
            await botapi_send(uid, "You have been granted admin access. Send /start.")
        except Exception:
            await m.reply_text("Could not DM them — ask them to /start first.")
        return

    if name == "admin_del":
        if not await db.is_owner(m.from_user.id):
            fsm.clear(m.from_user.id)
            return
        try:
            uid = int(txt)
        except ValueError:
            return await m.reply_text("Need a numeric id.")
        ok = await db.remove_admin(uid)
        fsm.clear(m.from_user.id)
        return await m.reply_text("Removed." if ok else "Cannot remove (missing or owner).")

    if name == "confirm_wipe":
        if txt != "WIPE ALL":
            return await m.reply_text("Type <code>WIPE ALL</code> exactly, or /cancel.")
        async with global_lock:
            n = await db.delete_all()
        if n:
            await db.inc("accounts_deleted", n)
        fsm.clear(m.from_user.id)
        await m.reply_text(f"🗑 Removed <b>{n}</b> account(s).")
        await broadcast(client, f"🔄 {h(m.from_user.first_name)} wiped the store ({n}).", exclude=m.from_user.id)
        return

    if name in {"2fa_en_new", "2fa_en_hint", "2fa_dis", "2fa_ch_cur", "2fa_ch_new", "2fa_ch_hint"}:
        return await _2fa_flow(m, name, data, txt)

    await m.reply_text("Send the file I asked for, or /cancel.")


@app.on_message(IN & filters.document)
async def on_doc(client: Client, m: Message):
    if not await ensure_admin(m):
        return
    st = fsm.get(m.from_user.id)
    if st is None:
        return await m.reply_text("Open <b>Add Account</b> or <b>Converters</b> first, then send the file.")
    name = st.name
    if name == "wait_session":
        path = await download_doc(client, m, ".session")
        if not path:
            return
        wait = await m.reply_text("Reading session file…")
        try:
            info = await inspect_parts(parse_session_file(path))
            await persist(client, m, info, info.get("source") or "session_file")
            await wait.delete()
        except Exception as e:  # noqa: BLE001
            await db.inc("add_fail", 1)
            await wait.edit_text(f"❌ {h(e)}")
        finally:
            path.unlink(missing_ok=True)
            fsm.clear(m.from_user.id)
        return

    if name == "wait_txt":
        path = await download_doc(client, m, ".txt")
        if not path:
            return
        try:
            lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
        finally:
            path.unlink(missing_ok=True)
        if not lines:
            fsm.clear(m.from_user.id)
            return await m.reply_text("File was empty.")
        status = await m.reply_text(f"Bulk 0/{len(lines)}")
        ok = fail = 0
        errs = []
        for i, line in enumerate(lines, 1):
            try:
                info = await inspect_text(line)
                await persist(client, m, info, "bulk_txt", quiet=True)
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                await db.inc("add_fail", 1)
                errs.append(f"{i}: {e}")
            if i % 2 == 0 or i == len(lines):
                await status.edit_text(f"Bulk {i}/{len(lines)} · ok {ok} · fail {fail}")
        tail = "\n\n" + "\n".join(f"• {h(e)}" for e in errs[:15]) if errs else ""
        await status.edit_text(f"📚 Done. Stored {ok}, failed {fail}.{tail}")
        if ok:
            await broadcast(client, f"🔄 {h(m.from_user.first_name)} bulk-imported {ok} accounts ({fail} failed).",
                            exclude=m.from_user.id)
        fsm.clear(m.from_user.id)
        return

    if name == "wait_tdata":
        path = await download_doc(client, m, ".zip")
        if not path:
            return
        fsm.set(m.from_user.id, "wait_tdata_pass", zip_path=str(path), convert_only=bool(st.data.get("convert_only")))
        return await _run_tdata(client, m, path, None, convert_only=bool(st.data.get("convert_only")))

    if name == "conv_file":
        path = await download_doc(client, m, ".session")
        if not path:
            return
        try:
            parts = parse_session_file(path)
            pyro, tele = TMP_DIR / f"{path.stem}_pyro.session", TMP_DIR / f"{path.stem}_tele.session"
            write_pyro_file(pyro, parts)
            write_tele_file(tele, parts)
            await m.reply_text(conv_card(parts))
            await m.reply_document(str(pyro), caption="Pyrogram .session")
            await m.reply_document(str(tele), caption="Telethon .session")
            pyro.unlink(missing_ok=True)
            tele.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            await m.reply_text(f"❌ {h(e)}")
        finally:
            path.unlink(missing_ok=True)
            fsm.clear(m.from_user.id)
        return

    if name == "wait_backup":
        if not (m.document.file_name or "").endswith(".json"):
            return await m.reply_text("Send a .json backup.")
        dest = TMP_DIR / f"restore_{m.from_user.id}.json"
        await botapi_download(m.document.file_id, dest)
        try:
            stats = await db.import_workspace(json.loads(dest.read_text(encoding="utf-8")))
            await m.reply_text(f"✅ Merged. Accounts {stats['accounts']} · Admins {stats['admins']} · Settings {stats['settings']}")
        except Exception as e:  # noqa: BLE001
            await m.reply_text(f"❌ {h(e)}")
        finally:
            dest.unlink(missing_ok=True)
            fsm.clear(m.from_user.id)
        return

    await m.reply_text("This step expects text, not a file. /cancel")


async def _finish_login(bot, m, tg, has_2fa=False):
    try:
        info = await inspect_client(tg)
        await persist(bot, m, info, "phone_otp", has_2fa=has_2fa)
    except Exception as e:  # noqa: BLE001
        await db.inc("add_fail", 1)
        await m.reply_text(f"Logged in but failed to store: <code>{h(e)}</code>")
    finally:
        fsm.clear(m.from_user.id)


async def _import_text(bot, m, raw, source):
    wait = await m.reply_text("Connecting…")
    try:
        info = await inspect_text(raw)
        await persist(bot, m, info, source)
        try:
            await wait.delete()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        await db.inc("add_fail", 1)
        await wait.edit_text(f"❌ Import failed: <code>{h(e)}</code>")
    finally:
        fsm.clear(m.from_user.id)


async def _run_tdata(bot, m, zip_path: Path, passcode, convert_only=False):
    wait = await m.reply_text("Extracting tdata…")
    try:
        parts_list = await zip_to_parts(zip_path, passcode)
        for i, parts in enumerate(parts_list, 1):
            if convert_only:
                await m.reply_text(f"Account {i}/{len(parts_list)}\n" + conv_card(parts))
            else:
                info = await inspect_parts(parts)
                await persist(bot, m, info, "tdata")
        await wait.edit_text(f"📦 Done — {len(parts_list)} account(s).")
        zip_path.unlink(missing_ok=True)
        fsm.clear(m.from_user.id)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "passcode" in msg.lower() and passcode is None:
            await wait.edit_text("Looks passcode-locked. Send the local passcode, or /cancel.")
            return
        await wait.edit_text(f"❌ {h(msg)}")
        zip_path.unlink(missing_ok=True)
        fsm.clear(m.from_user.id)


async def _2fa_flow(m: Message, name: str, data: dict, text: str):
    aid = data.get("account_id")
    if not aid:
        fsm.clear(m.from_user.id)
        return await m.reply_text("Missing account. /cancel")
    if name == "2fa_en_new":
        fsm.set(m.from_user.id, "2fa_en_hint", account_id=aid, new_password=text)
        return await m.reply_text("Optional hint (or send <code>-</code>).", reply_markup=cancel_kb())
    if name == "2fa_en_hint":
        return await _run_2fa(m, aid, "enable", new=data.get("new_password"), hint="" if text == "-" else text)
    if name == "2fa_dis":
        return await _run_2fa(m, aid, "disable", current=text)
    if name == "2fa_ch_cur":
        fsm.set(m.from_user.id, "2fa_ch_new", account_id=aid, current=text)
        return await m.reply_text("Send the <b>new</b> password.", reply_markup=cancel_kb())
    if name == "2fa_ch_new":
        fsm.set(m.from_user.id, "2fa_ch_hint", account_id=aid, current=data.get("current"), new_password=text)
        return await m.reply_text("Optional new hint (or <code>-</code>).", reply_markup=cancel_kb())
    if name == "2fa_ch_hint":
        return await _run_2fa(m, aid, "change", current=data.get("current"),
                              new=data.get("new_password"), hint="" if text == "-" else text)


async def _run_2fa(m, aid, action, current=None, new=None, hint=""):
    try:
        _, tg = await client_for(aid)
        async with opened(tg):
            if action == "enable":
                await tg.edit_2fa(new_password=new, hint=hint or "")
            elif action == "disable":
                await tg.edit_2fa(current_password=current, new_password=None)
            else:
                await tg.edit_2fa(current_password=current, new_password=new, hint=hint or "")
        await db.update_account(aid, {
            "has_2fa": action != "disable",
            "twofa_hint": hint or "",
        })
        await m.reply_text(f"✅ 2FA {action} succeeded.", reply_markup=twofa_kb(aid))
    except Exception as e:  # noqa: BLE001
        await m.reply_text(f"❌ 2FA {action} failed: <code>{h(e)}</code>")
    finally:
        fsm.clear(m.from_user.id)



# ═══════════════════════════════════════════════════════════════════
# Bot API dispatcher (webhook + getUpdates)
# ═══════════════════════════════════════════════════════════════════

_CB_ROUTES: list[tuple[re.Pattern, Any]] = []


def _install_cb_routes() -> None:
    if _CB_ROUTES:
        return
    pairs = [
        (r"^m:(main|help|x|cancel|dash)$", cb_root),
        (r"^a:l:(\d+)$", cb_list),
        (r"^a:v:([0-9a-f]+)$", cb_view),
        (r"^a:p:([0-9a-f]+)$", cb_ping),
        (r"^a:dna:([0-9a-f]+)$", cb_dna_one),
        (r"^a:dnaA$", cb_dna_all),
        (r"^a:d1:([0-9a-f]+)$", cb_del_ask),
        (r"^a:dx:([0-9a-f]+)$", cb_del_do),
        (r"^a:wipe1$", cb_wipe_ask),
        (r"^a:wipeX$", cb_wipe_do),
        (r"^h:(td|sf|hx|sk|rm)$", cb_hub),
        (r"^pk:(hex|str|file|tdata|rm|spam|otp|cln):(\d+)$", cb_pick),
        (r"^pg:(hex|str|file|tdata|rm|spam|otp|cln):([0-9a-f]+)$", cb_pick_go),
        (r"^n:m$", cb_add),
        (r"^n:(ph|hx|st|sf|tx|td)$", cb_add_kind),
        (r"^c:m$", cb_conv),
        (r"^c:(paste|file|td)$", cb_conv_kind),
        (r"^c:ex:([shf]):([0-9a-f]+)$", cb_export),
        (r"^s:m$", cb_sec),
        (r"^s:2:([0-9a-f]+)$", cb_2fa),
        (r"^s:2e:([0-9a-f]+)$", cb_2e),
        (r"^s:2d:([0-9a-f]+)$", cb_2d),
        (r"^s:2c:([0-9a-f]+)$", cb_2c),
        (r"^s:k1:([0-9a-f]+)$", cb_kill_ask),
        (r"^s:kx:([0-9a-f]+)$", cb_kill_do),
        (r"^s:t1:([0-9a-f]+)$", cb_term_ask),
        (r"^s:tx:([0-9a-f]+)$", cb_term_do),
        (r"^s:dv:([0-9a-f]+)$", cb_devices),
        (r"^s:dl:([0-9a-f]+):(\d+)$", cb_dev_out),
        (r"^k:m$", cb_cln),
        (r"^k:d1:([0-9a-f]+)$", cb_dm_ask),
        (r"^k:dx:([0-9a-f]+)$", cb_dm_do),
        (r"^k:n1:([0-9a-f]+)$", cb_nu_ask),
        (r"^k:nx:([0-9a-f]+)$", cb_nu_do),
        (r"^p:m$", cb_spam),
        (r"^p:1:([0-9a-f]+)$", cb_spam1),
        (r"^p:all$", cb_spam_all),
        (r"^o:m$", cb_otp),
        (r"^o:1:([0-9a-f]+)$", cb_otp1),
        (r"^o:all$", cb_otp_all),
        (r"^l:m$", cb_alerts),
        (r"^l:tg:(alerts_logout|alerts_ban|monitor_enabled)$", cb_tog),
        (r"^l:iv$", cb_iv),
        (r"^l:now$", cb_now),
        (r"^d:m$", cb_db),
        (r"^d:dl$", cb_dl),
        (r"^e:logs$", cb_logs),
        (r"^d:up$", cb_up),
        (r"^u:m$", cb_adm),
        (r"^u:add$", cb_adm_add),
        (r"^u:del$", cb_adm_del),
    ]
    for pat, fn in pairs:
        _CB_ROUTES.append((re.compile(pat), fn))


async def handle_update(upd: dict) -> None:
    _install_cb_routes()
    try:
        if "callback_query" in upd:
            cq = ApiCallback(upd["callback_query"])
            log.info("callback %s from %s", cq.data, cq.from_user.id)
            for rx, fn in _CB_ROUTES:
                if rx.match(cq.data or ""):
                    await fn(app, cq)
                    return
            log.warning("unhandled callback %s", cq.data)
            await cq.answer()
            return
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        m = ApiMessage(msg)
        if m.from_user and m.from_user.is_bot:
            return
        text = (m.text or "").strip()
        log.info("message from %s %r", getattr(m.from_user, "id", None), text[:80])
        cmd = text.split("@", 1)[0].split(" ", 1)[0].lower()
        if cmd in {"/start", "/menu"}:
            await cmd_start(app, m)
        elif cmd == "/help":
            await cmd_help(app, m)
        elif cmd == "/cancel":
            await cmd_cancel(app, m)
        elif cmd == "/id":
            await cmd_id(app, m)
        elif m.document:
            await on_doc(app, m)
        elif text:
            await on_text(app, m)
    except Exception:
        log.exception("handle_update failed")


async def drain_and_webhook() -> None:
    """Attach webhook. Drop stale queued /start so we do not replay 80 dashboards."""
    url = (PUBLIC_URL or "").strip().rstrip("/")
    try:
        await botapi_async("deleteWebhook", drop_pending_updates="true")
    except Exception:
        log.exception("deleteWebhook")
    if url.startswith("http"):
        hook = url + "/telegram"
        try:
            res = await botapi_async(
                "setWebhook",
                url=hook,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=json.dumps(["message", "callback_query"]),
                drop_pending_updates="true",
            )
            log.info("Webhook set %s → %s", hook, res)
        except Exception:
            log.exception("setWebhook failed")
    else:
        log.warning("No PUBLIC_URL — staying on getUpdates poll")


async def poll_loop() -> None:
    """Fallback long-poll if webhook is not attached."""
    await asyncio.sleep(4)
    offset = 0
    while True:
        try:
            info = await botapi_async("getWebhookInfo")
            if (info.get("result") or {}).get("url"):
                await asyncio.sleep(30)
                continue
            data = await botapi_async(
                "getUpdates",
                offset=offset,
                timeout=25,
                allowed_updates=json.dumps(["message", "callback_query"]),
            )
            for u in data.get("result") or []:
                offset = int(u.get("update_id") or 0) + 1
                await handle_update(u)
        except Exception:
            log.exception("poll_loop")
            await asyncio.sleep(3)


# ═══════════════════════════════════════════════════════════════════
# Render web server + self-ping keep-alive
# ═══════════════════════════════════════════════════════════════════

def _keepalive_target() -> str:
    url = (KEEP_ALIVE_URL or os.getenv("RENDER_EXTERNAL_URL", "") or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    return url + "/health"


async def _read_http(reader: asyncio.StreamReader) -> tuple[str, dict[str, str], bytes]:
    sep = (chr(13) + chr(10) + chr(13) + chr(10)).encode()
    nl = (chr(13) + chr(10)).encode()
    buf = b""
    while sep not in buf and len(buf) < 65536:
        chunk = await asyncio.wait_for(reader.read(2048), timeout=15)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(sep)
    lines = head.decode("iso-8859-1", "replace").split(nl.decode("ascii"))
    req = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    need = int(headers.get("content-length") or 0)
    body = rest
    while len(body) < need:
        body += await asyncio.wait_for(reader.read(need - len(body)), timeout=20)
    return req, headers, body[:need]


async def start_web_server() -> asyncio.AbstractServer:
    """Health + Telegram webhook so Render stays up and /start is received."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = "200 OK"
        payload = {
            "ok": True,
            "service": "session-manager-pro",
            "bot": f"@{BOT_USERNAME}",
            "python": sys.version.split()[0],
            "opentele": bool(HAS_OPENTELE),
            "ts": utcnow().isoformat(),
        }
        try:
            req, headers, body = await _read_http(reader)
            parts = req.split()
            method = parts[0] if parts else "GET"
            path = parts[1].split("?")[0] if len(parts) > 1 else "/"
            if method == "POST" and path.startswith("/telegram"):
                secret = headers.get("x-telegram-bot-api-secret-token", "")
                if secret and secret != WEBHOOK_SECRET:
                    status = "403 Forbidden"
                    payload = {"ok": False, "error": "bad secret"}
                else:
                    try:
                        upd = json.loads(body.decode("utf-8") or "{}")
                    except Exception:
                        upd = {}
                    if isinstance(upd, dict) and upd:
                        asyncio.create_task(handle_update(upd))
                    payload = {"ok": True}
        except Exception:
            log.exception("http handle")
        raw = json.dumps(payload).encode()
        nl = chr(13) + chr(10)
        header = (
            f"HTTP/1.1 {status}{nl}"
            f"Content-Type: application/json{nl}"
            f"Content-Length: {len(raw)}{nl}"
            f"Connection: close{nl}{nl}"
        ).encode()
        try:
            writer.write(header + raw)
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    server = await asyncio.start_server(handle, WEB_HOST, WEB_PORT)
    log.info("Web/webhook server on %s:%s", WEB_HOST, WEB_PORT)
    return server


async def keepalive_loop() -> None:
    """Ping our own public URL so Render free tier does not sleep after 15 minutes."""
    await asyncio.sleep(8)
    while True:
        url = _keepalive_target()
        if url:
            try:
                def _ping(u: str) -> int:
                    req = urllib.request.Request(u, headers={"User-Agent": "smp-keepalive/1.0"})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        return int(getattr(resp, "status", 200) or 200)

                status = await asyncio.to_thread(_ping, url)
                log.info("Keep-alive ping %s → %s", url, status)
            except Exception as exc:  # noqa: BLE001
                log.warning("Keep-alive ping failed (%s): %s", url, exc)
        else:
            log.info("Keep-alive waiting for KEEP_ALIVE_URL or RENDER_EXTERNAL_URL")
        await asyncio.sleep(max(20, KEEP_ALIVE_INTERVAL))


# ═══════════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════════

async def amain() -> None:
    global monitor, BOT_USERNAME
    WORKDIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    await db.connect()
    web = await start_web_server()
    ka_task = asyncio.create_task(keepalive_loop(), name="keep-alive")
    poll_task = asyncio.create_task(poll_loop(), name="botapi-poll")
    monitor = Monitor(app)
    try:
        await app.start()
        me = await app.get_me()
        BOT_USERNAME = me.username or BOT_USERNAME
        log.info("Pyrogram sender online as @%s (%s)", me.username, me.id)
    except Exception:
        log.exception("pyrogram start failed — Bot API path still active")
    await drain_and_webhook()
    try:
        await botapi_send(
            OWNER_ID,
            "✅ Render receiver is ON. Send /start now — dashboard should reply.",
        )
    except Exception:
        log.exception("owner ping")
    monitor.start()
    dna_task = asyncio.create_task(_dna_loop(), name="tgdna-auto")
    log.info("Bot ready @%s webhook=%s", BOT_USERNAME, PUBLIC_URL)
    try:
        await idle()
    finally:
        ka_task.cancel()
        poll_task.cancel()
        dna_task.cancel()
        monitor.stop()
        try:
            await botapi_async("deleteWebhook", drop_pending_updates="false")
        except Exception:
            pass
        web.close()
        await web.wait_closed()
        try:
            await app.stop()
        except Exception:
            pass
        await db.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception("fatal")
        print(f"\nFatal:\n{e}\n", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
