# Caldera — Deployment

> How Caldera is built, published, and run on the homelab Kubernetes cluster.
> Builds on [`DESIGN.md`](DESIGN.md) §9 and [`MCP.md`](MCP.md) §9. Manifests live
> in [`deploy/k8s/`](../deploy/k8s/).

## 1. Build & publish (GitHub Actions → GHCR)

- **CI** (`.github/workflows/ci.yml`) runs ruff + pytest (matrix 3.11/3.12) and a
  no-push Docker build on every push/PR.
- **Release** (`.github/workflows/release.yml`) builds the image and pushes it to
  **GHCR** (`ghcr.io/<owner>/caldera`) on a `v*` tag (semver tags) and on `main`
  (the `edge` tag). It authenticates with the workflow's `GITHUB_TOKEN`
  (`packages: write`); no extra secrets needed.

The image installs the `mcp` extra so `/mcp` works out of the box; `git` is
included for the GitHub source.

## 2. The single-writer constraint (read this first)

Each pod holds **its own git working tree**. Two pods committing/pushing
independently would diverge and fight over origin. Therefore the Deployment is
**`replicas: 1`** with **`strategy: Recreate`** (the old pod is torn down before
the new one starts, so the vault is never under two writers). **Do not scale it
up.** Horizontal scale-out (shared storage + a leader-elected writer, or a
read-replica/single-writer split) is deferred design work, not a config tweak.

`Recreate` means a brief full outage during rollouts — acceptable for a
single-tenant homelab tool.

## 3. Storage — two PVCs

| PVC | Mount | Holds | Notes |
|-----|-------|-------|-------|
| `caldera-vault` | `/vault` | the cloned git working tree | persists so a restart re-opens the clone instead of re-cloning |
| `caldera-data` | `/data` | Caldera state **outside** the vault — `vectors.db`, etc. | **never** under `/vault`, so embeddings are never committed/pushed (SEARCH.md §3.3) |

Both are `ReadWriteOnce` (fine for a single pod).

## 4. Configuration & secrets

- **Secrets** (`deploy/k8s/secret.example.yaml` → `secret.yaml`): `CALDERA_API_KEYS`
  (long random Bearer key(s); the REST API and MCP share them) and
  `CALDERA_GITHUB_TOKEN` (PAT, repo scope). Keep the filled-in copy out of git.
- **Env** (in the Deployment): source/repo/branch, paths, sync timings. See the
  full list in [`DESIGN.md`](DESIGN.md) §8 and [`.env.example`](../.env.example).
- **Read-only mode:** set `CALDERA_READ_ONLY=true` to serve a vault without ever
  writing or pushing (the sync loop still pulls).

## 5. Ingress (Streamable HTTP)

`/api/v1` and `/mcp` share one host. The MCP Streamable-HTTP endpoint needs the
ingress to **not buffer** responses and to allow a **long idle timeout** (the
example sets nginx `proxy-buffering: off` and `proxy-read-timeout: 3600`). Adjust
for your controller. Expose only over TLS; auth is a static Bearer key.

## 6. Health & lifecycle

- **`/readyz`** flips ready once the vault is cloned and indexed; the readiness
  probe allows a long window (`failureThreshold`) for a large first clone, and
  reports a `bootstrap_error` if the clone/PAT is bad.
- **`/healthz`** is liveness only.
- **Graceful shutdown:** on SIGTERM Caldera runs a final commit+push drain
  (§7.2.2). `terminationGracePeriodSeconds` (60s in the manifest) must exceed the
  flush+push budget, or the kubelet SIGKILLs mid-drain.

## 7. Durability & recovery

- `GET /api/v1/vault/status` exposes `committed_unpushed`, `state`
  (`idle`/`push_wedged`/`conflict_blocked`/…), and `last_discard`. **Alert on
  `committed_unpushed` climbing or `state=push_wedged`** — it means acknowledged
  writes aren't reaching origin (bad PAT, branch protection, network).
- On an origin-wins reset, discarded local commits are saved to a
  `refs/caldera/discarded/<ts>-<sha7>` ref (best-effort pushed). Recover with
  `git log`/`git cherry-pick` against the ref named in `last_discard.recovery_ref`.
- The vault itself is backed by the GitHub repo — that **is** the backup. `/data`
  (embeddings) is regenerable and need not be backed up.

## 8. Deploy

```bash
kubectl apply -f deploy/k8s/caldera.yaml          # namespace, PVCs, Deployment, Service, Ingress
cp deploy/k8s/secret.example.yaml secret.yaml      # fill in keys/PAT, then:
kubectl -n caldera apply -f secret.yaml
# Set the image to your published tag:
kubectl -n caldera set image deploy/caldera caldera=ghcr.io/<owner>/caldera:vX.Y.Z
kubectl -n caldera rollout status deploy/caldera
```

Upgrades: push a new tag (CI publishes it), then `set image` + `rollout status`.
Because of `Recreate`, expect a short gap while the new pod clones/indexes.
