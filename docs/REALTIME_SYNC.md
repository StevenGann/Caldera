# Real-time change stream

Caldera exposes a live feed of vault changes so an external client can mirror the
vault in real time. This is the server-side contract the
[Caldera-Sync](https://github.com/StevenGann/Caldera-Sync) Obsidian plugin builds
on, but it's a general-purpose API.

It complements, and does not replace, the [webhook](WEBHOOKS.md): the webhook is an
*outbound* POST that fires only on **external** (git-origin) changes; the change
stream is an *inbound* subscription that fires on **every** change — including
writes made through this server's own API by an agent.

## Concepts

Every note write publishes a **change event** to an in-process bus:

```json
{ "seq": 42, "ts": "2026-06-09T12:00:00+00:00", "type": "upsert",
  "path": "Projects/Caldera.md", "checksum": "sha256:…", "origin": "api" }
```

| field      | meaning |
|------------|---------|
| `seq`      | Monotonic sequence number. The client tracks the last seq it has applied. |
| `type`     | `upsert`, `delete`, or `resync` (a sentinel — reload the manifest). |
| `path`     | Vault-relative note path (`null` for `resync`). |
| `checksum` | New `sha256:` of the raw file for `upsert`; `null` otherwise. **Used for echo suppression** — a client that made the write recognises its own checksum and skips re-applying it. |
| `origin`   | `api` (a write through this server) or `external` (pulled from git origin). |

The bus keeps a bounded ring buffer (`CALDERA_EVENTS_BUFFER_SIZE`, default 1000)
for replay. It is a notification channel, not a durable log: a client that falls
behind the buffer is told to `resync`. Vault durability lives in the git source.

## Endpoints

All require the usual `Authorization: Bearer <key>`.

### `GET /api/v1/manifest[?folder=]`

Full reconciliation snapshot — every note's path + checksum, plus the stream
`head` to subscribe from:

```json
{ "head": 42, "notes": [ { "path": "Index.md", "checksum": "sha256:…" }, … ] }
```

`head` is captured *before* the snapshot, so a change racing the snapshot is
re-delivered over the stream (idempotent) rather than missed.

### `GET /api/v1/events[?since=<seq>]`  — Server-Sent Events

A `text/event-stream`. With `?since=<seq>` it first replays buffered events after
`seq` (or emits a single `resync` event if `since` fell behind the buffer), then
streams live events. Comment lines (`: keepalive`) are sent every
`CALDERA_EVENTS_KEEPALIVE` seconds to hold the connection open through proxies.

```
id: 43
data: {"seq":43,"ts":"…","type":"upsert","path":"Note.md","checksum":"sha256:…","origin":"api"}

: keepalive
```

Because the stream needs both an `Authorization` header and streaming, consume it
with `fetch` + a `ReadableStream` reader rather than the browser `EventSource`
(which can't set headers).

### `GET /api/v1/changes?since=<seq>[&limit=]`  — poll fallback

The SSE-free transport (e.g. Obsidian mobile, where held-open streams are
unreliable). Returns buffered events after `since`:

```json
{ "head": 42, "floor": 11, "resync": false, "events": [ … ] }
```

`resync: true` (with `events: []`) means `since` preceded the buffer — reload the
manifest. `floor` is the oldest replayable seq.

## How a client stays in sync

1. **Bootstrap.** `GET /manifest`. Diff against local state; pull/push differences.
   Remember `head`.
2. **Subscribe.** `GET /events?since=<head>` (or poll `/changes?since=<seq>`).
3. **Apply remote → local.** For each event whose `checksum` differs from the
   local copy: `upsert` → fetch raw and write; `delete` → remove. Skip events whose
   `checksum` matches what the client last wrote/received (echo suppression).
4. **Push local → remote.** On a local edit, `PUT /api/v1/notes/{path}` with
   `If-Match: "<last-known-checksum>"`. On `412`/`409`, refetch and resolve
   (e.g. write a conflict copy).
5. **On `resync`.** Go back to step 1.

### Byte-fidelity (important)

To avoid a sync loop that never settles, write notes **verbatim**: send the entire
file (frontmatter included) in the `content` field and **omit** `frontmatter`.
Caldera stores `content` as-is when no `frontmatter` is supplied, so
`sha256(raw)` matches on both sides. If you split frontmatter out, Caldera
re-serialises the YAML and the checksum drifts, causing perpetual re-sync. Read
notes back with `GET /api/v1/notes/{path}?format=markdown` (the raw file).

## Configuration

| env var | default | meaning |
|---------|---------|---------|
| `CALDERA_EVENTS_BUFFER_SIZE` | `1000` | Recent changes retained for replay. |
| `CALDERA_EVENTS_KEEPALIVE`   | `25`   | Seconds between SSE keepalive comments. |
