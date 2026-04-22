# Samba Open Files Monitor

Web application for monitoring open Samba (SMB) files on a remote Linux server.

## Features

- Collects open files over SSH using:
  - `sudo smbstatus --json` (priority)
  - `sudo smbstatus` (fallback parser)
  - `lsof -nP -c smbd -F pLfn` (final fallback)
- Returns structured data from `GET /files`:
  - `filename`
  - `filepath`
  - `pid`
  - `user`
  - `opened_at` (when available)
- Safely closes Samba connections via `POST /close/{pid}` using:
  - `sudo smbcontrol <PID> close-share <sharename>`
  - share name is resolved automatically from `sudo smbstatus --json` (`tcons.service`)
- Protects access with HTTP Basic Auth (`APP_USERNAME` / `APP_PASSWORD`).
- Caches file list for `CACHE_TTL_SECONDS` (default: 7).
- Includes optimized frontend rendering for large lists, live local text filtering, refresh button, and confirmation before closing a connection.

## Project Structure

```text
project/
  backend/
    __init__.py
    main.py
    ssh_client.py
    parser.py
    requirements.txt
  frontend/
    index.html
    script.js
    style.css
  Dockerfile
  docker-compose.yml
  README.md
```

## Run with Docker

1. Create `.env` next to `docker-compose.yml`:

```env
SSH_HOST=192.168.1.50
SSH_PORT=22
SSH_USER=smbmonitor
SSH_PASSWORD=strong_password
# Alternative to password:
# SSH_KEY=/run/secrets/id_rsa
# or full private key value:
# SSH_KEY=-----BEGIN OPENSSH PRIVATE KEY-----...

APP_USERNAME=admin
APP_PASSWORD=super_secret
APP_ROOT_URI=/smbmonitor

CACHE_TTL_SECONDS=7
SSH_TIMEOUT_SECONDS=10
LOG_LEVEL=INFO
```

2. Start:

```bash
docker-compose up --build
```

3. Open:

- `http://localhost:8000` (when `APP_ROOT_URI` is empty)
- `http://localhost:8000/smbmonitor` (when `APP_ROOT_URI=/smbmonitor`)

## Remote Server Sudo Requirement

The SSH user must be allowed to run `smbstatus` and `smbcontrol` without password, for example:

```sudoers
smbmonitor ALL=(ALL) NOPASSWD: /usr/bin/smbstatus, /usr/bin/smbcontrol
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SSH_HOST` | yes | Remote Linux host/IP |
| `SSH_PORT` | no | SSH port (default `22`) |
| `SSH_USER` | yes | SSH username |
| `SSH_PASSWORD` | yes* | SSH password |
| `SSH_KEY` | yes* | SSH private key path or key content |
| `SSH_TIMEOUT_SECONDS` | no | SSH connect/command timeout |
| `APP_USERNAME` | yes | HTTP Basic Auth username |
| `APP_PASSWORD` | yes | HTTP Basic Auth password |
| `APP_ROOT_URI` | no | Base URI prefix for UI/API (example: `/smbmonitor`) |
| `CACHE_TTL_SECONDS` | no | Open files cache TTL in seconds |
| `LOG_LEVEL` | no | Logging level |

\* Provide either `SSH_PASSWORD` or `SSH_KEY`.

## API

### `GET /files`

Query params:

- `search` - plain text search by file name/path (substring match, case-insensitive).
- `refresh` - `true` to bypass cache.

Example:

```bash
curl -u admin:super_secret "http://localhost:8000/smbmonitor/files?search=report"
```

### `POST /close/{pid}`

Flow:

1. Validates PID format.
2. Checks PID exists with `ps -p <PID>`.
3. Checks process name with `ps -p <PID> -o comm=`.
4. Allows close operation only if process name is exactly `smbd`.
5. Resolves share name(s) for this PID from `smbstatus --json` (`tcons`).
6. Executes `sudo smbcontrol <PID> close-share <sharename>` for each resolved share.

Example:

```bash
curl -u admin:super_secret -X POST "http://localhost:8000/smbmonitor/close/12345"
```

Success response:

```json
{
  "status": "success",
  "message": "Samba connection for PID 12345 was closed.",
  "pid": 12345
}
```

## Security Notes

- Command execution is restricted to a fixed whitelist (`sudo smbstatus`, `lsof`, `ps`, `sudo smbcontrol`).
- PID must be a positive integer.
- Close action is denied if PID is not an `smbd` process.
- SSH errors are returned with clear API messages.
- Secrets are configured via environment variables.
