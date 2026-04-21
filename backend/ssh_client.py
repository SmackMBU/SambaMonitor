from __future__ import annotations

import io
import json
import logging
import shlex
from dataclasses import dataclass

import paramiko

from .parser import OpenFileEntry, parse_lsof_output, parse_smbstatus_output

logger = logging.getLogger(__name__)


class SSHConnectionError(RuntimeError):
    """Raised when an SSH connection cannot be established."""


class SSHCommandError(RuntimeError):
    """Raised when a remote command fails."""


class SSHValidationError(SSHCommandError):
    """Raised when remote PID validation fails."""


@dataclass(frozen=True)
class SSHConfig:
    host: str
    username: str
    port: int = 22
    password: str | None = None
    key: str | None = None
    timeout_seconds: float = 10.0


class SSHSambaClient:
    ALLOWED_COMMANDS = {
        "smbstatus_json": "sudo smbstatus --json",
        "smbstatus_plain": "sudo smbstatus",
        "lsof_smbd": "lsof -nP -c smbd -F pLfn",
        "ps_exists": "ps -p {pid}",
        "ps_comm": "ps -p {pid} -o comm=",
        "smbcontrol_close": "sudo smbcontrol {pid} close-share {share}",
    }

    def __init__(self, config: SSHConfig):
        self.config = config

    def fetch_open_files(self) -> list[OpenFileEntry]:
        status, stdout_text, stderr_text = self._run_command("smbstatus_json")
        if status == 0 and stdout_text.strip():
            parsed = parse_smbstatus_output(stdout_text)
            if parsed:
                logger.info("Collected %s entries via sudo smbstatus --json", len(parsed))
                return parsed

            if '"open_files"' not in stdout_text:
                logger.info("Collected 0 entries via sudo smbstatus --json")
                return parsed

            logger.warning(
                "sudo smbstatus --json returned data but parser produced 0 entries. "
                "Falling back to plain output."
            )

        if status != 0:
            logger.warning(
                "sudo smbstatus --json failed with code %s, stderr=%s",
                status,
                stderr_text.strip(),
            )

        status, stdout_text, stderr_text = self._run_command("smbstatus_plain")
        if status == 0:
            parsed = parse_smbstatus_output(stdout_text)
            if parsed:
                logger.info("Collected %s entries via sudo smbstatus plain output", len(parsed))
                return parsed

            normalized_text = stdout_text.lower()
            if "no locked files" in normalized_text or "locked files" in normalized_text:
                logger.info("sudo smbstatus reported no open Samba files.")
                return parsed

            logger.warning("sudo smbstatus output was not parseable, using lsof fallback.")

        if status == 0:
            logger.warning("sudo smbstatus plain output is unsupported for parser. Falling back to lsof.")
        else:
            logger.warning(
                "sudo smbstatus plain failed with code %s, stderr=%s. Falling back to lsof.",
                status,
                stderr_text.strip(),
            )

        status, stdout_text, stderr_text = self._run_command("lsof_smbd")
        # lsof returns code 1 when no matching open files were found.
        if status not in (0, 1):
            raise SSHCommandError(
                f"Fallback lsof failed (code {status}): {stderr_text.strip() or stdout_text.strip()}"
            )

        parsed = parse_lsof_output(stdout_text)
        logger.info("Collected %s entries via lsof fallback", len(parsed))
        return parsed

    def close_samba_connection(self, pid: int) -> str:
        if pid <= 0:
            raise ValueError("PID must be a positive integer.")

        status, stdout_text, stderr_text = self._run_command("ps_exists", pid=pid)
        if status != 0:
            error_text = stderr_text.strip() or stdout_text.strip()
            if error_text:
                raise SSHValidationError(f"PID {pid} was not found: {error_text}")
            raise SSHValidationError(f"PID {pid} was not found.")

        status, stdout_text, stderr_text = self._run_command("ps_comm", pid=pid)
        if status != 0:
            error_text = stderr_text.strip() or stdout_text.strip() or f"exit code {status}"
            raise SSHCommandError(f"Failed to inspect PID {pid}: {error_text}")

        process_name = stdout_text.strip()
        if not self._is_smbd_process_name(process_name):
            safe_name = process_name or "unknown"
            raise SSHValidationError(
                f"PID {pid} belongs to '{safe_name}'. Only 'smbd' processes can be closed."
            )

        shares = self._discover_shares_for_pid(pid)
        if not shares:
            raise SSHValidationError(
                f"Could not determine Samba share for PID {pid}. "
                "Please verify there is an active SMB tree connection."
            )

        for share in shares:
            status, stdout_text, stderr_text = self._run_command(
                "smbcontrol_close",
                pid=pid,
                share=share,
            )
            if status != 0:
                error_text = stderr_text.strip() or stdout_text.strip() or f"exit code {status}"
                if "sudo" in error_text.lower():
                    raise SSHCommandError(
                        f"sudo error while closing Samba PID {pid} for share '{share}': {error_text}"
                    )
                raise SSHCommandError(
                    f"Failed to close Samba connection for PID {pid} (share '{share}'): {error_text}"
                )

        logger.info("Samba connection for PID %s was closed on shares: %s", pid, ", ".join(shares))
        if len(shares) == 1:
            return f"Samba connection for PID {pid} was closed for share '{shares[0]}'."
        return (
            f"Samba connection for PID {pid} was closed for shares: {', '.join(shares)}."
        )

    @staticmethod
    def _is_smbd_process_name(process_name: str) -> bool:
        normalized = process_name.strip().lower()
        if not normalized:
            return False

        # Depending on ps format, Samba workers may appear as:
        # smbd, smbd[client], smbd: worker, /usr/sbin/smbd
        leaf = normalized.rsplit("/", 1)[-1]
        return leaf.startswith("smbd")

    def _discover_shares_for_pid(self, pid: int) -> list[str]:
        status, stdout_text, stderr_text = self._run_command("smbstatus_json")
        if status != 0:
            error_text = stderr_text.strip() or stdout_text.strip() or f"exit code {status}"
            raise SSHCommandError(
                f"Failed to discover SMB share for PID {pid} via smbstatus --json: {error_text}"
            )

        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise SSHCommandError(
                f"Failed to parse smbstatus --json while resolving share for PID {pid}: {exc}"
            ) from exc

        tcons = payload.get("tcons")
        if not isinstance(tcons, dict):
            return []

        shares: set[str] = set()
        for tcon_info in tcons.values():
            if not isinstance(tcon_info, dict):
                continue

            server_id = tcon_info.get("server_id")
            server_pid = None
            if isinstance(server_id, dict):
                server_pid = _to_int(server_id.get("pid"))

            if server_pid != pid:
                continue

            share_name = tcon_info.get("service")
            if isinstance(share_name, str):
                normalized_share = share_name.strip()
                if normalized_share:
                    shares.add(normalized_share)

        return sorted(shares)

    def _run_command(
        self,
        command_key: str,
        *,
        pid: int | None = None,
        share: str | None = None,
    ) -> tuple[int, str, str]:
        if command_key not in self.ALLOWED_COMMANDS:
            raise ValueError(f"Command '{command_key}' is not allowed.")

        command_template = self.ALLOWED_COMMANDS[command_key]
        format_values: dict[str, object] = {}
        if "{pid}" in command_template:
            if pid is None or pid <= 0:
                raise ValueError("A positive PID is required for this command.")
            format_values["pid"] = pid
        if "{share}" in command_template:
            if share is None or not share.strip():
                raise ValueError("A non-empty Samba share name is required for this command.")
            format_values["share"] = shlex.quote(share.strip())

        command = command_template.format(**format_values) if format_values else command_template

        client = self._connect()
        try:
            _, stdout, stderr = client.exec_command(command, timeout=self.config.timeout_seconds)
            exit_status = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")
            return exit_status, stdout_text, stderr_text
        except Exception as exc:  # pragma: no cover - network/runtime errors
            raise SSHCommandError(f"Remote command failed: {exc}") from exc
        finally:
            client.close()

    def _connect(self) -> paramiko.SSHClient:
        if not self.config.password and not self.config.key:
            raise SSHConnectionError("Either SSH_PASSWORD or SSH_KEY must be provided.")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict[str, object] = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
            "timeout": self.config.timeout_seconds,
            "banner_timeout": self.config.timeout_seconds,
            "auth_timeout": self.config.timeout_seconds,
            "look_for_keys": False,
            "allow_agent": False,
        }

        if self.config.key:
            key = self._load_private_key(self.config.key)
            if key is not None:
                kwargs["pkey"] = key
            else:
                kwargs["key_filename"] = self.config.key

        if self.config.password:
            kwargs["password"] = self.config.password

        try:
            client.connect(**kwargs)
            return client
        except Exception as exc:  # pragma: no cover - network/runtime errors
            client.close()
            raise SSHConnectionError(f"Unable to connect to SSH host: {exc}") from exc

    def _load_private_key(self, key_value: str) -> paramiko.PKey | None:
        if "BEGIN" not in key_value:
            return None

        key_types = (
            paramiko.RSAKey,
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.DSSKey,
        )
        for key_cls in key_types:
            try:
                return key_cls.from_private_key(io.StringIO(key_value))
            except paramiko.SSHException:
                continue
        raise SSHConnectionError("SSH_KEY is present but could not be parsed as a private key.")


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
