"""Local web UI + JSON API (FastAPI). Start with:  cortex serve

GET    /                         the app (static/index.html)
GET    /api/status               keys found, data folder
GET    /api/docs                 library
POST   /api/docs                 upload one or more files (multipart "files"); ingests in the background
DELETE /api/docs/{slug}
GET    /api/docs/{slug}/outline  section tree with page ranges
GET    /api/docs/{slug}/node/{id}  section text, path, pages, card
GET    /api/docs/{slug}/file     the original file
POST   /api/ask                  {question, docs?: [slug], verify?: bool}
POST   /api/ask/stream           same, streamed as NDJSON: process steps live, then the result
POST   /api/feedback             {id, value: 1 | -1}
GET    /api/history
POST   /api/consolidate
"""
from __future__ import annotations

import json
import queue
import shutil
import tempfile
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, config
from .library import Library

STATIC = Path(__file__).parent / "static"
SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt", ".text", ".rst",
             ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}


class AskIn(BaseModel):
    question: str
    docs: list[str] | None = None
    verify: bool = True


class FeedbackIn(BaseModel):
    id: str
    value: int


def create_app(lib: Library | None = None) -> FastAPI:
    lib = lib or Library()
    app = FastAPI(title="Document Cortex", version=__version__)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status():
        return {"version": __version__, "keys": config.keys_status(), "home": str(lib.root)}

    @app.get("/api/docs")
    def docs():
        return lib.list()

    @app.post("/api/docs")
    async def upload(files: list[UploadFile] = File(...)):
        added = []
        for f in files:
            name = Path(f.filename or "document").name
            if Path(name).suffix.lower() not in SUPPORTED:
                raise HTTPException(400, f"{name}: unsupported type (use PDF, DOCX, MD, TXT or an image)")
            tmp = Path(tempfile.mkdtemp()) / name
            with tmp.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            added.append(lib.add(tmp, name=name, background=True))
        return {"added": added}

    @app.delete("/api/docs/{slug}")
    def delete(slug: str):
        try:
            lib.delete(slug)
        except FileNotFoundError:
            raise HTTPException(404, "no such document")
        return {"ok": True}

    def _doc(slug: str):
        if slug not in lib.ready():
            raise HTTPException(404, "document not ready")
        return lib.doc(slug)

    @app.get("/api/docs/{slug}/outline")
    def outline(slug: str):
        d = _doc(slug)
        t = d.tree

        def node(nid, depth):
            n = t.nodes[nid]
            return {"id": nid, "title": n.title, "pages": n.meta.get("pages"),
                    "children": [node(c, depth + 1) for c in n.children] if depth < 6 else []}
        return {"name": d.name, "summary": d.cards.get("root", {}).get("summary", ""),
                "ocr": t.root.meta.get("ocr"), "children": [node(c, 1) for c in t.root.children]}

    @app.get("/api/docs/{slug}/node/{nid}")
    def node_text(slug: str, nid: str):
        d = _doc(slug)
        if nid not in d.tree.nodes:
            raise HTTPException(404, "no such section")
        n = d.tree.nodes[nid]
        text = n.text
        if len(text) < 400 and n.children:  # thin parent: show the start of its subsections too
            text += "".join(f"\n\n## {d.tree.nodes[c].title}\n{d.tree.nodes[c].text[:1500]}" for c in n.children[:8])
        return {"doc": slug, "doc_name": d.name, "id": nid, "title": n.title, "path": d.tree.path_str(nid),
                "pages": n.meta.get("pages"), "text": text, "card": d.cards.get(nid, {})}

    @app.get("/api/docs/{slug}/file")
    def original(slug: str):
        work = lib.docs_dir / slug
        src = next(work.glob("source.*"), None) if work.exists() else None
        if not src:
            raise HTTPException(404, "no file")
        return FileResponse(src, filename=lib.meta(slug).get("file", src.name))

    @app.post("/api/ask")
    async def ask(q: AskIn):
        if not q.question.strip():
            raise HTTPException(400, "empty question")
        try:
            res = await run_in_threadpool(lib.ask, q.question.strip(), q.docs or None, verify=q.verify)
        except ValueError as e:
            raise HTTPException(400, str(e))
        for s in res["sources"]:
            s["text"] = s["text"][:1200]
        return res

    @app.post("/api/ask/stream")
    def ask_stream(q: AskIn):
        """NDJSON stream: {"type":"step", ...} for each process step as it happens,
        then {"type":"result", ...} (or {"type":"error"})."""
        if not q.question.strip():
            raise HTTPException(400, "empty question")
        events: queue.Queue = queue.Queue()

        def work():
            try:
                res = lib.ask(q.question.strip(), q.docs or None, verify=q.verify,
                              on_step=lambda st: events.put({"type": "step", **st}))
                for s in res["sources"]:
                    s["text"] = s["text"][:1200]
                events.put({"type": "result", **res})
            except Exception as e:  # shown in the UI instead of a silent failure
                events.put({"type": "error", "message": str(e)[:500]})
            events.put(None)

        threading.Thread(target=work, daemon=True).start()

        def stream():
            while (ev := events.get()) is not None:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.post("/api/feedback")
    def feedback(f: FeedbackIn):
        lib.feedback(f.id, f.value)
        return {"ok": True}

    @app.get("/api/history")
    def history():
        return lib.history_items(200)

    @app.post("/api/consolidate")
    async def consolidate():
        changed = await run_in_threadpool(lib.consolidate, lambda *_: None)
        return {"changed": changed}

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn
    url = f"http://{host}:{port}"
    print(f"Document Cortex running at {url}  (data: {config.home()})  Ctrl-C to stop")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
