---
name: ai-web-ftp
description: Use when an AI agent needs to read, write, delete, or manage files on a remote server via HTTP.
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

**Important:** Only one operation per request. Conflicting parameters (`mkdir` + `delete`, etc.) return 400.

**Do NOT use when:** You need real-time sync, WebDAV compatibility, or multi-user authentication beyond a shared access key.

**⚠️ Required discipline:** Always follow the [Agent Workflow](#agent-workflow) — **list → read key files → check permissions → then act**. Never write or delete without first listing the directory and understanding what exists.

## Agent Workflow

**Always follow this sequence. Do not skip steps.**

```
1. LIST →   GET /s/root?key=xxx&all=1     — Understand directory structure (include hidden files)
2. READ →   GET /s/root/file?key=xxx      — Read key files to understand context
3. CHECK →  GET /perm/root?key=xxx        — Verify you have the required permissions
4. DECIDE → Analyze the goal: modify existing files or create new ones?
5. ACT  →   Write / edit / delete (never before steps 1-4 are complete)
```

**Why this order?**
- Writing without listing first risks overwriting existing files or creating duplicates
- Editing without reading first may break existing content or introduce incompatible changes
- Acting without checking permissions wastes a request on a 403 error

**Golden rule: read at least one existing file before deciding what to write.** If the task involves modifying a project, you must read at least one relevant source file first.

### Example flow

```
# 1. List the directory
GET /s/project?key=xxx&all=1

# 2. Read key files to understand format and content
GET /s/project/README.md?key=xxx
GET /s/project/src/main.py?key=xxx

# 3. Check permissions
GET /s/project?key=xxx&json=1  (check response for write permission)

# 4. Now perform the operation
GET /s/project/src/main.py?key=xxx&content=... 
```

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
GET /tool/edit?path=...&key=xxx&old_str=...&new_str=... → Edit text file (replace string)
```

For agents that support other HTTP methods:
```
PUT  /s/{path}?key=xxx             → Upload raw body as file
POST /s/{path}?key=xxx             → Upload multipart form file
POST /s/{path}?key=xxx&mkdir=1     → Create directory
DELETE /s/{path}?key=xxx           → Delete file or empty directory
```

Specialized tools under `/tool/`:
```
GET /tool/edit?path=...&key=xxx&old_str=...&new_str=...    → Edit text file (exact string replacement)
GET /tool/edit?path=...&key=xxx&old_str=...&new_str=...&replace_all=1  → Replace all occurrences
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
| `all` | int (0/1) | Show hidden files (dot-files) in directory listing |
| `help` | string | Attach documentation: basic, full, md |
| `filename` | string | Override filename for upload (upload_url/content/PUT/POST) |
| `path` | string | Target file path (used by `/tool/edit`) |
| `old_str` | string | String to find (used by `/tool/edit`) |
| `new_str` | string | Replacement string (used by `/tool/edit`) |
| `replace_all` | int (0/1) | Replace all occurrences (used by `/tool/edit`) |

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

### Pattern 0: List directory contents (always start here)

```
GET /s/data?key=abc123&all=1
```
- Returns all entries (files/subdirectories) in the directory; `&all=1` includes hidden files (dot-files)
- Always inspect the structure before deciding which files to access — prevents blind operations
- Recurse into subdirectories until you find the target file

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
The server downloads the file from the URL and saves it. Use `&filename=out.zip` to override the saved filename:
```
GET /s/data/uploads?key=abc123&upload_url=https://example.com/download&filename=package.zip
```

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

### Pattern 6: Edit text file (precision string replacement)

```
GET /tool/edit?path=/data/config.json&key=abc123&old_str=debug%3Atrue&new_str=debug%3Afalse
```
Replaces the first occurrence of `debug:true` with `debug:false` in `config.json`. Add `&replace_all=1` to replace every occurrence.

```
GET /tool/edit?path=/data/README.md&key=abc123&old_str=v1.0&new_str=v2.0&replace_all=1
```

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

1. **Writing before reading** — Never create/edit/delete files before listing the directory and reading at least one existing file. Blind writes cause duplication, overwrites, and broken projects. Always follow the [Agent Workflow](#agent-workflow).
2. **Conflicting parameters** — Only one operation per request. `mkdir=1&delete=1` returns 400.
3. **Missing trailing slash for directory root** — `/s/test?key=abc` works, but `/s/test/?key=abc` is also valid
4. **Cross-share move** — Moving files between different shares is rejected
5. **Deleting non-empty directory** — Must delete contents first (or use recursive delete)
6. **Overwriting existing target on rename** — Rename/move fails with 409 if target exists
