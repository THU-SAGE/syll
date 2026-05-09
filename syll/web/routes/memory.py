"""Memory API routes."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["memory"])


@router.get("/memory")
async def memory_overview(request: Request):
    """Return memory overview: long-term content, today's notes, file list."""
    store = request.app.state.memory_store
    files = store.list_memory_files()
    return {
        "long_term": store.read_long_term(),
        "today": store.read_today(),
        "files": [
            {"name": f.name, "path": str(f)}
            for f in files
        ],
    }


@router.get("/memory/{filename}")
async def read_memory_file(filename: str, request: Request):
    """Read a specific memory file."""
    store = request.app.state.memory_store
    file_path = store.memory_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Memory file not found")

    return {
        "filename": filename,
        "content": file_path.read_text(encoding="utf-8"),
    }
