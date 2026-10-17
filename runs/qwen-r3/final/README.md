# Livedoc

A real-time collaborative plain-text editing service. Multiple users can edit the same document simultaneously, with every participant seeing the same text at all times.

## Quick Start

### Server

```bash
python3 server.py --port 8765 --data-dir livedoc_data
```

- Listens on `127.0.0.1:PORT`
- Persists data to `DIR` (creates it if missing)
- Runs until killed (`SIGTERM` or `SIGINT`)

### Client (Python)

```python
from livedoc_client import Client

client = Client("127.0.0.1", 8765, "mydoc", "alice")

# Edit the document
client.insert(0, "Hello, ")
client.insert(7, "world!")
client.wait_synced()

# See the current text
print(client.text)  # "Hello, world!"

# See other users' cursors
print(client.cursors)  # {"bob": 5, ...}

# Set your cursor
client.set_cursor(10)

# Query all cursors
cursors = client.query_cursors()

# Close when done
client.close()
```

### Running Tests

```bash
pytest tests/ -q
```

All tests start their own servers on free ports and clean up after themselves.

## Data Model

- **Documents** are identified by string names (up to 100 characters). Created empty at revision 0.
- **Text** is a Python `str` (Unicode code points).
- **Revision** is the count of committed operations. Each operation increments it by 1.
- **Operations** are lists of edits (`ins` or `del`), applied left to right.

## Wire Protocol

Messages are JSON objects on single lines, UTF-8 encoded, terminated by `\n`.

### Messages

| Type | Direction | Description |
|---|---|---|
| `join` | Client → Server | Join a document. Optional `since` for delta sync. |
| `joined` | Server → Client | Document state (full text or ops since `since`). |
| `op` | Client → Server | Submit an edit operation. |
| `ack` | Server → Client | Acknowledge an operation. |
| `op` | Server → Client | Broadcast of a remote operation. |
| `cursor` | Client → Server | Set a cursor position. |
| `cursor` | Server → Client | Broadcast of a remote cursor. |
| `cursors` | Client → Server | Query all cursors. |
| `cursors` | Server → Client | Reply with all cursor positions. |
| `snapshot` | Client → Server | Get document text at a specific revision. |
| `snapshot` | Server → Client | Reply with text at the requested revision. |
| `leave` | Server → Client | A client has disconnected. |
| `error` | Server → Client | Error response. |

### Errors

| Code | When |
|---|---|
| `bad_json` | Not a JSON object. |
| `bad_request` | Unknown type, missing/wrong fields. |
| `not_joined` | Operation on unjoined document. |
| `bad_rev` | Revision is negative or too large. |
| `out_of_range` | Edit position outside text bounds. |
| `not_found` | Document does not exist (snapshots). |

## Features

### Collaborative Editing

- **Optimistic UI**: Edits are applied locally before server acknowledgment.
- **One operation in flight**: Edits made while one is pending are buffered.
- **OT-based consistency**: Operations are transformed against concurrent edits to ensure all clients converge to the same text.

### Live Cursors (Presence)

- Set your cursor position to let others see where you are.
- Cursors are automatically moved forward when text changes around them.
- Your own typing pushes your cursor forward; others' typing at your cursor position does not.
- `leave` messages notify when a user disconnects.

### Document History

- Retrieve the text of a document at any past revision.
- Does not require joining the document.
- Works after server restarts (persisted in `state.json`).
- Efficient: snapshots of 5,000 revisions complete in well under a second.

## Design

### Transformation Engine

The server uses Operational Transformation (OT) to handle concurrent edits. When an operation arrives with a base revision older than the current one, it is transformed against every operation committed in between. The transformation rules are edit-vs-edit, composed for multi-edit operations:

- **ins vs ins**: Earlier insert stays left; later insert shifts right by the earlier's text length.
- **ins vs del**: Insert before delete stays; inside moves to delete start; after shifts left.
- **del vs ins**: Delete after insert shifts right; before stays; inside splits to avoid deleting inserted text.
- **del vs del**: Overlapping deletes shrink to avoid deleting already-deleted characters.

### Persistence

State (documents, history, seen operations) is saved to `state.json` in the data directory after each acknowledged operation. On restart, the server loads this file to restore all documents and the exactly-once deduplication set. Cursors are not persisted.

### Concurrency

- Each connection is handled in its own thread.
- A global lock protects server state mutations.
- The client uses a background thread for receiving messages and a condition variable for synchronization.
- The client's `text` and `rev` are protected by a lock for thread safety.

### Cursor Movement

When text changes, stored cursor positions are moved through each edit:

- **Insert at `p` of length `k`**: If `p < cursor` (or `p == cursor` and the cursor's owner made the edit), cursor becomes `cursor + k`.
- **Delete at `p` of length `n`**: If `cursor > p`, cursor becomes `max(p, cursor - n)`.
