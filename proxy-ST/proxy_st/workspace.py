import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .config import (
    WORKSPACE_EXCLUDE_NAMES,
    WORKSPACE_EXPLORER_ENABLED,
    WORKSPACE_HOST_ROOT,
    WORKSPACE_MAX_DEPTH,
    WORKSPACE_MAX_ENTRIES,
    WORKSPACE_PREVIEW_MAX_BYTES,
    WORKSPACE_ROOT,
)
from .state import SESSION_INFOS


TEXT_EXTENSIONS = {
    ".bat",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".diff",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".patch",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
IMAGE_MIME_PREFIX = "image/"


def _modified_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _resolve_lenient(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return path.expanduser()


def _map_host_path_to_workspace(path: Path) -> Path:
    if not path.is_absolute() or not WORKSPACE_ROOT or not WORKSPACE_HOST_ROOT:
        return path

    try:
        relative = _resolve_lenient(path).relative_to(_resolve_lenient(WORKSPACE_HOST_ROOT))
    except ValueError:
        return path
    return WORKSPACE_ROOT / relative


def _session_cwd(session_id: str) -> Path | None:
    info = SESSION_INFOS.get(session_id) or {}
    hermes_info = info.get("hermes") if isinstance(info.get("hermes"), dict) else {}
    cwd = hermes_info.get("cwd") or info.get("cwd")
    if not cwd:
        return None
    return _map_host_path_to_workspace(Path(str(cwd)).expanduser())


def _resolve_dir(path: Path | None) -> Path | None:
    if not path:
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _session_root_for_session(session_id: str) -> Path | None:
    return _resolve_dir(_session_cwd(session_id))


def workspace_root_for_session(session_id: str) -> Path:
    if not WORKSPACE_EXPLORER_ENABLED:
        raise HTTPException(status_code=404, detail="Workspace explorer is disabled")

    configured_root = _resolve_dir(WORKSPACE_ROOT)
    if configured_root:
        return configured_root

    session_root = _session_root_for_session(session_id)
    if session_root:
        return session_root

    raise HTTPException(
        status_code=404,
        detail="No accessible workspace root found for this session",
    )


def workspace_cwd_for_selection(relative_path: str | None) -> str | None:
    """Resolve a browser-selected relative workspace path to the Hermes host cwd.

    Selection is deliberately confined to the configured WORKSPACE_ROOT. When the
    proxy sees a distinct host mount (WORKSPACE_HOST_ROOT), map the validated
    relative path back to the host path Hermes can mount into its Docker sandbox.
    """
    # No browser override means the configured workspace root itself. This is
    # important when WORKSPACE_HOST_ROOT is "/": the default chat workspace
    # must still be sent explicitly to Hermes so its Docker backend mounts it.
    if relative_path is None:
        relative_path = ""
    if not WORKSPACE_ROOT:
        if not relative_path.strip():
            # The public text-only installation has no workspace mount. An
            # absent selection must therefore remain a no-CWD session; only an
            # explicit selection requires a configured, confined workspace.
            return None
        raise HTTPException(status_code=400, detail="Workspace selection requires WORKSPACE_ROOT")
    root, selected = resolve_workspace_path("__workspace_selector__", relative_path)
    if not selected.is_dir():
        raise HTTPException(status_code=400, detail="Selected workspace path is not a directory")
    if WORKSPACE_HOST_ROOT:
        try:
            relative = selected.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Selected path is outside the workspace root") from exc
        return str((_resolve_lenient(WORKSPACE_HOST_ROOT) / relative))
    return str(selected)


def resolve_workspace_path(session_id: str, relative_path: str | None = "") -> tuple[Path, Path]:
    root = workspace_root_for_session(session_id)
    # Resolve root once so symlinked components in root's own path are
    # canonical. Without this, root /a/link/b and target /a/real/b
    # might not share a prefix after individual resolve() calls.
    root = root.resolve()
    requested_raw = str(relative_path or "").strip()
    try:
        requested_path = Path(requested_raw).expanduser()
        if requested_path.is_absolute():
            target = _map_host_path_to_workspace(requested_path).resolve()
        else:
            requested = requested_raw.lstrip("/\\")
            target = (root / requested).resolve()
            configured_root = _resolve_dir(WORKSPACE_ROOT)
            if configured_root and not target.exists():
                alternate = (configured_root / requested).resolve()
                if _is_within(alternate, root):
                    target = alternate
            session_root = _session_root_for_session(session_id)
            if session_root and not target.exists():
                alternate = (session_root / requested).resolve()
                if _is_within(alternate, root):
                    target = alternate
    except OSError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not _is_within(target, root):
        raise HTTPException(status_code=403, detail="Path is outside the workspace root")
    return root, target


def _entry_payload(root: Path, path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    rel = "" if path == root else path.relative_to(root).as_posix()
    is_dir = path.is_dir()
    mime = _mime_type(path)
    return {
        "name": path.name or root.name,
        "path": rel,
        "type": "directory" if is_dir else "file",
        "size": None if is_dir else stat.st_size,
        "modified_at": _modified_iso(path),
        "mime": mime,
        "previewable": is_dir or _is_text_path(path, mime) or mime.startswith(IMAGE_MIME_PREFIX),
    }


def _is_text_path(path: Path, mime: str | None = None) -> bool:
    mime = mime or _mime_type(path)
    if mime.startswith("text/"):
        return True
    if mime in {"application/json", "application/xml", "application/x-sh", "application/x-yaml"}:
        return True
    return path.suffix.lower() in TEXT_EXTENSIONS


def list_workspace_tree(session_id: str, relative_path: str = "", depth: int | None = None) -> dict[str, Any]:
    root, start = resolve_workspace_path(session_id, relative_path)
    if not start.exists():
        raise HTTPException(status_code=404, detail="Workspace path not found")
    if not start.is_dir():
        raise HTTPException(status_code=400, detail="Workspace path is not a directory")

    max_depth = max(0, min(int(depth if depth is not None else WORKSPACE_MAX_DEPTH), 8))
    remaining = {"count": max(1, WORKSPACE_MAX_ENTRIES)}

    def sort_key(child: Path) -> tuple[bool, bool, str]:
        return (not child.is_dir(), child.name.startswith("."), child.name.lower())

    def walk(path: Path, current_depth: int) -> list[dict[str, Any]]:
        if remaining["count"] <= 0 or current_depth >= max_depth:
            return []
        try:
            children = sorted(
                [child for child in path.iterdir() if child.name not in WORKSPACE_EXCLUDE_NAMES],
                key=sort_key,
            )
        except OSError:
            return []

        entries: list[dict[str, Any]] = []
        expandable: list[tuple[Path, dict[str, Any]]] = []
        for child in children:
            if remaining["count"] <= 0:
                break
            try:
                child.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            try:
                entry = _entry_payload(root, child)
                is_dir = child.is_dir()
            except HTTPException:
                continue
            remaining["count"] -= 1
            if is_dir:
                entry["children"] = []
                expandable.append((child, entry))
            entries.append(entry)

        for child, entry in expandable:
            if remaining["count"] <= 0:
                break
            entry["children"] = walk(child, current_depth + 1)
        return entries

    return {
        "root": str(root),
        "path": "" if start == root else start.relative_to(root).as_posix(),
        "max_depth": max_depth,
        "truncated": remaining["count"] <= 0,
        "entries": walk(start, 0),
    }


def preview_workspace_file(session_id: str, relative_path: str) -> dict[str, Any]:
    root, path = resolve_workspace_path(session_id, relative_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Workspace file not found")
    if not path.is_file():
        raise HTTPException(status_code=400, detail="Workspace path is not a file")

    entry = _entry_payload(root, path)
    mime = entry["mime"]
    if mime.startswith(IMAGE_MIME_PREFIX):
        return {**entry, "kind": "image", "content": None}

    if not _is_text_path(path, mime):
        return {**entry, "kind": "binary", "content": None}

    size = int(entry.get("size") or 0)
    if size > WORKSPACE_PREVIEW_MAX_BYTES:
        return {
            **entry,
            "kind": "text",
            "content": "",
            "truncated": True,
            "max_bytes": WORKSPACE_PREVIEW_MAX_BYTES,
        }

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {**entry, "kind": "text", "content": content, "truncated": False}
