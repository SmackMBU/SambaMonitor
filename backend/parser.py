from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath
from typing import Any, Iterable, TypedDict

logger = logging.getLogger(__name__)


class OpenFileEntry(TypedDict):
    filename: str
    filepath: str
    pid: int
    user: str
    opened_at: str | None


def parse_smbstatus_output(raw_output: str) -> list[OpenFileEntry]:
    text = raw_output.strip()
    if not text:
        return []

    if text[0] in "{[":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("smbstatus --json returned invalid JSON, fallback to plain parser.")
        else:
            return _deduplicate(_parse_smbstatus_json(payload))

    return _deduplicate(_parse_smbstatus_plain(text))


def parse_lsof_output(raw_output: str) -> list[OpenFileEntry]:
    entries: list[OpenFileEntry] = []
    current_pid: int | None = None
    current_user = "unknown"

    for line in raw_output.splitlines():
        if not line:
            continue

        field, value = line[0], line[1:].strip()

        if field == "p":
            current_pid = _to_int(value)
            current_user = "unknown"
            continue

        if field == "L" and value:
            current_user = value
            continue

        if field != "n" or current_pid is None:
            continue

        filepath = value.removesuffix(" (deleted)")
        if not filepath.startswith("/"):
            continue

        entries.append(
            OpenFileEntry(
                filename=PurePosixPath(filepath).name or filepath,
                filepath=filepath,
                pid=current_pid,
                user=current_user,
                opened_at=None,
            )
        )

    return _deduplicate(entries)


def _parse_smbstatus_json(payload: Any) -> list[OpenFileEntry]:
    if isinstance(payload, dict):
        modern_entries = _parse_modern_smbstatus_json(payload)
        if modern_entries:
            return modern_entries

    entries: list[OpenFileEntry] = []
    for item in _iter_json_entries(payload):
        pid = _extract_pid(item)
        if pid is None:
            continue

        user = _extract_user(item, {})

        filepath = _extract_filepath(item)
        if not filepath:
            continue

        entries.append(
            OpenFileEntry(
                filename=PurePosixPath(filepath).name or filepath,
                filepath=filepath,
                pid=pid,
                user=user,
                opened_at=_extract_opened_at(item),
            )
        )

    return entries


def _parse_modern_smbstatus_json(payload: dict[str, Any]) -> list[OpenFileEntry]:
    open_files = payload.get("open_files")
    if not isinstance(open_files, dict):
        return []

    uid_to_user = _build_uid_user_map(payload)
    entries: list[OpenFileEntry] = []

    for file_path_key, file_info in open_files.items():
        if not isinstance(file_info, dict):
            continue

        filepath = (
            str(file_path_key).strip()
            if isinstance(file_path_key, str) and file_path_key.strip()
            else _extract_filepath(file_info)
        )
        if not filepath:
            continue

        filename = str(file_info.get("filename") or PurePosixPath(filepath).name or filepath)
        opened_at_fallback = _extract_opened_at(file_info)
        opens = file_info.get("opens")

        if isinstance(opens, dict) and opens:
            for open_id, open_info in opens.items():
                if not isinstance(open_info, dict):
                    continue

                pid = _extract_pid(open_info, open_id=open_id)
                if pid is None:
                    continue

                entries.append(
                    OpenFileEntry(
                        filename=filename,
                        filepath=filepath,
                        pid=pid,
                        user=_extract_user(open_info, uid_to_user),
                        opened_at=_extract_opened_at(open_info) or opened_at_fallback,
                    )
                )
            continue

        # Compatibility path for payloads where pid/user are directly under open_files entry.
        pid = _extract_pid(file_info)
        if pid is None:
            continue

        entries.append(
            OpenFileEntry(
                filename=filename,
                filepath=filepath,
                pid=pid,
                user=_extract_user(file_info, uid_to_user),
                opened_at=opened_at_fallback,
            )
        )

    return entries


def _build_uid_user_map(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        return result

    for session_info in sessions.values():
        if not isinstance(session_info, dict):
            continue
        uid = session_info.get("uid")
        username = session_info.get("username") or session_info.get("user")
        if uid is None or not username:
            continue
        result[str(uid)] = str(username)
    return result


def _extract_pid(item: dict[str, Any], *, open_id: Any | None = None) -> int | None:
    direct_candidates = (
        item.get("pid"),
        item.get("PID"),
        item.get("process_id"),
        item.get("smb_pid"),
    )
    for candidate in direct_candidates:
        parsed = _to_int(candidate)
        if parsed is not None:
            return parsed

    server_id = item.get("server_id")
    if isinstance(server_id, dict):
        parsed = _to_int(server_id.get("pid"))
        if parsed is not None:
            return parsed

    # Modern smbstatus JSON often uses keys like "2953/408" inside "opens".
    if isinstance(open_id, str) and "/" in open_id:
        parsed = _to_int(open_id.split("/", 1)[0])
        if parsed is not None:
            return parsed

    return None


def _extract_user(item: dict[str, Any], uid_to_user: dict[str, str]) -> str:
    for key in ("username", "user", "owner"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    uid = item.get("uid")
    if uid is not None:
        uid_key = str(uid)
        if uid_key in uid_to_user:
            return uid_to_user[uid_key]
        return uid_key

    return "unknown"


def _iter_json_entries(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_json_entries(item)
        return

    if not isinstance(payload, dict):
        return

    list_keys = ("open_files", "locked_files", "locks", "files")
    for key in list_keys:
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item

    for value in payload.values():
        if isinstance(value, dict):
            yield from _iter_json_entries(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    yield from _iter_json_entries(item)


def _parse_smbstatus_plain(text: str) -> list[OpenFileEntry]:
    if "locked files" not in text.lower():
        return []

    lines = text.splitlines()
    locked_start: int | None = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("locked files"):
            locked_start = i + 1
            break

    if locked_start is None:
        return []

    entries: list[OpenFileEntry] = []
    for raw_line in lines[locked_start:]:
        line = raw_line.strip()
        if not line:
            continue
        if set(line) <= {"-"}:
            continue
        lowered = line.lower()
        if lowered.startswith("pid") or lowered.startswith("samba"):
            continue
        if lowered.endswith("files:") and entries:
            break

        columns = raw_line.split(maxsplit=7)
        if len(columns) < 8:
            continue

        pid = _to_int(columns[0])
        if pid is None:
            continue

        user = columns[1]
        share_path = columns[6]

        name_and_time = columns[7].strip()
        if not name_and_time:
            continue

        name_parts = name_and_time.split(maxsplit=1)
        file_name = name_parts[0]
        opened_at = name_parts[1] if len(name_parts) > 1 else None
        filepath = _join_path(share_path, file_name)

        entries.append(
            OpenFileEntry(
                filename=file_name,
                filepath=filepath,
                pid=pid,
                user=user,
                opened_at=opened_at,
            )
        )

    return entries


def _extract_filepath(item: dict[str, Any]) -> str | None:
    direct_candidates = ("filepath", "path", "full_path", "file_path", "filename", "file")
    for key in direct_candidates:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    share_path = item.get("share_path") or item.get("sharepath")
    name = item.get("name")
    if isinstance(share_path, str) and isinstance(name, str):
        return _join_path(share_path, name)
    return None


def _extract_opened_at(item: dict[str, Any]) -> str | None:
    for key in ("opened_at", "open_time", "time", "connected_at"):
        value = item.get(key)
        if value is None:
            continue
        stringified = str(value).strip()
        if stringified:
            return stringified
    return None


def _join_path(base_path: str, child_name: str) -> str:
    if child_name.startswith("/"):
        return child_name
    clean_base = base_path.rstrip("/")
    if not clean_base:
        return f"/{child_name.lstrip('/')}"
    return f"{clean_base}/{child_name.lstrip('/')}"


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _deduplicate(entries: list[OpenFileEntry]) -> list[OpenFileEntry]:
    unique: list[OpenFileEntry] = []
    seen: set[tuple[int, str, str]] = set()
    for entry in entries:
        key = (entry["pid"], entry["filepath"], entry["user"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique
