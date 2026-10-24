"""File tools: reading downloaded documents.

In AcmeOps the invoice PDF is the authoritative source for the total payable -
the web summary list deliberately shows only the pre-tax subtotal. So a task
cannot be completed correctly by scraping HTML alone, and the agent has to
decide for itself that it needs the document.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from .base import Observation, ToolRegistry, number, obj_schema, string

MAX_CHARS = 8000


def register_file_tools(registry: ToolRegistry) -> None:
    @registry.tool(
        "read_pdf",
        "Extract the text of a PDF that has been downloaded to local disk. Use this to "
        "read authoritative figures off an invoice document.",
        obj_schema(
            {
                "path": string("Local path returned by browser_download"),
                "max_pages": number("Pages to read, default 3"),
            },
            ["path"],
        ),
    )
    def read_pdf(path: str, max_pages: float | int = 3) -> Observation:  # type: ignore[misc]
        # An empty path resolves to "." and then fails with "Is a directory",
        # which tells the agent nothing about what it did wrong.
        if not (path or "").strip():
            return Observation.failure(
                "no path given. Download the document first with browser_download, "
                "then pass the path it returned."
            )
        file_path = Path(path)
        if file_path.is_dir():
            return Observation.failure(f"{path} is a directory, not a PDF file")
        if not file_path.exists():
            return Observation.failure(f"no such file: {path}")
        try:
            reader = PdfReader(str(file_path))
            pages = reader.pages[: int(max_pages or 3)]
            text = "\n\n".join((page.extract_text() or "") for page in pages).strip()
        except Exception as exc:  # noqa: BLE001 - surface parse errors to the planner
            return Observation.failure(f"could not parse PDF {path}: {exc}")
        if not text:
            return Observation.failure(
                f"{path} produced no extractable text (it may be a scanned image)"
            )
        return Observation(
            ok=True,
            summary=f"Text of {file_path.name} ({len(reader.pages)} page(s)):\n{text[:MAX_CHARS]}",
            data={"path": str(file_path), "text": text[:MAX_CHARS], "pages": len(reader.pages)},
            artifacts=[str(file_path)],
        )

    @registry.tool(
        "read_text_file",
        "Read a UTF-8 text file from local disk.",
        obj_schema({"path": string("Local path to read")}, ["path"]),
    )
    def read_text_file(path: str) -> Observation:  # type: ignore[misc]
        file_path = Path(path)
        if not file_path.exists():
            return Observation.failure(f"no such file: {path}")
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return Observation.failure(f"could not read {path}: {exc}")
        return Observation(
            ok=True,
            summary=f"Contents of {file_path.name}:\n{text[:MAX_CHARS]}",
            data={"text": text[:MAX_CHARS]},
        )
