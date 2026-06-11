"""Resolve the true Obsidian vault root inside the source working tree.

The working-tree root (where ``.git`` lives) is not always the Obsidian vault
root — a repo may keep the vault in a subdirectory (e.g. ``Vault/`` with a README
or CI config beside it). If Caldera indexes from the working-tree root in that
case, every note path is prefixed with the subdir name (``Vault/Note.md``); a
sync client rooted at the real Obsidian vault then writes those back one level
too deep (``Vault/Vault/Note.md``) — a path-nesting loop.

We locate the vault deterministically:
  1. the shallowest ``.obsidian`` marker directory → its parent is the root;
  2. otherwise a ``caldera.json`` at the working-tree root naming the subdir;
  3. otherwise the working-tree root itself (unchanged legacy behavior).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("caldera.layout")

CONFIG_NAME = "caldera.json"


def find_obsidian_dirs(working_tree: Path) -> list[Path]:
    """All ``.obsidian`` directories under the tree (excluding ``.git``),
    shallowest first so the canonical vault root sorts to the front."""
    found = [
        p for p in working_tree.rglob(".obsidian")
        if p.is_dir() and ".git" not in p.parts
    ]
    found.sort(key=lambda p: len(p.relative_to(working_tree).parts))
    return found


def resolve_vault_root(working_tree: str | Path) -> Path:
    """Return the directory Caldera should treat as the vault root."""
    wt = Path(working_tree).resolve()

    # 1. .obsidian marker (the Obsidian vault root is its parent).
    obsidian = find_obsidian_dirs(wt)
    if obsidian:
        root = obsidian[0].parent
        if len(obsidian) > 1:
            # Extra .obsidian dirs usually mean a nested-vault accident (the very
            # bug this guards against). Use the shallowest and flag the rest.
            others = [str(p.parent.relative_to(wt)) for p in obsidian[1:]]
            logger.warning(
                "found %d .obsidian directories; using shallowest vault root %r. "
                "Nested vaults cause path duplication — ignoring: %s",
                len(obsidian), str(root.relative_to(wt)) or ".", others,
            )
        else:
            logger.info("vault root detected via .obsidian: %s", root)
        return root

    # 2. caldera.json override at the working-tree root.
    cfg = wt / CONFIG_NAME
    if cfg.is_file():
        root = _root_from_config(wt, cfg)
        if root is not None:
            logger.info("vault root from %s: %s", CONFIG_NAME, root)
            return root

    # 3. Fall back to the working tree itself.
    logger.info("no .obsidian or %s; vault root is the working tree: %s", CONFIG_NAME, wt)
    return wt


def _root_from_config(wt: Path, cfg: Path) -> Path | None:
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable %s: %s", CONFIG_NAME, exc)
        return None
    sub = data.get("vault_root") or data.get("root")
    if not isinstance(sub, str) or not sub.strip():
        return None
    root = (wt / sub).resolve()
    # Refuse a configured root that escapes the working tree.
    if root != wt and wt not in root.parents:
        logger.warning("%s vault_root %r escapes the working tree; ignoring", CONFIG_NAME, sub)
        return None
    if not root.is_dir():
        logger.warning("%s vault_root %r is not a directory; ignoring", CONFIG_NAME, sub)
        return None
    return root
