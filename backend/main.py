from __future__ import annotations

import base64
import re
import hmac
import logging
import os
import time
from fnmatch import translate as fnmatch_translate
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .parser import OpenFileEntry
from .ssh_client import (
    SSHCommandError,
    SSHConfig,
    SSHConnectionError,
    SSHSambaClient,
    SSHValidationError,
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
INDEX_TEMPLATE_PATH = FRONTEND_DIR / "index.html"
CACHE_TTL_SECONDS = max(1, int(os.getenv("CACHE_TTL_SECONDS", "7")))
WILDCARD_META_CHARS = set("*?[]")


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is required.")
    return value


def _normalize_root_uri(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized == "/":
        return ""
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/")


APP_USERNAME = _require_env("APP_USERNAME")
APP_PASSWORD = _require_env("APP_PASSWORD")
APP_ROOT_URI = _normalize_root_uri(os.getenv("APP_ROOT_URI", ""))
INDEX_TEMPLATE = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")

ssh_config = SSHConfig(
    host=_require_env("SSH_HOST"),
    username=_require_env("SSH_USER"),
    port=int(os.getenv("SSH_PORT", "22")),
    password=os.getenv("SSH_PASSWORD"),
    key=os.getenv("SSH_KEY"),
    timeout_seconds=float(os.getenv("SSH_TIMEOUT_SECONDS", "10")),
)
ssh_client = SSHSambaClient(ssh_config)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, username: str, password: str) -> None:
        super().__init__(app)
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.url.path == "/healthz":
            return await call_next(request)

        authorization = request.headers.get("Authorization")
        if not self._is_authorized(authorization):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": "Basic"},
                content="Unauthorized",
            )
        return await call_next(request)

    def _is_authorized(self, auth_header: str | None) -> bool:
        if not auth_header or not auth_header.startswith("Basic "):
            return False

        try:
            encoded = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
        except Exception:
            return False

        username, _, password = decoded.partition(":")
        return hmac.compare_digest(username.encode("utf-8"), self.username) and hmac.compare_digest(
            password.encode("utf-8"), self.password
        )


class OpenFilesCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._value: list[OpenFileEntry] | None = None
        self._expires_at = 0.0
        self._lock = Lock()

    def get(self) -> list[OpenFileEntry] | None:
        with self._lock:
            now = time.time()
            if self._value is None or now >= self._expires_at:
                return None
            return list(self._value)

    def set(self, value: list[OpenFileEntry]) -> None:
        with self._lock:
            self._value = list(value)
            self._expires_at = time.time() + self.ttl_seconds

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._expires_at = 0.0


def _render_index_html() -> str:
    return INDEX_TEMPLATE.replace("__APP_ROOT_URI__", APP_ROOT_URI)


app = FastAPI(title="Samba Open Files Monitor", version="1.0.0")
app.add_middleware(BasicAuthMiddleware, username=APP_USERNAME, password=APP_PASSWORD)
cache = OpenFilesCache(CACHE_TTL_SECONDS)
router = APIRouter(prefix=APP_ROOT_URI)

logger.info("Configured app root URI: %s", APP_ROOT_URI or "/")


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


if APP_ROOT_URI:

    @app.get(APP_ROOT_URI, include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{APP_ROOT_URI}/", status_code=307)


@router.get("/")
async def get_index() -> HTMLResponse:
    return HTMLResponse(_render_index_html())


@router.get("/static/{asset_path:path}")
async def get_static(asset_path: str) -> FileResponse:
    safe_root = FRONTEND_DIR.resolve()
    file_path = (FRONTEND_DIR / asset_path).resolve()

    try:
        file_path.relative_to(safe_root)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(file_path)


@router.get("/files")
async def list_open_files(
    search: str | None = Query(default=None),
    extension: str | None = Query(default=None),
    refresh: bool = Query(default=False),
) -> JSONResponse:
    try:
        files = await _get_open_files(refresh)
    except SSHConnectionError as exc:
        logger.exception("SSH connection error while fetching files.")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SSHCommandError as exc:
        logger.exception("SSH command error while fetching files.")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    filtered_files = _filter_entries(files, search=search, extension=extension)
    return JSONResponse({"files": filtered_files, "count": len(filtered_files)})


@router.post("/close/{pid}")
async def close_connection(pid: int) -> JSONResponse:
    if pid <= 0:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "PID must be a positive integer."},
        )

    try:
        message = await run_in_threadpool(ssh_client.close_samba_connection, pid)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})
    except SSHValidationError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})
    except SSHConnectionError as exc:
        logger.exception("SSH connection error while closing Samba PID %s.", pid)
        return JSONResponse(status_code=502, content={"status": "error", "message": str(exc)})
    except SSHCommandError as exc:
        logger.exception("SSH command error while closing Samba PID %s.", pid)
        return JSONResponse(status_code=502, content={"status": "error", "message": str(exc)})

    cache.clear()
    return JSONResponse(
        {"status": "success", "message": message, "pid": pid},
    )


app.include_router(router)


async def _get_open_files(refresh: bool) -> list[OpenFileEntry]:
    if not refresh:
        cached = cache.get()
        if cached is not None:
            return cached

    fresh = await run_in_threadpool(ssh_client.fetch_open_files)
    cache.set(fresh)
    return fresh


def _filter_entries(
    entries: list[OpenFileEntry],
    *,
    search: str | None,
    extension: str | None,
) -> list[OpenFileEntry]:
    search_value = (search or "").strip()
    extension_masks = _split_masks(extension)
    normalized_extension_masks = [_normalize_extension_mask(mask) for mask in extension_masks]
    extension_patterns = [
        _compile_wildcard_pattern(mask)
        for mask in normalized_extension_masks
        if mask
    ]

    search_masks = _split_masks(search)
    search_wildcard_patterns = [
        _compile_wildcard_pattern(mask)
        for mask in search_masks
        if _contains_wildcards(mask)
    ]
    search_substrings = [mask.casefold() for mask in search_masks if not _contains_wildcards(mask)]

    if not search_value and not extension_patterns:
        return list(entries)

    filtered: list[OpenFileEntry] = []
    for entry in entries:
        filename = entry["filename"]
        filepath = entry["filepath"]

        if search_value and (search_wildcard_patterns or search_substrings):
            searchable = f"{filename} {filepath}".casefold()
            wildcard_match = any(
                pattern.fullmatch(filename) or pattern.fullmatch(filepath)
                for pattern in search_wildcard_patterns
            )
            substring_match = any(term in searchable for term in search_substrings)
            if not wildcard_match and not substring_match:
                continue

        if extension_patterns:
            if not any(pattern.fullmatch(filename) for pattern in extension_patterns):
                continue

        filtered.append(entry)
    return filtered


def _split_masks(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.strip()
    if not normalized:
        return []
    return [part.strip() for part in re.split(r"[;,]", normalized) if part.strip()]


def _contains_wildcards(value: str) -> bool:
    return any(char in value for char in WILDCARD_META_CHARS)


def _normalize_extension_mask(mask: str) -> str:
    normalized = mask.strip()
    if not normalized:
        return ""
    if _contains_wildcards(normalized):
        return normalized
    if normalized.startswith("."):
        return f"*{normalized}"
    return f"*.{normalized}"


@lru_cache(maxsize=256)
def _compile_wildcard_pattern(mask: str) -> re.Pattern[str]:
    return re.compile(fnmatch_translate(mask), flags=re.IGNORECASE)
