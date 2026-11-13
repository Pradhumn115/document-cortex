"""Projects and chats, stored next to the library.

    $CORTEX_HOME/projects.json      [{id, name, docs: [slug], created}]
    $CORTEX_HOME/chats/<id>.json    {id, title, project, docs, pinned, created, updated, messages: [...]}

A message is {question, standalone?, result: {id, answer, sources, check, route, trace, ...}, ts}.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.chats_dir = self.root / "chats"
        self.chats_dir.mkdir(parents=True, exist_ok=True)
        self.pfile = self.root / "projects.json"
        self._lock = threading.RLock()

    # ------------------------------------------------------------- projects
    def projects(self) -> list[dict]:
        if not self.pfile.exists():
            return []
        return json.loads(self.pfile.read_text(encoding="utf-8"))

    def _save_projects(self, ps: list[dict]) -> None:
        self.pfile.write_text(json.dumps(ps, ensure_ascii=False, indent=1), encoding="utf-8")

    def project(self, pid: str) -> dict:
        p = next((p for p in self.projects() if p["id"] == pid), None)
        if not p:
            raise KeyError(pid)
        return p

    def create_project(self, name: str, docs: list[str] | None = None) -> dict:
        name = name.strip()[:80] or "Untitled project"
        p = {"id": uuid.uuid4().hex[:10], "name": name, "docs": list(dict.fromkeys(docs or [])), "created": _now()}
        with self._lock:
            ps = self.projects()
            ps.append(p)
            self._save_projects(ps)
        return p

    def update_project(self, pid: str, name: str | None = None, docs: list[str] | None = None,
                       add_docs: list[str] | None = None) -> dict:
        with self._lock:
            ps = self.projects()
            p = next((p for p in ps if p["id"] == pid), None)
            if not p:
                raise KeyError(pid)
            if name is not None:
                p["name"] = name.strip()[:80] or p["name"]
            if docs is not None:
                p["docs"] = list(dict.fromkeys(docs))
            if add_docs:
                p["docs"] = list(dict.fromkeys(p["docs"] + add_docs))
            self._save_projects(ps)
            return p

    def delete_project(self, pid: str) -> None:
        """The project goes; its chats stay (moved out of the project)."""
        with self._lock:
            self._save_projects([p for p in self.projects() if p["id"] != pid])
            for c in self.chats(project=pid):
                self.update_chat(c["id"], project="")

    def forget_doc(self, slug: str) -> None:
        """A document was removed from the library."""
        with self._lock:
            ps = self.projects()
            for p in ps:
                p["docs"] = [d for d in p["docs"] if d != slug]
            self._save_projects(ps)

    # ---------------------------------------------------------------- chats
    def _cfile(self, cid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{6,32}", cid):
            raise KeyError(cid)
        return self.chats_dir / f"{cid}.json"

    def chat(self, cid: str) -> dict:
        f = self._cfile(cid)
        if not f.exists():
            raise KeyError(cid)
        return json.loads(f.read_text(encoding="utf-8"))

    def _save_chat(self, c: dict) -> None:
        self._cfile(c["id"]).write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")

    def chats(self, project: str | None = None) -> list[dict]:
        """Summaries, newest first. project=None: all chats; "": chats outside any project."""
        out = []
        for f in self.chats_dir.glob("*.json"):
            try:
                c = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if project is not None and (c.get("project") or "") != project:
                continue
            out.append({k: c.get(k) for k in ("id", "title", "project", "docs", "pinned", "created", "updated")}
                       | {"count": len(c.get("messages", []))})
        return sorted(out, key=lambda c: c.get("updated") or "", reverse=True)

    def create_chat(self, project: str = "", docs: list[str] | None = None, title: str = "") -> dict:
        c = {"id": uuid.uuid4().hex[:12], "title": title, "project": project or "", "docs": docs,
             "pinned": False, "created": _now(), "updated": _now(), "messages": []}
        with self._lock:
            self._save_chat(c)
        return c

    def update_chat(self, cid: str, **upd) -> dict:
        allowed = {"title", "pinned", "project", "docs"}
        with self._lock:
            c = self.chat(cid)
            for k, v in upd.items():
                if k in allowed:
                    c[k] = v.strip()[:120] if k == "title" and isinstance(v, str) else v
            self._save_chat(c)
            return c

    def delete_chat(self, cid: str) -> None:
        with self._lock:
            f = self._cfile(cid)
            if f.exists():
                f.unlink()

    def clear_chat(self, cid: str) -> dict:
        with self._lock:
            c = self.chat(cid)
            c["messages"] = []
            c["updated"] = _now()
            self._save_chat(c)
            return c

    def append(self, cid: str, question: str, result: dict, standalone: str | None = None) -> dict:
        with self._lock:
            c = self.chat(cid)
            c["messages"].append({"question": question, "standalone": standalone, "result": result, "ts": _now()})
            if not c.get("title"):
                c["title"] = _title(question)
            c["updated"] = _now()
            self._save_chat(c)
            return c

    def delete_message(self, cid: str, index: int) -> dict:
        with self._lock:
            c = self.chat(cid)
            if not 0 <= index < len(c["messages"]):
                raise KeyError(index)
            c["messages"].pop(index)
            self._save_chat(c)
            return c

    def history_pairs(self, cid: str, n: int = 3) -> list[tuple[str, str]]:
        """The last n (question, answer) turns, for reading follow-up questions."""
        c = self.chat(cid)
        return [(m.get("standalone") or m["question"], m["result"].get("answer", ""))
                for m in c["messages"][-n:]]

    def export_markdown(self, cid: str) -> str:
        c = self.chat(cid)
        lines = [f"# {c.get('title') or 'Chat'}", "", f"_Exported from Document Cortex, {_now()[:10]}_", ""]
        for m in c["messages"]:
            r = m["result"]
            lines += [f"## {m['question']}", "", r.get("answer", "").strip(), ""]
            if r.get("sources"):
                lines.append("**Sources**")
                lines.append("")
                for i, s in enumerate(r["sources"], 1):
                    pg = s.get("pages")
                    pg = f" (p. {pg[0]}{'–' + str(pg[1]) if pg and pg[1] != pg[0] else ''})" if pg else ""
                    lines.append(f"{i}. {s.get('doc_name', s.get('doc'))} › {s.get('path', '')}{pg}")
                lines.append("")
        return "\n".join(lines)


def _title(q: str) -> str:
    q = re.sub(r"\s+", " ", q).strip()
    return q if len(q) <= 60 else q[:57].rsplit(" ", 1)[0] + "…"
