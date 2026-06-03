# Caldera — Webhooks

Caldera can **POST a notification to an external URL when the vault changes from
an external source** — e.g. a human edits the vault in Obsidian and pushes, and
Caldera pulls those changes on its next reconcile. This lets an agent (such as
Hermes) know to re-read affected notes instead of polling.

## What triggers it

A webhook fires **only for external changes** brought in by a pull/reconcile from
origin — never for the agent's own writes through the API/MCP. Caldera guarantees
this by diffing the in-memory index **after committing local writes but before the
pull**, against the state **after** the pull: the agent's own edits are already in
the "before" snapshot, so only origin's changes remain. Push-echo (Caldera pulling
back a commit it just pushed) is suppressed the same way.

Concretely, it fires when a reconcile results in a fast-forward, a clean merge, or
an origin-wins reset that changes one or more notes.

## Configuration

| Env var | Meaning |
|---------|---------|
| `CALDERA_WEBHOOK_URL` | Where to POST the event. Unset → webhooks disabled. |
| `CALDERA_WEBHOOK_SECRET` | Optional HMAC-SHA256 signing key (strongly recommended). |
| `CALDERA_WEBHOOK_TIMEOUT` | Per-request timeout in seconds (default `10`). |

## Request

`POST <CALDERA_WEBHOOK_URL>` with `Content-Type: application/json`.

Headers:

| Header | Value |
|--------|-------|
| `X-Caldera-Event` | `vault.updated` |
| `X-Caldera-Signature` | `sha256=<hex>` — HMAC-SHA256 of the **raw body** with the secret (present only if a secret is set) |

Body:

```json
{
  "event": "vault.updated",
  "source": "external",
  "at": "2026-06-03T01:43:03.536767+00:00",
  "added":    ["Inbox/2026-06-03.md"],
  "removed":  ["Archive/Old.md"],
  "modified": ["Index.md"],
  "counts": { "added": 1, "removed": 1, "modified": 1 }
}
```

The payload carries **note paths only**, not content — fetch what you need via
`GET /api/v1/notes/{path}` (the agent's API key works for both). Paths are
vault-relative, canonical (NFC, `.md`).

## Verifying the signature (receiver side)

Compute HMAC-SHA256 over the exact raw request body and compare in constant time:

```python
import hmac, hashlib

def verify(raw_body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")
```

> Hash the **raw bytes** you received, before any JSON re-serialization — a
> re-encoded body will not match.

## Delivery semantics

- **Best-effort, non-blocking.** Delivery runs as a background task; a slow or
  down receiver never blocks Caldera's sync loop.
- **Retries.** Up to 3 attempts with exponential backoff (1s, 2s) on a network
  error or an HTTP ≥ 400 response. After that the failure is logged at ERROR and
  the event is dropped (no durable queue — treat the webhook as a *hint to
  re-read*, with the periodic poll as the backstop).
- **No ordering/exactly-once guarantees.** Coalesced bursts may arrive as one
  event covering several paths. Design the receiver to be idempotent (re-read the
  listed paths).

## Example: wiring an agent

1. Stand up an HTTP endpoint that accepts `POST` and verifies the signature.
2. Set `CALDERA_WEBHOOK_URL` (and `CALDERA_WEBHOOK_SECRET`) on the Caldera
   container.
3. On each event, re-read the `added`/`modified` notes and forget the `removed`
   ones.

See also: the change-detection mechanism is part of the sync engine
([`ARCHITECTURE.md`](ARCHITECTURE.md) §4), and the per-note read API is in
[`API.md`](API.md) §4.
