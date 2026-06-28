---
name: ai-web-ftp
description: Use when an AI Agent needs to read, write, delete, or manage files on a remote server via HTTP. Covers all CRUD operations, permission models, and security constraints.
---

# AI Web FTP — Agent Skill Reference

## Overview

AI Web FTP is an HTTP-based file service designed for LLM agents. All file operations are exposed as simple HTTP requests. Agents that can only send GET requests (no custom headers or body) can still perform full CRUD via query parameters.

## When to Use

- Your agent needs to **read** files or list directory contents from a remote server
- Your agent needs to **write/upload** files to a shared directory
- Your agent needs to **delete** or **rename** files remotely
- Your agent is **limited to GET requests** only (no headers, no body)
- Your agent needs to **check permissions** before attempting an operation

**Do NOT use when:** You need real-time sync, WebDAV compatibility, or multi-user authentication beyond a shared access key.

## Core Pattern

Every request targets the same endpoint `/s/{path}` with an `?key=` access key. The `path` maps to a virtual directory configured on the server.

```
GET /s/{path}?key=xxx              → Read file or list directory
GET /s/{path}?key=xxx&mkdir=1      → Create directory
GET /s/{path}?key=xxx&upload_url=URL → Upload file from URL
GET /s/{path}?key=xxx&content=...  → Upload text content
GET /s/{path}?key=xxx&delete=1     → Delete file or empty directory
GET /s/{path}?key=xxx&rename_to=NEW → Rename
GET /s/{path}?key=xxx&move_to=/DEST → Move to another directory
```

For agents that support other HTTP methods:
```
PUT  /s/{path}?key=xxx             → Upload raw body as file
POST /s/{path}?key=xxx             → Upload multipart form file
POST /s/{path}?key=xxx&mkdir=1     → Create directory
DELETE /s/{path}?key=xxx           → Delete file or empty directory
```

## Quick Reference

### Permissions

Each share has independent boolean permissions. An agent can query its own permissions:

```
GET /perm/{path}?key=xxx
→ {"path": "...", "permissions": {"list": true, "read": true, "write": false, ...}}
```

| Permission | Operation | Effect |
|-----------|-----------|--------|
| `list` | GET directory | Returns directory listing |
| `read` | GET file | Returns file content or download |
| `write` | POST/PUT mkdir/upload | Creates files and directories |
| `delete` | GET/DELETE with delete=1 | Removes files or empty dirs |
| `rename` | GET with rename_to/move_to | Renames or moves items |

### Common Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | string | Access key for the target share (required) |
| `json` | int (0/1) | Return JSON instead of HTML |
| `raw` | int (0/1) | Return raw file content as plain text |
| `download` | int (0/1) | Force file download |
| `help` | string | Attach documentation: basic, full, md |

### Security Constraints

| Constraint | Limit |
|-----------|-------|
| Max upload size | 500 MB |
| Max content= param | 64 KB |
| upload_url protocol | HTTPS only |
| Upload URL destination | Public internet only (no private IPs) |
| Write rate limit | 60 operations/minute |
| Path traversal | Blocked by realpath + normcase check |

## Usage Patterns

### Pattern 1: Read a file (GET-only agent)

```
GET /s/data/readme.txt?key=abc123
```
Returns HTML with file preview. Add `?json=1` for JSON:
```
GET /s/data/readme.txt?key=abc123&json=1
→ {"type": "file", "filename": "readme.txt", "size": 1024, ...}
```

### Pattern 2: Upload from a URL (GET-only agent)

```
GET /s/data/uploads?key=abc123&upload_url=https://example.com/file.zip
```
The server downloads the file from the URL and saves it to the target path.

### Pattern 3: Upload text content (GET-only agent)

```
GET /s/data/notes/hello.txt?key=abc123&content=Hello+World
```
Creates `hello.txt` with content "Hello World". Max 64KB.

### Pattern 4: Upload binary (agent with PUT support)

```
PUT /s/data/image.jpg?key=abc123
Content-Type: application/octet-stream

<binary data>
```

### Pattern 5: Create directory + upload file

```
GET /s/data/newproject?key=abc123&mkdir=1
GET /s/data/newproject/main.py?key=abc123&content=print(%22hello%22)
```

## Admin Endpoints

| Endpoint | Description | Auth |
|----------|-------------|------|
| `GET /admin` | Admin dashboard | Cookie auth |
| `POST /admin/login` | Login with admin key | Form |
| `GET /admin/stats` | Server statistics (JSON) | Cookie auth |
| `GET /admin/logs?action=&path=&ip=&keyword=&date_from=&date_to=` | Filterable access logs | Cookie auth |
| `GET /health` | Health check | None |

## Error Handling

All errors return appropriate HTTP status codes:
- `400` — Bad request (invalid params, filename, etc.)
- `403` — Access denied (wrong key, permission denied)
- `404` — Share or path not found
- `409` — Conflict (target exists, directory not empty)
- `413` — Payload too large
- `429` — Rate limited
- `500` — Server error
- `507` — Insufficient storage

With `?json=1`, errors return `{"success": false, "error": "...", "code": N}`.

## Common Mistakes

1. **Missing trailing slash for directory root** — `/s/test?key=abc` works, but `/s/test/?key=abc` is also valid
2. **Cross-share move** — Moving files between different shares is rejected
3. **Deleting non-empty directory** — Must delete contents first (or use recursive delete)
4. **Overwriting existing target on rename** — Rename/move fails with 409 if target exists
5. **Request logging** — Admin GET page views are not logged (only login/logout/config changes)
