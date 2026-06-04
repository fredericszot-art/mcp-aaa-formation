#!/usr/bin/env python3
"""
Local MCP server exposing filesystem and script-execution tools.

Tools:
  run_script  — run a Python script with optional arguments
  list_files  — list files in a folder
  read_file   — read the contents of a file

Transport: Streamable HTTP on localhost:3000
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", 10000))
mcp = FastMCP("local_tools_mcp", host="0.0.0.0", port=PORT)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve(raw_path: str) -> Path:
    """Expand ~ and env vars, return an absolute Path."""
    return Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()


def _error(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class RunScriptInput(BaseModel):
    """Input model for run_script."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    script_path: str = Field(
        ...,
        description="Absolute or relative path to the Python script to run (e.g. 'C:/Users/frede/Desktop/drive_upload.py').",
        min_length=1,
    )
    args: list[str] = Field(
        default_factory=list,
        description="List of command-line arguments to pass to the script (e.g. ['--auto-thread', 'file.pdf']).",
    )
    timeout: Optional[int] = Field(
        default=120,
        description="Maximum seconds to wait before killing the process (default: 120).",
        ge=1,
        le=600,
    )

    @field_validator("script_path")
    @classmethod
    def must_be_py(cls, v: str) -> str:
        if not v.strip().endswith(".py"):
            raise ValueError("script_path must point to a .py file")
        return v


class ListFilesInput(BaseModel):
    """Input model for list_files."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    folder_path: str = Field(
        ...,
        description="Absolute or relative path to the folder to list (e.g. 'C:/Users/frede/Desktop').",
        min_length=1,
    )
    pattern: str = Field(
        default="*",
        description="Glob pattern to filter results (e.g. '*.pdf', '*.py'). Default: '*' (all files).",
    )
    recursive: bool = Field(
        default=False,
        description="If true, recurse into sub-folders.",
    )


class ReadFileInput(BaseModel):
    """Input model for read_file."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    file_path: str = Field(
        ...,
        description="Absolute or relative path to the file to read (e.g. 'C:/Users/frede/Desktop/drive_upload.py').",
        min_length=1,
    )
    encoding: str = Field(
        default="utf-8",
        description="File encoding (default: 'utf-8'). Use 'latin-1' for legacy Windows files.",
    )
    max_bytes: int = Field(
        default=512_000,
        description="Maximum bytes to read to avoid huge responses (default: 512 KB).",
        ge=1,
        le=10_000_000,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="run_script",
    annotations={
        "title": "Run Python Script",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def run_script(params: RunScriptInput) -> str:
    """Run a Python script with optional arguments and return its output.

    Executes the script using the same Python interpreter that runs this server
    (sys.executable). stdout and stderr are both captured and returned.

    Args:
        params (RunScriptInput):
            - script_path (str): Path to the .py file.
            - args (list[str]): CLI arguments forwarded to the script.
            - timeout (int): Kill timeout in seconds (default 120).

    Returns:
        str: JSON with keys:
            - "returncode" (int): Process exit code (0 = success).
            - "stdout" (str): Standard output from the script.
            - "stderr" (str): Standard error output.
            - "error" (str, only on failure): Human-readable error message.

    Examples:
        - Run drive_upload.py on a file:
          script_path="C:/Users/frede/Desktop/drive_upload.py"
          args=["--auto-thread", "C:/Users/frede/Downloads/Facture_X.pdf"]
        - Run with no args:
          script_path="C:/Users/frede/Desktop/drive_upload.py", args=[]
    """
    script = _resolve(params.script_path)

    if not script.exists():
        return _error(f"Script not found: {script}")
    if not script.is_file():
        return _error(f"Path is not a file: {script}")

    cmd = [sys.executable, str(script)] + params.args

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=params.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return _error(f"Script timed out after {params.timeout}s: {script.name}")

        return json.dumps(
            {
                "returncode": proc.returncode,
                "stdout": stdout_b.decode("utf-8", errors="replace"),
                "stderr": stderr_b.decode("utf-8", errors="replace"),
            },
            ensure_ascii=False,
            indent=2,
        )

    except Exception as exc:
        return _error(f"Failed to launch script: {exc}")


@mcp.tool(
    name="list_files",
    annotations={
        "title": "List Files in Folder",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_files(params: ListFilesInput) -> str:
    """List files (and optionally sub-folders) inside a directory.

    Args:
        params (ListFilesInput):
            - folder_path (str): Path to the folder.
            - pattern (str): Glob filter, e.g. '*.pdf' (default '*').
            - recursive (bool): Recurse into sub-folders (default False).

    Returns:
        str: JSON with keys:
            - "folder" (str): Resolved folder path.
            - "count" (int): Number of entries returned.
            - "files" (list[dict]): Each entry has:
                - "name" (str): Filename.
                - "path" (str): Full absolute path.
                - "size_bytes" (int): File size (0 for directories).
                - "is_dir" (bool): True if entry is a directory.
    """
    folder = _resolve(params.folder_path)

    if not folder.exists():
        return _error(f"Folder not found: {folder}")
    if not folder.is_dir():
        return _error(f"Path is not a directory: {folder}")

    glob_fn = folder.rglob if params.recursive else folder.glob
    entries = sorted(glob_fn(params.pattern), key=lambda p: (p.is_file(), p.name.lower()))

    files = []
    for entry in entries:
        try:
            size = entry.stat().st_size if entry.is_file() else 0
        except OSError:
            size = -1
        files.append(
            {
                "name": entry.name,
                "path": str(entry),
                "size_bytes": size,
                "is_dir": entry.is_dir(),
            }
        )

    return json.dumps(
        {"folder": str(folder), "count": len(files), "files": files},
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="read_file",
    annotations={
        "title": "Read File Contents",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def read_file(params: ReadFileInput) -> str:
    """Read and return the text contents of a file.

    Args:
        params (ReadFileInput):
            - file_path (str): Path to the file.
            - encoding (str): Text encoding (default 'utf-8').
            - max_bytes (int): Read limit in bytes (default 512 KB).

    Returns:
        str: JSON with keys:
            - "file" (str): Resolved file path.
            - "size_bytes" (int): Actual file size on disk.
            - "read_bytes" (int): How many bytes were read.
            - "truncated" (bool): True if file was larger than max_bytes.
            - "content" (str): File text content.
    """
    path = _resolve(params.file_path)

    if not path.exists():
        return _error(f"File not found: {path}")
    if not path.is_file():
        return _error(f"Path is not a file: {path}")

    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            raw = fh.read(params.max_bytes)

        content = raw.decode(params.encoding, errors="replace")
        return json.dumps(
            {
                "file": str(path),
                "size_bytes": size,
                "read_bytes": len(raw),
                "truncated": size > params.max_bytes,
                "content": content,
            },
            ensure_ascii=False,
            indent=2,
        )

    except Exception as exc:
        return _error(f"Could not read file: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting local MCP server on http://localhost:3000 …", flush=True)
    mcp.run(transport="streamable-http")
