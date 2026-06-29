# AI Web FTP

HTTP-based file service designed for AI Agents. All CRUD operations are accessible via simple HTTP requests — even agents limited to **GET-only** (no custom headers, no body) can read, write, delete, and manage files remotely.

## Features

- **Full CRUD via GET** — Create directories, upload files (from URL or inline content), delete, rename, and move — all through query parameters
- **Standard HTTP methods** — PUT/POST/DELETE also supported for agents that can send them
- **Per-share permission model** — Independent `list`/`read`/`write`/`delete`/`rename` permissions per virtual path
- **Security first** — SSRF protection, path traversal prevention, rate limiting, filename sanitization, atomic writes
- **JSON output mode** — `?json=1` returns machine-readable responses
- **Structured logging** — JSONL format with auto-sharding (10 MB per file) and total volume cap (500 MB)
- **Admin dashboard** — Web UI for managing shares, viewing logs with search/filter, and server stats
- **CORS enabled** — Can be accessed from any origin

## Quick Start

```bash
# Install dependencies
uv sync

# Configure shares (edit config.json)
# Start the server
uv run python main.py
```

The server starts at `http://localhost:8000`. Open `/admin` to manage shares.

## Configuration

All settings are managed via [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) in `config.py`. Override any value with an environment variable:

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_KEY` | `admin123` | Admin panel password |
| `SECRET_KEY` | `supersecretkey` | JWT signing key |
| `CONFIG_FILE` | `config.json` | Shares configuration file |
| `LISTEN_HOST` | `0.0.0.0` | Server bind address |
| `LISTEN_PORT` | `8000` | Server port |

### Share Configuration (`config.json`)

```json
[
  {
    "id": "mydata",
    "name": "My Data",
    "virtual_path": "/data",
    "real_path": "/path/on/server",
    "permissions": {
      "list": true,
      "read": true,
      "write": false,
      "delete": false,
      "rename": false
    },
    "access_key": "your-secret-key"
  }
]
```

Each share maps a `virtual_path` (used in URLs) to a `real_path` (actual filesystem directory). Multiple shares can be configured.

## Usage

### Core Endpoint

```
GET /s/{path}?key={access_key}
```

Lists a directory or displays a file based on whether `path` resolves to a file or directory.

### Read Operations

```bash
# List directory contents
curl "http://localhost:8000/s/data?key=abc123"

# List directory including hidden files (dot-files)
curl "http://localhost:8000/s/data?key=abc123&all=1"

# View file content (HTML preview)
curl "http://localhost:8000/s/data/readme.txt?key=abc123"

# Raw file content
curl "http://localhost:8000/s/data/readme.txt?key=abc123&raw=1"

# Download file
curl "http://localhost:8000/s/data/file.zip?key=abc123&download=1"

# JSON output
curl "http://localhost:8000/s/data?key=abc123&json=1"
```

### Write Operations

```bash
# Create directory
curl "http://localhost:8000/s/data/newdir?key=abc123&mkdir=1"

# Upload from URL (GET-only agent)
curl "http://localhost:8000/s/data/uploads?key=abc123&upload_url=https://example.com/file.zip"
# Override filename
curl "http://localhost:8000/s/data/uploads?key=abc123&upload_url=https://example.com/download&filename=out.zip"

# Upload text content (GET-only agent, max 64KB)
curl "http://localhost:8000/s/data/note.txt?key=abc123&content=Hello+World"

# Upload via PUT (raw body)
curl -X PUT "http://localhost:8000/s/data/image.jpg?key=abc123" --data-binary @image.jpg

# Upload via POST (multipart form)
curl -X POST "http://localhost:8000/s/data" -F "file=@document.pdf" -F "key=abc123"
```

### Delete & Rename

```bash
# Delete file or empty directory
curl "http://localhost:8000/s/data/old.txt?key=abc123&delete=1"

# Rename
curl "http://localhost:8000/s/data/old.txt?key=abc123&rename_to=new.txt"

# Move to another path (same share)
curl "http://localhost:8000/s/data/old.txt?key=abc123&move_to=/data/archive/old.txt"
```

> **Note:** Only one operation per request. Conflicting parameters return `400 Bad Request`.

### Check Permissions

```bash
curl "http://localhost:8000/perm/data?key=abc123"
# → {"path": "/data", "share_name": "My Data", "permissions": {...}}
```

### Help

```bash
# HTML help pages
curl "http://localhost:8000/help?level=basic"
curl "http://localhost:8000/help?level=full"

# Markdown help (for LLM agents)
curl "http://localhost:8000/help?format=md"
```

## Permission Model

Each share has five independent boolean permissions:

| Permission | Effect |
|-----------|--------|
| `list` | Can list directory contents |
| `read` | Can view/download files |
| `write` | Can upload files and create directories |
| `delete` | Can delete files and empty directories |
| `rename` | Can rename/move files and directories |

## Security

- **SSRF protection** — `upload_url` only accepts HTTPS; private/internal IPs are blocked
- **Path traversal prevention** — All paths are resolved via `os.path.realpath()` with boundary checks
- **Rate limiting** — Write operations limited to 60/minute per key (configurable)
- **File size limits** — 500 MB per upload, 64 KB for inline content uploads
- **Filename sanitization** — Dangerous characters stripped, path separators rejected, max 255 chars, names ending with a dot rejected
- **Atomic writes** — Files are written to a `.tmp` path then atomically renamed
- **Audit logging** — All access denials and operations are logged with masked keys

## Logging

Logs are stored in `logs/` as JSONL files:

- Each log entry is a JSON object on one line
- Files auto-split at 10 MB (`log_max_size`)
- Total log volume capped at 500 MB (`log_max_total_size`), oldest shards deleted first
- Logs older than 90 days (`log_max_age_days`) are automatically cleaned up
- Admin GET page views are not logged (only login, logout, and config changes)

## Admin Endpoints

| Endpoint | Description |
|----------|-------------|
| `/admin` | Web management dashboard |
| `/admin/logs?action=&path=&ip=&keyword=&date_from=&date_to=` | Filterable access log viewer |
| `/admin/stats` | Server statistics (JSON) |
| `/health` | Health check |

## Architecture

```
config.py     → pydantic-settings configuration
utils.py      → Data models, security tools, file operation handlers, logging
main.py       → FastAPI app, middleware, routes
HELP.md       → LLM Agent skill reference (markdown)
templates/    → Jinja2 HTML templates
logs/         → Auto-sharded JSONL log files
```

## Development

```bash
uv sync                    # Install dependencies
uv run python main.py      # Start dev server (auto-reload on)
uv run python -c "import py_compile; py_compile.compile('main.py', doraise=True); print('OK')"  # Syntax check
```
