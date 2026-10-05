"""Sandboxed filesystem access.

Every path is resolved and then checked to be inside ``workspace/``. The check
is on the *resolved* path, so ``../../etc/passwd`` and symlink tricks both fail
closed. The agent gets a filesystem; it does not get the filesystem.
"""

from __future__ import annotations

from pathlib import Path

from app.tools.base import ToolError, ToolResult, ToolSpec, prop
from app.tools.context import ToolContext

MAX_READ_CHARS = 20_000
MAX_WRITE_CHARS = 200_000


def _resolve(ctx: ToolContext, raw: str) -> Path:
    root = ctx.workspace.resolve()
    candidate = (root / raw.lstrip("/\\")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(
            f"Path '{raw}' is outside the workspace.",
            kind="blocked",
            remediation=f"Only paths inside the workspace directory are allowed. Try listing '.' first.",
        )
    return candidate


async def file_list(ctx: ToolContext, path: str = ".") -> ToolResult:
    target = _resolve(ctx, path)
    if not target.exists():
        raise ToolError(
            f"'{path}' does not exist in the workspace.",
            kind="not_found",
            remediation="List the workspace root with path='.' to see what is available.",
        )
    if target.is_file():
        return ToolResult(content=f"{path} is a file ({target.stat().st_size} bytes).")

    rows: list[str] = []
    for child in sorted(target.iterdir()):
        rel = child.relative_to(ctx.workspace.resolve()).as_posix()
        if child.is_dir():
            rows.append(f"  {rel}/   (directory)")
        else:
            rows.append(f"  {rel}   ({child.stat().st_size} bytes)")
    listing = "\n".join(rows) or "  (empty)"
    return ToolResult(content=f"Workspace contents of '{path}':\n{listing}")


async def file_read(ctx: ToolContext, path: str) -> ToolResult:
    target = _resolve(ctx, path)
    if not target.is_file():
        raise ToolError(
            f"No file at '{path}'.",
            kind="not_found",
            remediation="Use file_list to see what exists before reading.",
        )
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise ToolError(f"Could not read '{path}': {exc}", kind="invalid_input") from exc

    # Anything credential-shaped in this file is registered now, so it is
    # scrubbed from every durable record for the rest of the run. The model
    # still receives the real value below - it needs it to sign in.
    ctx.secrets.learn_from(text)

    truncated = len(text) > MAX_READ_CHARS
    if truncated:
        text = text[:MAX_READ_CHARS]
    suffix = "\n… file truncated." if truncated else ""
    return ToolResult(content=f"--- {path} ---\n{text}{suffix}")


async def file_write(ctx: ToolContext, path: str, content: str) -> ToolResult:
    if len(content) > MAX_WRITE_CHARS:
        raise ToolError(
            "Content too large to write.",
            kind="invalid_input",
            remediation=f"Keep file writes under {MAX_WRITE_CHARS} characters.",
        )
    target = _resolve(ctx, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return ToolResult(
        content=f"Wrote {len(content)} characters to '{path}'.",
    )


FILE_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="file_list",
        description="List files in the shared workspace directory. Start with path='.'.",
        parameters={
            "properties": {"path": prop("string", "Directory inside the workspace; '.' for the root.")},
            "required": ["path"],
        },
        handler=file_list,
        read_only=True,
    ),
    ToolSpec(
        name="file_read",
        description=(
            "Read a UTF-8 text file from the workspace. Useful for credentials, "
            "reference notes and data the task refers to."
        ),
        parameters={
            "properties": {"path": prop("string", "File path relative to the workspace root.")},
            "required": ["path"],
        },
        handler=file_read,
        read_only=True,
    ),
    ToolSpec(
        name="file_write",
        description="Write a text file into the workspace, creating parent folders as needed.",
        parameters={
            "properties": {
                "path": prop("string", "File path relative to the workspace root."),
                "content": prop("string", "Full file contents to write."),
            },
            "required": ["path", "content"],
        },
        handler=file_write,
        read_only=False,
        mutating=True,
    ),
]
