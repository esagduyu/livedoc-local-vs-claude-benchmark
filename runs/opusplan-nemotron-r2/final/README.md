# Livedoc: Collaborative Plain-Text Editing with OT

## Overview

Livedoc is a real-time collaborative plain-text editing service that uses Operational Transformation (OT) to ensure all participants end up with exactly the same text, no matter how their edits interleave.

## Deliverables

- `server.py` - The asyncio-based OT server
- `livedoc_client.py` - The Python client library
- `tests/` - pytest tests
- `README.md` - Documentation

## How to Run the Server

```bash
python3 server.py --port PORT --data-dir DIR
```

- Listens on `127.0.0.1:PORT`
- Stores persistent data under `DIR` (created if missing)
- Runs until killed with Ctrl+C

## Client Library

The `Client` class in `livedoc_client.py` provides:

```python
from livedoc_client import Client

client = Client("127.0.0.1", port, "doc_name", "my_name")
# Connects, joins the doc, and returns once joined
# Raises TimeoutError if not joined within timeout

client.text      # Local text state
client.rev       # Last server revision incorporated
client.insert(pos, text)  # Insert text optimistically, send to server
client.delete(pos, length)  # Delete characters optimistically, send to server
client.wait_synced(timeout)  # Block until all local edits acknowledged
client.close()  # Close connection
```

## Transformation (OT Core)

The OT transformation rules are implemented in `livedoc_client.py`:

- `validate_op(op, length)` - validates operation structure and range
- `apply_op(text, op)` - applies list of edits to text left-to-right
- `xf_edit(a, b)` - transforms two edits against each other (all spec cases)
- `xf(A, B)` - recursive transform of operation A against earlier operation B
- `transform_against_history(op, ops)` - folds xf over committed ops in order

## Wire Protocol

TCP connection. Each message is one JSON object on a single line, UTF-8 encoded and terminated by `\n`.

### Messages

- **join** - `{"type": "join", "doc": D, "client": NAME}` + optional `"since": R`
- **op** - `{"type": "op", "doc": D, "rev": R, "id": ID, "op": OP}`
- **ack** - `{"type": "ack", "doc": D, "id": ID, "rev": N}`
- **error** - `{"type": "error", "code": CODE, "message": "..."}`
- **joined** - `{"type": "joined", "doc": D, "rev": N, "text": T}` or with `"ops"` instead of `"text"`
- **cursors** - `{"type": "cursors", "doc": D, "rev": N, "cursors": {NAME: POS}}`
- **cursor** - `{"type": "cursor", "doc": D, "rev": R, "pos": P}`
- **snapshots** - `{"type": "snapshot", "doc": D, "rev": R}`
- **leave** - `{"type": "leave", "doc": D, "client": NAME}`

### Error Codes

- `bad_json` - line is not a JSON object
- `bad_request` - unknown type, missing/wrong fields, empty/doc > 100 chars
- `not_joined` - op for a doc this connection hasn't joined
- `bad_rev` - rev or since negative or > current revision
- `out_of_range` - operation outside text range

## Design Summary

- **Single-threaded event loop** - All commits and broadcasts are done on the event loop thread, ensuring revision order and proper ack sequencing
- **Append-only JSONL logs** - Each document has a persistent JSON log file for durability across restarts
- **Dedup map** - `(client, id) -> rev` map prevents re-committing duplicate operations
- **Client state machine** - One operation in flight at a time, with buffered edits sent after ack
