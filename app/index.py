"""PDF indexing + hybrid (BM25 keyword + embedding) search with page-level citations."""
from __future__ import annotations

import difflib
import re
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import numpy as np

try:
    import pymupdf  # type: ignore
except ImportError:  # older PyMuPDF
    import fitz as pymupdf  # type: ignore

STOP = set(
    "the a an of in on for to and or is are was were be by with what which who how does do did this that these those "
    "from as at it its into than then their there paper papers used use using show shows say said about between".split()
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(id INTEGER PRIMARY KEY, rel TEXT UNIQUE, title TEXT, mtime REAL, size INTEGER, pages INTEGER);
CREATE TABLE IF NOT EXISTS pages(paper_id INTEGER, page INTEGER, text TEXT, PRIMARY KEY(paper_id, page));
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, paper_id INTEGER, page INTEGER, text TEXT, emb BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text);
CREATE INDEX IF NOT EXISTS chunks_paper ON chunks(paper_id);
"""


def chunk_page(text: str, size: int = 1100, overlap: int = 150) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"\n{2,}", "\n\n", text).strip()
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    if len(text) <= size:
        return [text] if len(text) > 40 else []
    out, i = [], 0
    while i < len(text):
        j = min(i + size, len(text))
        if j < len(text):
            k = text.rfind(". ", i + size // 2, j)
            if k != -1:
                j = k + 1
        out.append(text[i:j].strip())
        if j >= len(text):
            break
        i = max(j - overlap, i + 1)
    return out


class Index:
    def __init__(self, paper_root: str, data_dir: str, embed_url: str, embed_model: str):
        self.root = Path(paper_root)
        self.dbfile = Path(data_dir) / "index.sqlite"
        self.dbfile.parent.mkdir(parents=True, exist_ok=True)
        self.embed_url, self.embed_model = embed_url.rstrip("/"), embed_model
        self.lock = threading.Lock()
        self.status = {"state": "idle", "done": 0, "total": 0, "current": "", "embeddings": None, "error": ""}
        self._mat = None  # (ids, paper_ids, normalized matrix)
        con = self.db()
        con.executescript(SCHEMA)
        con.close()

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.dbfile, timeout=60)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # ---------------- embeddings ----------------
    def embed(self, texts: list[str], prefix: str) -> list[list[float]]:
        r = httpx.post(
            f"{self.embed_url}/api/embed",
            json={"model": self.embed_model, "input": [prefix + t for t in texts]},
            timeout=300,
        )
        r.raise_for_status()
        return r.json()["embeddings"]

    # ---------------- indexing ----------------
    def scan(self) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.status.update(state="scanning", error="")
            disk = {}
            for p in self.root.rglob("*.pdf"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                disk[p.relative_to(self.root).as_posix()] = (st.st_mtime, st.st_size, p)
            con = self.db()
            known = {r["rel"]: r for r in con.execute("SELECT * FROM papers")}
            for rel in set(known) - set(disk):
                self._delete_paper(con, known[rel]["id"])
            todo = [rel for rel, (mt, sz, _p) in disk.items()
                    if rel not in known or abs(known[rel]["mtime"] - mt) > 1 or known[rel]["size"] != sz]
            self.status.update(state="indexing", done=0, total=len(todo))
            for n, rel in enumerate(sorted(todo), 1):
                self.status.update(current=rel, done=n - 1)
                try:
                    self._index_pdf(con, rel, *disk[rel])
                except Exception as e:  # keep going on bad PDFs
                    self.status["error"] = f"{rel}: {e}"
            self._embed_missing(con)
            con.close()
            self._mat = None
            self.status.update(state="idle", done=len(todo), current="")
        except Exception as e:
            self.status.update(state="error", error=str(e))
        finally:
            self.lock.release()

    def _delete_paper(self, con, pid: int) -> None:
        ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE paper_id=?", (pid,))]
        con.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        con.execute("DELETE FROM chunks WHERE paper_id=?", (pid,))
        con.execute("DELETE FROM pages WHERE paper_id=?", (pid,))
        con.execute("DELETE FROM papers WHERE id=?", (pid,))
        con.commit()

    def _index_pdf(self, con, rel: str, mtime: float, size: int, path: Path) -> None:
        old = con.execute("SELECT id FROM papers WHERE rel=?", (rel,)).fetchone()
        if old:
            self._delete_paper(con, old["id"])
        doc = pymupdf.open(path)
        title = Path(rel).stem
        cur = con.execute("INSERT INTO papers(rel,title,mtime,size,pages) VALUES(?,?,?,?,?)",
                          (rel, title, mtime, size, doc.page_count))
        pid = cur.lastrowid
        for pno in range(doc.page_count):
            text = doc[pno].get_text("text") or ""
            con.execute("INSERT INTO pages VALUES(?,?,?)", (pid, pno + 1, text))
            for ch in chunk_page(text):
                c = con.execute("INSERT INTO chunks(paper_id,page,text) VALUES(?,?,?)", (pid, pno + 1, ch))
                con.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (c.lastrowid, ch))
        doc.close()
        con.commit()

    def _embed_missing(self, con) -> None:
        rows = con.execute("SELECT id,text FROM chunks WHERE emb IS NULL").fetchall()
        if not rows:
            self.status["embeddings"] = True
            return
        self.status.update(state="embedding", done=0, total=len(rows))
        try:
            for i in range(0, len(rows), 16):
                batch = rows[i:i + 16]
                vecs = self.embed([r["text"] for r in batch], "search_document: ")
                con.executemany("UPDATE chunks SET emb=? WHERE id=?",
                                [(np.asarray(v, dtype=np.float32).tobytes(), r["id"]) for v, r in zip(vecs, batch)])
                con.commit()
                self.status["done"] = i + len(batch)
            self.status["embeddings"] = True
        except Exception as e:
            self.status["embeddings"] = False
            self.status["error"] = f"embedding unavailable ({e}); keyword search still works"

    # ---------------- search ----------------
    def papers(self) -> list[dict]:
        con = self.db()
        rows = [dict(r) for r in con.execute("SELECT id,rel,title,pages FROM papers ORDER BY title")]
        con.close()
        return rows

    def resolve(self, name: str) -> tuple[dict | None, list[str]]:
        papers = self.papers()
        n = name.strip().lower()
        for p in papers:
            if p["title"].lower() == n:
                return p, []
        subs = [p for p in papers if n in p["title"].lower() or n in p["rel"].lower()]
        if len(subs) == 1:
            return subs[0], []
        if len(subs) > 1:
            return None, [p["title"] for p in subs]
        close = difflib.get_close_matches(n, [p["title"].lower() for p in papers], n=3, cutoff=0.5)
        if len(close) == 1:
            return next(p for p in papers if p["title"].lower() == close[0]), []
        return None, [p["title"] for p in papers if p["title"].lower() in close]

    def _load_matrix(self):
        if self._mat is None:
            con = self.db()
            rows = con.execute("SELECT id,paper_id,emb FROM chunks WHERE emb IS NOT NULL").fetchall()
            con.close()
            if not rows:
                self._mat = (np.array([], dtype=np.int64), np.array([], dtype=np.int64), np.zeros((0, 1), np.float32))
            else:
                m = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
                m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
                self._mat = (np.array([r["id"] for r in rows]), np.array([r["paper_id"] for r in rows]), m)
        return self._mat

    def search(self, query: str, k: int = 6, paper: str | None = None) -> list[dict]:
        pid_filter: set[int] | None = None
        if paper:
            p, _ = self.resolve(paper)
            if p:
                pid_filter = {p["id"]}
        con = self.db()
        toks = [t for t in re.findall(r"\w{2,}", query) if t.lower() not in STOP]
        fts_ids: list[int] = []
        if toks:
            q = " OR ".join(f'"{t}"' for t in toks)
            sql = ("SELECT c.id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ? "
                   + (f"AND c.paper_id IN ({','.join(map(str, pid_filter))}) " if pid_filter else "")
                   + "ORDER BY bm25(chunks_fts) LIMIT 40")
            try:
                fts_ids = [r[0] for r in con.execute(sql, (q,))]
            except sqlite3.OperationalError:
                fts_ids = []
        vec_ids: list[int] = []
        try:
            ids, pids, mat = self._load_matrix()
            if len(ids):
                qv = np.asarray(self.embed([query], "search_query: ")[0], dtype=np.float32)
                qv /= np.linalg.norm(qv) + 1e-9
                sims = mat @ qv
                if pid_filter:
                    sims = np.where(np.isin(pids, list(pid_filter)), sims, -1)
                vec_ids = [int(ids[i]) for i in np.argsort(-sims)[:40] if sims[i] > 0]
        except Exception:
            pass
        score: dict[int, float] = {}
        for lst in (fts_ids, vec_ids):
            for rank, cid in enumerate(lst):
                score[cid] = score.get(cid, 0) + 1 / (60 + rank)
        top = sorted(score, key=score.get, reverse=True)[:k]
        out = []
        for cid in top:
            r = con.execute("SELECT c.page,c.text,p.title FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?", (cid,)).fetchone()
            out.append({"paper": r["title"], "page": r["page"], "text": r["text"], "score": round(score[cid], 4)})
        con.close()
        return out

    def pages(self, paper: str, start: int, end: int) -> tuple[str, str | None]:
        p, alts = self.resolve(paper)
        if not p:
            return "", "No paper matches '%s'. Candidates: %s" % (paper, ", ".join(alts) or "none")
        con = self.db()
        rows = con.execute("SELECT page,text FROM pages WHERE paper_id=? AND page BETWEEN ? AND ? ORDER BY page",
                           (p["id"], start, end)).fetchall()
        con.close()
        body = "\n\n".join(f"--- {p['title']} | page {r['page']}/{p['pages']} ---\n{r['text'].strip()}" for r in rows)
        return body, None
