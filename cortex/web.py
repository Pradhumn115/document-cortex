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
PATCH  /api/docs/{slug}          {title?, folder?}
GET    /api/suggest?docs=a,b     example questions from the documents' index cards
GET    /api/projects             POST {name, docs}   PATCH/DELETE /api/projects/{id}
GET    /api/chats?project=       POST {project?, docs?}
GET    /api/chats/{id}           PATCH {title?, pinned?, project?, docs?}   DELETE
POST   /api/chats/{id}/clear     DELETE /api/chats/{id}/messages/{i}   GET /api/chats/{id}/export
"""
from __future__ import annotations

import json
import queue
import re
import shutil
import tempfile
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, config
from .library import Library
from .workspace import Workspace

STATIC = Path(__file__).parent / "static"
SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt", ".text", ".rst",
             ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}


class AskIn(BaseModel):
    question: str
    docs: list[str] | None = None
    verify: bool = True
    chat_id: str | None = None
    project: str | None = None
    replace: int | None = None  # retry: drop this message index first
    chat_docs: list[str] | None = None  # a new chat's hand-picked documents (None: follow its project)


class DocPatch(BaseModel):
    title: str | None = None
    folder: str | None = None


class ProjectIn(BaseModel):
    name: str | None = None
    docs: list[str] | None = None
    add_docs: list[str] | None = None


class ChatIn(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    project: str | None = None
    docs: list[str] | None = None


class FeedbackIn(BaseModel):
    id: str
    value: int


def create_app(lib: Library | None = None) -> FastAPI:
    lib = lib or Library()
    ws = Workspace(lib.root)
    app = FastAPI(title="Document Cortex", version=__version__)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status():
        from .llm import GROQ_FALLBACK
        from .ocr import VLM_DEFAULT
        return {"version": __version__, "keys": config.keys_status(), "home": str(lib.root),
                "models": {"Index cards and answers": GROQ_FALLBACK[0], "Citation check": "openai/gpt-oss-20b",
                           "Handwriting and poor scans": VLM_DEFAULT, "Printed scans": "Tesseract",
                           "Search embeddings": "intfloat/multilingual-e5-small (local)"}}

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
        ws.forget_doc(slug)
        return {"ok": True}

    @app.patch("/api/docs/{slug}")
    def patch_doc(slug: str, p: DocPatch):
        if not (lib.docs_dir / slug / "meta.json").exists():
            raise HTTPException(404, "no such document")
        upd = {}
        if p.title is not None:
            upd["title"] = p.title.strip()[:120]
        if p.folder is not None:
            upd["folder"] = "/".join(x.strip() for x in p.folder.split("/") if x.strip())[:120]
        return {"slug": slug, **lib._write_meta(slug, **upd)}

    @app.get("/api/suggest")
    def suggest(docs: str = ""):
        import random
        slugs = [d for d in docs.split(",") if d] or lib.ready()
        pool = []
        for slug in slugs[:12]:
            if slug not in lib.ready():
                continue
            d = lib.doc(slug)
            tops = ["root"] + list(d.tree.root.children)[:12]
            qs = [q for nid in tops for q in d.cards.get(nid, {}).get("questions", [])[:2]]
            pool += [(slug, q) for q in qs if 12 <= len(q) <= 110]
        rng = random.Random(len(pool))
        rng.shuffle(pool)
        per, out = {}, []
        for slug, q in pool:  # at most two per document
            if per.get(slug, 0) < 2:
                per[slug] = per.get(slug, 0) + 1
                out.append({"doc": slug, "question": q})
            if len(out) >= 4:
                break
        return out

    # ---------------------------------------------------------- projects
    @app.get("/api/projects")
    def projects():
        return ws.projects()

    @app.post("/api/projects")
    def new_project(p: ProjectIn):
        return ws.create_project(p.name or "", p.docs)

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, p: ProjectIn):
        try:
            return ws.update_project(pid, p.name, p.docs, p.add_docs)
        except KeyError:
            raise HTTPException(404, "no such project")

    @app.delete("/api/projects/{pid}")
    def del_project(pid: str):
        ws.delete_project(pid)
        return {"ok": True}

    # ------------------------------------------------------------- chats
    def _chat(cid: str) -> dict:
        try:
            return ws.chat(cid)
        except KeyError:
            raise HTTPException(404, "no such chat")

    @app.get("/api/chats")
    def chats(project: str | None = None):
        return ws.chats(project)

    @app.post("/api/chats")
    def new_chat(c: ChatIn):
        return ws.create_chat(c.project or "", c.docs, c.title or "")

    @app.get("/api/chats/{cid}")
    def get_chat(cid: str):
        return _chat(cid)

    @app.patch("/api/chats/{cid}")
    def patch_chat(cid: str, c: ChatIn):
        _chat(cid)
        return ws.update_chat(cid, **{k: v for k, v in c.model_dump().items() if v is not None})

    @app.delete("/api/chats/{cid}")
    def del_chat(cid: str):
        _chat(cid)
        ws.delete_chat(cid)
        return {"ok": True}

    @app.post("/api/chats/{cid}/clear")
    def clear_chat(cid: str):
        _chat(cid)
        return ws.clear_chat(cid)

    @app.delete("/api/chats/{cid}/messages/{i}")
    def del_message(cid: str, i: int):
        _chat(cid)
        try:
            return ws.delete_message(cid, i)
        except KeyError:
            raise HTTPException(404, "no such message")

    @app.get("/api/chats/{cid}/export")
    def export_chat(cid: str):
        c = _chat(cid)
        name = re.sub(r"[^\w -]+", "", c.get("title") or "chat").strip()[:60] or "chat"
        return PlainTextResponse(ws.export_markdown(cid), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.md"'})

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

        cid = q.chat_id
        if cid:
            _chat(cid)
            if q.replace is not None:
                try:
                    ws.delete_message(cid, q.replace)
                except KeyError:
                    pass
        else:
            cid = ws.create_chat(q.project or "", q.chat_docs)["id"]
        history = ws.history_pairs(cid) if ws.chat(cid)["messages"] else None
        events.put({"type": "chat", "chat_id": cid})

        def work():
            try:
                res = lib.ask(q.question.strip(), q.docs or None, verify=q.verify, history=history,
                              on_step=lambda st: events.put({"type": "step", **st}))
                for s in res["sources"]:
                    s["text"] = s["text"][:1200]
                ws.append(cid, q.question.strip(), res, res.get("standalone"))
                events.put({"type": "result", "chat_id": cid, **res})
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
