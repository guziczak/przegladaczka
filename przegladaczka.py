#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Przegladaczka sesji  -  panel sterowania do Claude Code i Codex.

Jeden plik, zero zaleznosci (tylko biblioteka standardowa Pythona).
- Przeglada sesje Claude Code (~/.claude/projects) i Codex (~/.codex/sessions)
- Ladnie renderuje rozmowe: markdown, kod, myslenie, wywolania narzedzi, obrazy
- Panel sterowania: wznawia sesje w nowym terminalu (w katalogu, ktorego dotyczyla),
  zaklada nowe sesje, otwiera folder
- Domyslnie z "dangerously skip permissions" (Claude) /
  "--dangerously-bypass-approvals-and-sandbox" (Codex) - checkbox mozna odznaczyc

Uruchomienie:  python przegladaczka.py      (przy 1. razie zaklada .venv i sam sie przeladowuje)
               python przegladaczka.py --no-venv   (bez venv)
               python przegladaczka.py --port 8800
"""

import os
import sys
import subprocess

# ============================================================================
#  SAMO-INSTALACJA: zaloz .venv obok pliku i przeladuj sie do niego.
#  (Apka nie ma zaleznosci - venv to tylko izolacja "prawdziwej apki".)
# ============================================================================
def _bootstrap():
    if os.environ.get("PRZEGLADARKA_BOOT") == "1" or "--no-venv" in sys.argv:
        os.environ["PRZEGLADARKA_BOOT"] = "1"
        return
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        vdir = os.path.join(here, ".venv")
        py = (os.path.join(vdir, "Scripts", "python.exe") if os.name == "nt"
              else os.path.join(vdir, "bin", "python"))
        if not os.path.exists(py):
            print("[setup] Tworze srodowisko .venv (jednorazowo, bez instalacji paczek)...")
            import venv as _venv
            _venv.EnvBuilder(with_pip=False, clear=False).create(vdir)
        if os.path.abspath(py) != os.path.abspath(sys.executable):
            env = dict(os.environ)
            env["PRZEGLADARKA_BOOT"] = "1"
            print("[setup] Uruchamiam w .venv ...")
            rc = subprocess.call([py, os.path.abspath(__file__)] + sys.argv[1:], env=env)
            sys.exit(rc)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[setup] Pomijam venv ({e}) - uruchamiam bez izolacji.")
    os.environ["PRZEGLADARKA_BOOT"] = "1"


_bootstrap()

# --- reszta importow (juz w docelowym interpreterze) ---
import json
import html as _html
import time
import socket
import threading
import shutil
import tempfile
import webbrowser
import datetime
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============================================================================
#  KONFIGURACJA / ODKRYWANIE
# ============================================================================
HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(APP_DIR, ".session_cache.json")

WT_BIN = shutil.which("wt")
CLAUDE_BIN = shutil.which("claude")
CODEX_BIN = shutil.which("codex")

MAX_OUT = 16000      # przycinanie dlugich wyjsc narzedzi
MAX_TEXT = 80000     # przycinanie dlugich tekstow

# orientacyjne ceny (USD / 1M tokenow) tylko do szacunku "≈"
PRICES = {
    "opus": (15.0, 75.0), "sonnet": (3.0, 15.0), "haiku": (0.8, 4.0),
    "gpt-5.5": (1.25, 10.0), "gpt-5": (1.25, 10.0), "o3": (2.0, 8.0), "o4": (1.1, 4.4),
}

# ============================================================================
#  POMOCNIKI
# ============================================================================
def _load_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _img_uri(source):
    if isinstance(source, dict) and source.get("type") == "base64" and source.get("data"):
        mt = source.get("media_type", "image/png")
        return f"data:{mt};base64,{source['data']}"
    if isinstance(source, dict) and isinstance(source.get("url"), str):
        return source["url"]
    return None


def _trunc(s, n):
    if s is None:
        return None, False, 0
    s = str(s)
    if len(s) <= n:
        return s, False, len(s)
    return s[:n], True, len(s)


def _iso_from_mtime(ts):
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    except Exception:
        return None


def _est_cost(model, tin, tout):
    if not model:
        return None
    m = model.lower()
    for key, (pi, po) in PRICES.items():
        if key in m:
            return round(tin / 1e6 * pi + tout / 1e6 * po, 3)
    return None


def ev(t, ts=None, **kw):
    d = {"t": t}
    if ts:
        d["ts"] = ts
    for k, v in kw.items():
        if v is not None and v != "":
            d[k] = v
    return d


def _text_ev(t, ts, text, **kw):
    txt, tr, _ = _trunc(text, MAX_TEXT)
    if tr:
        kw["text_trunc"] = True
    return ev(t, ts, text=txt, **kw)


# ============================================================================
#  NORMALIZACJA CLAUDE CODE
# ============================================================================
def _claude_user_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _result_content(content):
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if isinstance(content, list):
        texts, imgs = [], []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    texts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    u = _img_uri(b.get("source"))
                    if u:
                        imgs.append(u)
            elif isinstance(b, str):
                texts.append(b)
        return "\n".join(t for t in texts if t), imgs
    return json.dumps(content, ensure_ascii=False), []


def normalize_claude(path):
    objs = _load_jsonl(path)
    results = {}
    for o in objs:
        msg = o.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    txt, imgs = _result_content(b.get("content"))
                    results[b.get("tool_use_id")] = {
                        "out": txt, "imgs": imgs, "ok": not b.get("is_error")}

    events = []
    cwd = None
    title = None
    first_user = None
    created = updated = None
    n_user = n_asst = 0
    tin = tout = tcache = 0
    model_last = None

    for o in objs:
        t = o.get("type")
        ts = o.get("timestamp")
        if ts:
            created = created or ts
            updated = ts
        if o.get("cwd"):
            cwd = o.get("cwd")
        if t == "ai-title" and o.get("aiTitle"):
            title = o.get("aiTitle")

        if t == "assistant":
            msg = o.get("message") or {}
            model = msg.get("model")
            if model:
                model_last = model
            agent = o.get("attributionAgent")
            u = msg.get("usage") or {}
            tin += u.get("input_tokens", 0) or 0
            tout += u.get("output_tokens", 0) or 0
            tcache += (u.get("cache_read_input_tokens", 0) or 0) + \
                      (u.get("cache_creation_input_tokens", 0) or 0)
            had_visible = False
            for b in msg.get("content", []) or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    events.append(_text_ev("assistant", ts, b["text"], model=model, agent=agent))
                    had_visible = True
                elif bt == "thinking" and b.get("thinking", "").strip():
                    events.append(_text_ev("thinking", ts, b["thinking"], agent=agent))
                elif bt == "tool_use":
                    res = results.get(b.get("id"), {})
                    out, otr, olen = _trunc(res.get("out"), MAX_OUT)
                    events.append(ev("tool_call", ts,
                                     tool=b.get("name"),
                                     input=b.get("input", {}),
                                     output=out,
                                     out_trunc=otr or None,
                                     out_len=olen if otr else None,
                                     ok=res.get("ok"),
                                     images=res.get("imgs") or None,
                                     agent=agent))
                elif bt == "image":
                    u = _img_uri(b.get("source"))
                    if u:
                        events.append(ev("image", ts, img=u, who="assistant"))
            if had_visible:
                n_asst += 1
        elif t == "user":
            msg = o.get("message") or {}
            if o.get("isMeta"):
                tx = _claude_user_text(msg.get("content"))
                if tx:
                    events.append(_text_ev("meta", ts, tx, label="systemowe"))
                continue
            c = msg.get("content")
            if isinstance(c, str):
                if c.strip():
                    events.append(_text_ev("user", ts, c))
                    if not first_user:
                        first_user = c.strip()
                    n_user += 1
            elif isinstance(c, list):
                texts, imgs = [], []
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        texts.append(b.get("text", ""))
                    elif b.get("type") == "image":
                        u = _img_uri(b.get("source"))
                        if u:
                            imgs.append(u)
                jt = "\n".join(x for x in texts if x).strip()
                if jt:
                    events.append(_text_ev("user", ts, jt))
                    if not first_user:
                        first_user = jt
                    n_user += 1
                for im in imgs:
                    events.append(ev("image", ts, img=im, who="user"))
        elif t == "system":
            sub = o.get("subtype", "system")
            dur = o.get("durationMs")
            extra = f" - {round(dur/1000,1)} s" if isinstance(dur, (int, float)) else ""
            events.append(ev("system", ts, text=f"{sub}{extra}", meta=True))

    if not title:
        title = (first_user[:90] if first_user else "(bez tytulu)")

    meta = {
        "tool": "claude",
        "id": os.path.splitext(os.path.basename(path))[0],
        "title": title,
        "cwd": cwd or "",
        "model": model_last or "",
        "created": created,
        "updated": updated or _iso_from_mtime(os.path.getmtime(path)),
        "messages": n_user + n_asst,
        "user": n_user,
        "tokens_in": tin,
        "tokens_out": tout,
        "tokens_cache": tcache,
        "tokens_total": tin + tout,
        "cost": _est_cost(model_last, tin + tcache, tout),
    }
    return {"meta": meta, "events": events}


# ============================================================================
#  NORMALIZACJA CODEX
# ============================================================================
def _codex_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("text"))
    return ""


def _codex_args(s):
    if isinstance(s, dict):
        return s
    if isinstance(s, str):
        try:
            return json.loads(s)
        except Exception:
            return {"_raw": s}
    return {}


def _codex_reasoning(p):
    s = p.get("summary")
    if isinstance(s, list) and s:
        parts = [x.get("text", "") if isinstance(x, dict) else str(x) for x in s]
        t = "\n".join(x for x in parts if x).strip()
        return t or None
    return None


def normalize_codex(path):
    objs = _load_jsonl(path)
    outputs = {}
    for o in objs:
        if o.get("type") == "response_item":
            p = o.get("payload", {}) or {}
            if p.get("type") == "function_call_output":
                outputs[p.get("call_id")] = p.get("output")

    events = []
    cwd = None
    sid = os.path.splitext(os.path.basename(path))[0]
    model = None
    title = None
    created = updated = None
    n_user = n_asst = 0
    total_tokens = 0
    tin = tout = 0

    for o in objs:
        t = o.get("type")
        ts = o.get("timestamp")
        if ts:
            created = created or ts
            updated = ts
        p = o.get("payload", {}) or {}
        pt = p.get("type")

        if t == "session_meta":
            cwd = p.get("cwd") or cwd
            if p.get("id"):
                sid = p.get("id")
        elif t == "turn_context":
            model = p.get("model") or model
        elif t == "event_msg":
            if pt == "user_message":
                tx = (p.get("message") or "").strip()
                if tx:
                    events.append(_text_ev("user", ts, tx))
                    if not title:
                        title = tx[:90]
                    n_user += 1
            elif pt == "token_count":
                info = p.get("info") or {}
                tu = info.get("total_token_usage") or {}
                total_tokens = tu.get("total_tokens", total_tokens) or total_tokens
                tin = tu.get("input_tokens", tin) or tin
                tout = tu.get("output_tokens", tout) or tout
            elif pt == "task_complete":
                pass
        elif t == "response_item":
            if pt == "message":
                role = p.get("role")
                text = _codex_text(p.get("content"))
                if role == "assistant":
                    if text.strip():
                        events.append(_text_ev("assistant", ts, text, model=model))
                        n_asst += 1
                else:
                    if text.strip():
                        events.append(_text_ev("meta", ts, text, label=role or "kontekst"))
            elif pt == "function_call":
                cid = p.get("call_id")
                out, otr, olen = _trunc(outputs.get(cid), MAX_OUT)
                events.append(ev("tool_call", ts,
                                 tool=p.get("name"),
                                 input=_codex_args(p.get("arguments")),
                                 output=out,
                                 out_trunc=otr or None,
                                 out_len=olen if otr else None,
                                 ok=True))
            elif pt == "reasoning":
                r = _codex_reasoning(p)
                if r:
                    events.append(_text_ev("thinking", ts, r))

    if not title:
        title = "(bez tytulu)"
    if not total_tokens:
        total_tokens = tin + tout

    meta = {
        "tool": "codex",
        "id": sid,
        "title": title,
        "cwd": cwd or "",
        "model": model or "",
        "created": created,
        "updated": updated or _iso_from_mtime(os.path.getmtime(path)),
        "messages": n_user + n_asst,
        "user": n_user,
        "tokens_in": tin,
        "tokens_out": tout,
        "tokens_cache": 0,
        "tokens_total": total_tokens,
        "cost": _est_cost(model, tin, tout),
    }
    return {"meta": meta, "events": events}


def normalize(path):
    tool = "codex" if (os.sep + ".codex" + os.sep) in path or ".codex" in path.lower() else "claude"
    if tool == "codex":
        return normalize_codex(path)
    return normalize_claude(path)


# ============================================================================
#  ODKRYWANIE PLIKOW + LEKKI SKAN METADANYCH (z cache)
# ============================================================================
def discover():
    files = []
    if os.path.isdir(CLAUDE_PROJECTS):
        for root, dirs, fs in os.walk(CLAUDE_PROJECTS):
            if (os.sep + "subagents") in (root + os.sep):
                continue
            for fn in fs:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(root, fn)
                # tylko glowne pliki sesji: projects/<projekt>/<id>.jsonl
                if os.path.dirname(os.path.dirname(path)) == os.path.abspath(CLAUDE_PROJECTS) \
                        or os.path.dirname(os.path.dirname(path)) == CLAUDE_PROJECTS:
                    files.append((path, "claude"))
    if os.path.isdir(CODEX_SESSIONS):
        for root, dirs, fs in os.walk(CODEX_SESSIONS):
            for fn in fs:
                if fn.startswith("rollout-") and fn.endswith(".jsonl"):
                    files.append((os.path.join(root, fn), "codex"))
    return files


def _row_from_meta(path, tool, meta):
    cwd = meta.get("cwd") or ""
    stem = path[:-6] if path.endswith(".jsonl") else path
    subdir = os.path.join(stem, "subagents")
    n_sub = 0
    if os.path.isdir(subdir):
        try:
            n_sub = len([f for f in os.listdir(subdir)
                         if f.startswith("agent-") and f.endswith(".jsonl")])
        except Exception:
            n_sub = 0
    return {
        "tool": tool,
        "path": path,
        "id": meta.get("id"),
        "title": meta.get("title") or "(bez tytulu)",
        "cwd": cwd,
        "folder": os.path.basename(cwd.rstrip("\\/")) if cwd else "(nieznany)",
        "model": meta.get("model") or "",
        "created": meta.get("created"),
        "updated": meta.get("updated"),
        "mtime": os.path.getmtime(path),
        "messages": meta.get("messages", 0),
        "tokens_total": meta.get("tokens_total", 0),
        "tokens_out": meta.get("tokens_out", 0),
        "cost": meta.get("cost"),
        "subagents": n_sub,
    }


CACHE = {}
SCAN = {"sessions": [], "done": False, "total": 0, "scanned": 0,
        "started": False, "lock": threading.Lock()}


def load_cache():
    global CACHE
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            CACHE = json.load(f)
    except Exception:
        CACHE = {}


def save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(CACHE, f)
    except Exception:
        pass


def get_row(path, tool):
    try:
        st = os.stat(path)
    except Exception:
        return None
    c = CACHE.get(path)
    if c and c.get("mtime") == st.st_mtime and c.get("size") == st.st_size:
        return c["row"]
    data = normalize(path)
    row = _row_from_meta(path, tool, data["meta"])
    CACHE[path] = {"mtime": st.st_mtime, "size": st.st_size, "row": row}
    return row


def _sorted(rows):
    return sorted(rows, key=lambda r: (r.get("updated") or "", r.get("mtime") or 0), reverse=True)


def scan_all():
    files = discover()
    with SCAN["lock"]:
        SCAN["total"] = len(files)
        SCAN["scanned"] = 0
        SCAN["done"] = False
    rows = []
    valid = set()
    for i, (p, t) in enumerate(files):
        try:
            r = get_row(p, t)
            if r:
                rows.append(r)
                valid.add(p)
        except Exception:
            pass
        if i % 15 == 0:
            with SCAN["lock"]:
                SCAN["sessions"] = _sorted(rows)
                SCAN["scanned"] = i + 1
    # sprzataj cache po usunietych plikach
    for k in list(CACHE.keys()):
        if k not in valid:
            CACHE.pop(k, None)
    with SCAN["lock"]:
        SCAN["sessions"] = _sorted(rows)
        SCAN["scanned"] = len(files)
        SCAN["done"] = True
    save_cache()


def ensure_scan(force=False):
    with SCAN["lock"]:
        if SCAN["started"] and not force:
            return
        SCAN["started"] = True
        SCAN["done"] = False
    threading.Thread(target=scan_all, daemon=True).start()


# ============================================================================
#  LAUNCHER (terminal / folder / picker)
# ============================================================================
def _q(s):
    s = str(s)
    if s == "" or any(c in s for c in ' \t&()[]{}^=;!+,`~%'):
        return '"' + s.replace('"', '') + '"'
    return s


def build_command(tool, sid, skip, model):
    if tool == "codex":
        p = ["codex"]
        if sid:
            p += ["resume", sid]
        if model:
            p += ["-m", model]
        if skip:
            p += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        p = ["claude"]
        if sid:
            p += ["--resume", sid]
        if model:
            p += ["--model", model]
        if skip:
            p += ["--dangerously-skip-permissions"]
    return p


def launch_terminal(tool, cwd, sid, skip, model):
    parts = build_command(tool, sid, skip, model)
    inner = " ".join(_q(x) for x in parts)
    if not cwd or not os.path.isdir(cwd):
        cwd = HOME
    if sys.platform == "win32":
        if WT_BIN:
            cmd = f'start "" {_q(WT_BIN)} -d {_q(cwd)} cmd /k {inner}'
        else:
            cmd = f'start "" /D {_q(cwd)} cmd /k {inner}'
        subprocess.Popen(cmd, shell=True, cwd=cwd)
    elif sys.platform == "darwin":
        script = f'cd {_q(cwd)} && {inner}'
        subprocess.Popen(["osascript", "-e",
                          f'tell application "Terminal" to do script "{script}"'])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or "xterm"
        subprocess.Popen([term, "-e", "bash", "-lc", f'cd {_q(cwd)}; {inner}; exec bash'])
    return inner, cwd


def open_folder(path):
    if not path or not os.path.isdir(path):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def pick_folder(initial):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        d = filedialog.askdirectory(initialdir=initial if initial and os.path.isdir(initial) else HOME)
        root.destroy()
        return d or ""
    except Exception:
        return None


# ============================================================================
#  SERWER HTTP
# ============================================================================
def _safe_session_path(path):
    if not path:
        return False
    ap = os.path.abspath(path)
    roots = [os.path.abspath(CLAUDE_PROJECTS), os.path.abspath(CODEX_SESSIONS)]
    ok = any(ap == r or ap.startswith(r + os.sep) for r in roots)
    return ok and os.path.isfile(ap)


def session_payload(path):
    data = normalize(path)
    stem = path[:-6] if path.endswith(".jsonl") else path
    subdir = os.path.join(stem, "subagents")
    subs = []
    if os.path.isdir(subdir):
        try:
            for fn in sorted(os.listdir(subdir)):
                if fn.startswith("agent-") and fn.endswith(".jsonl"):
                    subs.append({"path": os.path.join(subdir, fn),
                                 "label": fn[:-6].replace("agent-", "agent ")})
        except Exception:
            pass
    return {"ok": True, "path": path, "meta": data["meta"],
            "events": data["events"], "subagents": subs}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                self._send(200, HTML, "text/html; charset=utf-8")
            elif u.path == "/api/config":
                self._send(200, {
                    "claude": bool(CLAUDE_BIN), "codex": bool(CODEX_BIN),
                    "wt": bool(WT_BIN), "platform": sys.platform,
                    "claudeRoot": CLAUDE_PROJECTS, "codexRoot": CODEX_SESSIONS,
                })
            elif u.path == "/api/sessions":
                ensure_scan()
                with SCAN["lock"]:
                    self._send(200, {"sessions": SCAN["sessions"], "done": SCAN["done"],
                                     "total": SCAN["total"], "scanned": SCAN["scanned"]})
            elif u.path == "/api/refresh":
                ensure_scan(force=True)
                self._send(200, {"ok": True})
            elif u.path == "/api/session":
                path = (qs.get("path") or [""])[0]
                if not _safe_session_path(path):
                    self._send(403, {"ok": False, "error": "niedozwolona sciezka"})
                    return
                self._send(200, session_payload(path))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e), "trace": traceback.format_exc()})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        b = self._body()
        try:
            if u.path == "/api/launch":
                tool = b.get("tool", "claude")
                cwd = b.get("cwd") or ""
                sid = b.get("sessionId") or None
                skip = bool(b.get("skip", True))
                model = b.get("model") or None
                dry = bool(b.get("dryRun"))
                parts = build_command(tool, sid, skip, model)
                inner = " ".join(_q(x) for x in parts)
                full = f'cd /d {_q(cwd)} && {inner}' if cwd else inner
                if dry:
                    self._send(200, {"ok": True, "cmd": inner, "full": full, "cwd": cwd})
                    return
                if tool == "claude" and not CLAUDE_BIN:
                    self._send(200, {"ok": False, "error": "Nie znaleziono 'claude' na PATH",
                                     "cmd": inner})
                    return
                if tool == "codex" and not CODEX_BIN:
                    self._send(200, {"ok": False, "error": "Nie znaleziono 'codex' na PATH",
                                     "cmd": inner})
                    return
                used_inner, used_cwd = launch_terminal(tool, cwd, sid, skip, model)
                self._send(200, {"ok": True, "cmd": used_inner, "full": full, "cwd": used_cwd})
            elif u.path == "/api/open-folder":
                ok = open_folder(b.get("path"))
                self._send(200, {"ok": ok})
            elif u.path == "/api/pick-folder":
                d = pick_folder(b.get("initial"))
                if d is None:
                    self._send(200, {"ok": False, "unsupported": True})
                else:
                    self._send(200, {"ok": True, "path": d})
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e), "trace": traceback.format_exc()})


# ============================================================================
#  FRONTEND (HTML + CSS + JS, wszystko inline)
# ============================================================================
HTML = r'''<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Przegladaczka sesji - Claude Code &amp; Codex</title>
<style>
:root{
  --bg:#0e1015; --panel:#151823; --panel2:#1b1f2b; --panel3:#222736;
  --border:#2a303f; --border2:#363d50;
  --text:#e7e9f0; --dim:#9aa3b6; --dim2:#6b7488;
  --accent:#6ea8fe; --accent2:#5a8de0;
  --claude:#d2785a; --claude-bg:#2a1d18;
  --codex:#19c37d; --codex-bg:#14271f;
  --warn:#e6a35c; --danger:#ef6a6a; --ok:#54c98a;
  --user:#7c8cf8; --user-bg:#1a1d2e;
  --radius:12px; --mono:"Cascadia Code","JetBrains Mono",Consolas,"SF Mono",monospace;
  --ui:"Segoe UI",system-ui,-apple-system,"Inter",sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font-family:var(--ui);font-size:14px;line-height:1.55;overflow:hidden}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:8px;border:3px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#454d63}
button{font-family:inherit;cursor:pointer}
a{color:var(--accent)}

#app{display:flex;flex-direction:column;height:100%}

/* ---- topbar ---- */
.topbar{display:flex;align-items:center;gap:14px;padding:10px 16px;background:var(--panel);
  border-bottom:1px solid var(--border);flex-shrink:0;z-index:5}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:15px;letter-spacing:.2px}
.brand .logo{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--claude),var(--codex));color:#0e1015;font-weight:900}
.brand small{font-weight:500;color:var(--dim);font-size:11px}
.search{flex:1;max-width:520px;position:relative}
.search input{width:100%;padding:8px 12px 8px 34px;background:var(--panel2);border:1px solid var(--border);
  border-radius:9px;color:var(--text);font-size:13px;outline:none}
.search input:focus{border-color:var(--accent2)}
.search .ic{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--dim2)}
.spacer{flex:1}
.btn{display:inline-flex;align-items:center;gap:7px;padding:8px 14px;border-radius:9px;border:1px solid var(--border2);
  background:var(--panel2);color:var(--text);font-size:13px;font-weight:600;transition:.12s}
.btn:hover{background:var(--panel3);border-color:#46506a}
.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#0a0d14}
.btn.primary:hover{filter:brightness(1.07)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--dim);padding:8px 10px}
.btn.ghost:hover{background:var(--panel2);color:var(--text)}
.btn.sm{padding:5px 10px;font-size:12px}

/* ---- layout ---- */
.main{display:flex;flex:1;min-height:0}
#sidebar{width:340px;flex-shrink:0;background:var(--panel);border-right:1px solid var(--border);
  display:flex;flex-direction:column;min-height:0}
.filters{display:flex;gap:6px;padding:10px 12px;border-bottom:1px solid var(--border)}
.chip{padding:6px 12px;border-radius:20px;border:1px solid var(--border2);background:transparent;color:var(--dim);
  font-size:12px;font-weight:600;display:flex;align-items:center;gap:6px}
.chip:hover{color:var(--text)}
.chip.on{background:var(--panel3);color:var(--text);border-color:#46506a}
.chip .dot{width:8px;height:8px;border-radius:50%}
.chip .cnt{color:var(--dim2);font-weight:500}
.scanbar{padding:7px 12px;font-size:11px;color:var(--dim);display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.scanbar .track{flex:1;height:4px;background:var(--panel3);border-radius:3px;overflow:hidden}
.scanbar .fill{height:100%;background:var(--accent);width:0;transition:width .3s}
.list{overflow-y:auto;flex:1;padding:6px}
.group-h{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--dim2);
  padding:12px 10px 5px;display:flex;align-items:center;gap:7px;position:sticky;top:0;background:var(--panel);z-index:1}
.group-h .gc{margin-left:auto;font-weight:600}
.item{padding:9px 10px;border-radius:9px;cursor:pointer;border:1px solid transparent;margin-bottom:2px}
.item:hover{background:var(--panel2)}
.item.on{background:var(--panel3);border-color:var(--border2)}
.item .row1{display:flex;align-items:center;gap:7px;margin-bottom:3px}
.item .tl{font-weight:600;font-size:13px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .row2{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--dim2)}
.item .row2 .r2l{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .row2 .r2r{flex-shrink:0;color:var(--dim2)}
.item .row2 .sep{opacity:.4;margin:0 1px}
.badge{font-size:9.5px;font-weight:800;padding:2px 6px;border-radius:5px;text-transform:uppercase;letter-spacing:.4px;flex-shrink:0}
.badge.claude{background:var(--claude-bg);color:var(--claude)}
.badge.codex{background:var(--codex-bg);color:var(--codex)}

/* ---- view ---- */
#view{flex:1;min-width:0;display:flex;flex-direction:column;background:var(--bg)}
.empty{margin:auto;text-align:center;color:var(--dim);max-width:460px;padding:40px}
.empty .big{font-size:54px;margin-bottom:8px}
.empty h2{color:var(--text);font-weight:700;margin:.2em 0}
.empty .warn{margin-top:22px;padding:12px 14px;border-radius:10px;background:var(--claude-bg);
  border:1px solid #4a3326;color:#e9b08f;font-size:12.5px;text-align:left}

.shead{padding:14px 22px;border-bottom:1px solid var(--border);background:var(--panel);flex-shrink:0}
.shead .t1{display:flex;align-items:center;gap:10px}
.shead h1{font-size:17px;margin:0;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.shead .path{font-size:12px;color:var(--dim);font-family:var(--mono);margin-top:3px;cursor:pointer}
.shead .path:hover{color:var(--accent)}
.metarow{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px;align-items:center}
.pill{font-size:11px;color:var(--dim);background:var(--panel2);border:1px solid var(--border);
  padding:3px 9px;border-radius:7px;display:inline-flex;gap:5px;align-items:center}
.pill b{color:var(--text);font-weight:600}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:13px;align-items:center}
.skipwrap{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--warn);
  padding:6px 11px;border:1px solid #4a3a22;border-radius:9px;background:#241c12;user-select:none;cursor:pointer}
.skipwrap input{accent-color:var(--warn);width:15px;height:15px}
.toolbar{display:flex;gap:8px;margin-left:auto;align-items:center}

/* ---- transcript ---- */
.stream{overflow-y:auto;flex:1;padding:22px 0}
.wrap{max-width:900px;margin:0 auto;padding:0 22px}
.turn{margin-bottom:18px}
.who{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;text-transform:uppercase;
  letter-spacing:.5px;color:var(--dim2);margin-bottom:6px}
.who .av{width:20px;height:20px;border-radius:6px;display:grid;place-items:center;font-size:11px;color:#0e1015;font-weight:900}
.msg{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:13px 16px;overflow:hidden}
.msg.user{background:var(--user-bg);border-color:#2c3354}
.msg.user .who{color:var(--user)}
.bubble :first-child{margin-top:0}.bubble :last-child{margin-bottom:0}
.bubble p{margin:.5em 0}
.bubble h1,.bubble h2,.bubble h3,.bubble h4{margin:.7em 0 .35em;line-height:1.3}
.bubble h1{font-size:1.35em}.bubble h2{font-size:1.2em}.bubble h3{font-size:1.07em}
.bubble ul,.bubble ol{margin:.4em 0;padding-left:1.5em}
.bubble li{margin:.15em 0}
.bubble blockquote{margin:.5em 0;padding:.2em 0 .2em 12px;border-left:3px solid var(--border2);color:var(--dim)}
.bubble hr{border:none;border-top:1px solid var(--border);margin:1em 0}
.bubble code{font-family:var(--mono);font-size:.88em;background:var(--panel3);padding:1.5px 5px;border-radius:5px}
.bubble table{border-collapse:collapse;margin:.5em 0;font-size:.92em}
.bubble th,.bubble td{border:1px solid var(--border2);padding:5px 9px}
.bubble th{background:var(--panel2)}
.codeblock{margin:.6em 0;border:1px solid var(--border);border-radius:9px;overflow:hidden;background:#0c0e14}
.codeblock .cbh{display:flex;align-items:center;justify-content:space-between;padding:5px 11px;
  background:var(--panel2);font-size:11px;color:var(--dim);font-family:var(--mono)}
.codeblock .cbh .cp{cursor:pointer;color:var(--dim)}
.codeblock .cbh .cp:hover{color:var(--text)}
.codeblock pre{margin:0;padding:12px 14px;overflow-x:auto;font-family:var(--mono);font-size:12.5px;line-height:1.5}
.tok-k{color:#c98af0}.tok-s{color:#9ad17a}.tok-c{color:#6b7488;font-style:italic}.tok-n{color:#e6a35c}.tok-f{color:#6ea8fe}

/* thinking / tools / meta = details */
details.blk{margin:7px 0;border:1px solid var(--border);border-radius:10px;background:var(--panel);overflow:hidden}
details.blk>summary{list-style:none;cursor:pointer;padding:9px 13px;display:flex;align-items:center;gap:9px;
  font-size:12.5px;user-select:none}
details.blk>summary::-webkit-details-marker{display:none}
details.blk>summary:hover{background:var(--panel2)}
details.blk>summary .chev{color:var(--dim2);transition:transform .15s;font-size:10px}
details.blk[open]>summary .chev{transform:rotate(90deg)}
details.blk .body{padding:11px 14px;border-top:1px solid var(--border);font-size:13px}
details.think>summary{color:#b79df0}
details.think{border-color:#312a4a;background:#181527}
details.tool{border-color:#26303a}
details.tool>summary .tn{font-weight:700;font-family:var(--mono);font-size:12px}
details.tool>summary .ts{color:var(--dim);font-family:var(--mono);font-size:11.5px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;flex:1}
.sdot{width:8px;height:8px;border-radius:50%;background:var(--dim2);flex-shrink:0}
.sdot.ok{background:var(--ok)}.sdot.err{background:var(--danger)}
.tico{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;background:var(--panel3);font-size:12px;flex-shrink:0}
details.meta{border-color:var(--border);background:transparent;opacity:.8}
details.meta>summary{color:var(--dim2);font-size:11.5px}
.hide-meta details.meta,.hide-meta .ev-meta{display:none}
.tout{margin-top:9px;background:#0c0e14;border:1px solid var(--border);border-radius:8px;padding:10px 12px;
  font-family:var(--mono);font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:420px;overflow:auto}
.tin{background:#0c0e14;border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-family:var(--mono);
  font-size:12px;white-space:pre-wrap;word-break:break-word}
.tlabel{font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim2);font-weight:700;margin:8px 0 4px}
.trunc-note{color:var(--warn);font-size:11px;margin-top:6px}
.imgwrap{margin:8px 0}
.imgwrap img{max-width:100%;max-height:520px;border-radius:9px;border:1px solid var(--border);display:block}
.ev-img{margin:8px 0}
.ev-meta.sys{font-size:11px;color:var(--dim2);text-align:center;margin:10px 0;font-family:var(--mono)}
.subhead{margin:26px 0 10px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);
  display:flex;align-items:center;gap:8px}
.subhead::before,.subhead::after{content:"";flex:1;height:1px;background:var(--border)}

/* ---- modal ---- */
.overlay{position:fixed;inset:0;background:rgba(6,8,12,.66);display:none;align-items:center;justify-content:center;z-index:50;backdrop-filter:blur(3px)}
.overlay.on{display:flex}
.modal{background:var(--panel);border:1px solid var(--border2);border-radius:16px;width:min(560px,92vw);
  padding:22px 24px;box-shadow:0 24px 80px rgba(0,0,0,.6)}
.modal h2{margin:0 0 4px;font-size:17px}
.modal .sub{color:var(--dim);font-size:12.5px;margin-bottom:16px}
.field{margin-bottom:14px}
.field label{display:block;font-size:12px;font-weight:600;color:var(--dim);margin-bottom:6px}
.field input,.field select{width:100%;padding:9px 11px;background:var(--panel2);border:1px solid var(--border2);
  border-radius:9px;color:var(--text);font-size:13px;outline:none}
.field input:focus,.field select:focus{border-color:var(--accent2)}
.toolsel{display:flex;gap:9px}
.toolsel .opt{flex:1;padding:11px;border:1px solid var(--border2);border-radius:10px;text-align:center;font-weight:700;color:var(--dim)}
.toolsel .opt.on{color:var(--text);border-width:2px}
.toolsel .opt.claude.on{border-color:var(--claude);background:var(--claude-bg)}
.toolsel .opt.codex.on{border-color:var(--codex);background:var(--codex-bg)}
.foldrow{display:flex;gap:8px}
.foldrow input{flex:1}
.modal .ft{display:flex;gap:9px;justify-content:flex-end;margin-top:20px}
.cmdprev{font-family:var(--mono);font-size:11.5px;background:#0c0e14;border:1px solid var(--border);border-radius:8px;
  padding:9px 11px;color:var(--codex);margin-top:6px;word-break:break-all}

/* ---- toast ---- */
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(40px);opacity:0;
  background:var(--panel3);border:1px solid var(--border2);border-radius:10px;padding:11px 18px;font-size:13px;
  z-index:99;transition:.25s;max-width:80vw;box-shadow:0 10px 40px rgba(0,0,0,.5)}
#toast.on{transform:translateX(-50%) translateY(0);opacity:1}
#toast.err{border-color:var(--danger);color:#ffb4b4}
#toast.ok{border-color:var(--ok)}
.mut{color:var(--dim)}
.kbd{font-family:var(--mono);font-size:11px;background:var(--panel3);border:1px solid var(--border2);
  border-radius:5px;padding:1px 6px;color:var(--dim)}
</style>
</head>
<body>
<div id="app">
  <header class="topbar">
    <div class="brand"><div class="logo">S</div>
      <div>Przegladaczka sesji<br><small>Claude Code &amp; Codex - panel sterowania</small></div>
    </div>
    <div class="search"><span class="ic">&#128269;</span>
      <input id="q" placeholder="Szukaj: tytul, folder, model..." autocomplete="off"></div>
    <div class="spacer"></div>
    <button class="btn ghost sm" id="refresh" title="Odswiez liste">&#8635;</button>
    <button class="btn primary" id="newbtn">+ Nowa sesja</button>
  </header>

  <div class="main">
    <aside id="sidebar">
      <div class="filters" id="filters"></div>
      <div class="scanbar" id="scanbar" style="display:none">
        <span id="scantxt">Skanuje...</span>
        <div class="track"><div class="fill" id="scanfill"></div></div>
      </div>
      <div class="list" id="list"></div>
    </aside>

    <section id="view">
      <div class="empty" id="empty">
        <div class="big">&#128202;</div>
        <h2>Wybierz sesje z listy</h2>
        <div>Po lewej masz wszystkie sesje Claude Code i Codex, pogrupowane po folderze.
             Kliknij, zeby zobaczyc cala rozmowe - albo <b>wznow ja jednym klikiem</b> w nowym terminalu.</div>
        <div class="warn">&#9888;&#65039; <b>Uwaga:</b> wznawianie i nowe sesje startuja domyslnie z
          <b>pominieciem uprawnien</b> (Claude: <span class="kbd">--dangerously-skip-permissions</span>,
          Codex: <span class="kbd">--dangerously-bypass-approvals-and-sandbox</span>).
          Mozesz to odznaczyc przy kazdym uruchomieniu.</div>
      </div>
    </section>
  </div>
</div>

<!-- modal nowej sesji -->
<div class="overlay" id="overlay">
  <div class="modal">
    <h2>Nowa sesja</h2>
    <div class="sub">Otworzy sie nowy terminal w wybranym folderze i odpali narzedzie.</div>
    <div class="field"><label>Narzedzie</label>
      <div class="toolsel" id="toolsel">
        <div class="opt claude on" data-tool="claude">Claude Code</div>
        <div class="opt codex" data-tool="codex">Codex</div>
      </div>
    </div>
    <div class="field"><label>Folder roboczy</label>
      <div class="foldrow">
        <input id="nfolder" list="folders" placeholder="C:\sciezka\do\projektu">
        <button class="btn sm" id="browse">Przegladaj...</button>
      </div>
      <datalist id="folders"></datalist>
    </div>
    <div class="field"><label>Model (opcjonalnie)</label>
      <input id="nmodel" placeholder="np. opus / sonnet / gpt-5.5 - puste = domyslny"></div>
    <div class="field">
      <label class="skipwrap" style="display:inline-flex"><input type="checkbox" id="nskip" checked>
        Uruchom z pominieciem uprawnien (dangerously skip)</label>
      <div class="cmdprev" id="ncmd"></div>
    </div>
    <div class="ft">
      <button class="btn" id="ncancel">Anuluj</button>
      <button class="btn primary" id="nlaunch">&#9654; Uruchom</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const $ = (s,r=document)=>r.querySelector(s);
const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
const state = {sessions:[], filter:"all", q:"", current:null, cfg:{},
  skip: JSON.parse(localStorage.getItem("skip") ?? "true"),
  newTool:"claude"};

function esc(s){return String(s==null?"":s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function fmtNum(n){n=n||0;if(n>=1e6)return (n/1e6).toFixed(1)+"M";if(n>=1e3)return (n/1e3).toFixed(1)+"k";return ""+n;}
function fmtDate(iso){
  if(!iso)return "";
  const d=new Date(iso); if(isNaN(d))return "";
  const now=new Date(), diff=(now-d)/1000;
  if(diff<60)return "teraz"; if(diff<3600)return Math.floor(diff/60)+" min temu";
  if(diff<86400)return Math.floor(diff/3600)+" godz temu";
  if(diff<7*86400)return Math.floor(diff/86400)+" dni temu";
  return d.toLocaleDateString("pl-PL",{day:"2-digit",month:"2-digit",year:"2-digit"});
}
function toast(msg,kind=""){const t=$("#toast");t.textContent=msg;t.className="on "+kind;
  clearTimeout(t._t);t._t=setTimeout(()=>t.className="",2600);}

/* ---------- markdown ---------- */
const KW=new Set(("const let var function return if else for while do switch case break continue class new "+
 "import from export default async await try catch finally throw def lambda print self None True False and or not "+
 "in is with as pass yield raise elif fn pub use struct impl match enum mut let type interface public private "+
 "static void int float double bool string echo end then fi done local export").split(" "));
function hl(code){
  // code juz zescapowany (&<>); podswietl stringi, komentarze, liczby, slowa kluczowe
  try{
    const out=[]; let i=0; const n=code.length;
    const push=(t,c)=>out.push(c?`<span class="${c}">${t}</span>`:t);
    while(i<n){
      const ch=code[i], two=code.substr(i,2);
      if(ch==='"'||ch==="'"||ch==="`"){let j=i+1;while(j<n&&code[j]!==ch){if(code[j]==="\\")j++;j++;}push(code.slice(i,j+1),"tok-s");i=j+1;continue;}
      if(two==="//"||ch==="#"){let j=i;while(j<n&&code[j]!=="\n")j++;push(code.slice(i,j),"tok-c");i=j;continue;}
      if(two==="/*"){let j=code.indexOf("*/",i);if(j<0)j=n;else j+=2;push(code.slice(i,j),"tok-c");i=j;continue;}
      if(/[A-Za-z_$]/.test(ch)){let j=i;while(j<n&&/[A-Za-z0-9_$]/.test(code[j]))j++;const w=code.slice(i,j);
        push(w,KW.has(w)?"tok-k":(code[j]==="("?"tok-f":""));i=j;continue;}
      if(/[0-9]/.test(ch)){let j=i;while(j<n&&/[0-9.xXa-fA-F]/.test(code[j]))j++;push(code.slice(i,j),"tok-n");i=j;continue;}
      // przepisz znaki, w tym encje &...; w calosci
      if(ch==="&"){let j=code.indexOf(";",i);if(j>=0&&j-i<8){push(code.slice(i,j+1));i=j+1;continue;}}
      push(ch);i++;
    }
    return out.join("");
  }catch(e){return code;}
}
let _cb=[];
function md(src){
  if(src==null)return "";
  src=String(src); _cb=[]; const S=String.fromCharCode(0xF8FF);
  // bloki kodu ```
  src=src.replace(/```([\w+\-.]*)\n?([\s\S]*?)```/g,(m,lang,code)=>{
    const i=_cb.length;_cb.push({lang:lang||"",code:code.replace(/\n$/,"")});return "\n"+S+"B"+i+S+"\n";});
  // inline code
  const inl=[];
  src=src.replace(/`([^`\n]+)`/g,(m,c)=>{const i=inl.length;inl.push(c);return S+"I"+i+S;});
  src=esc(src);
  // tabele (proste)
  src=src.replace(/(^\|.+\|\s*\n\|[ :|\-]+\|\s*\n(?:\|.*\|\s*\n?)*)/gm,tbl=>{
    const rows=tbl.trim().split("\n").filter(r=>r.trim());
    if(rows.length<2)return tbl;
    const cells=r=>r.replace(/^\||\|$/g,"").split("|").map(x=>x.trim());
    let h="<table><thead><tr>"+cells(rows[0]).map(c=>`<th>${c}</th>`).join("")+"</tr></thead><tbody>";
    for(let k=2;k<rows.length;k++)h+="<tr>"+cells(rows[k]).map(c=>`<td>${c}</td>`).join("")+"</tr>";
    return h+"</tbody></table>";
  });
  const lines=src.split("\n"); let out=[]; let i2=0;
  function inline(t){
    t=t.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,'<img src="$2" alt="$1" style="max-width:100%">');
    t=t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
    t=t.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
    t=t.replace(/(^|[^*\w])\*([^*\n]+)\*/g,"$1<em>$2</em>");
    t=t.replace(/(^|[^_\w])_([^_\n]+)_/g,"$1<em>$2</em>");
    t=t.replace(/~~([^~]+)~~/g,"<del>$1</del>");
    return t;
  }
  while(i2<lines.length){
    let ln=lines[i2];
    if(new RegExp("^\\s*"+S+"B\\d+"+S+"\\s*$").test(ln)){out.push(ln.trim());i2++;continue;}
    if(/^\s*$/.test(ln)){i2++;continue;}
    let hm=ln.match(/^(#{1,6})\s+(.*)$/);
    if(hm){out.push(`<h${hm[1].length}>${inline(hm[2])}</h${hm[1].length}>`);i2++;continue;}
    if(/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(ln)){out.push("<hr>");i2++;continue;}
    if(/^\s*&gt;/.test(ln)){let q=[];while(i2<lines.length&&/^\s*&gt;/.test(lines[i2])){q.push(lines[i2].replace(/^\s*&gt;\s?/,""));i2++;}
      out.push("<blockquote>"+inline(q.join("<br>"))+"</blockquote>");continue;}
    if(/^\s*[-*+]\s+/.test(ln)){let it=[];while(i2<lines.length&&/^\s*[-*+]\s+/.test(lines[i2])){it.push(lines[i2].replace(/^\s*[-*+]\s+/,""));i2++;}
      out.push("<ul>"+it.map(x=>`<li>${inline(x)}</li>`).join("")+"</ul>");continue;}
    if(/^\s*\d+[.)]\s+/.test(ln)){let it=[];while(i2<lines.length&&/^\s*\d+[.)]\s+/.test(lines[i2])){it.push(lines[i2].replace(/^\s*\d+[.)]\s+/,""));i2++;}
      out.push("<ol>"+it.map(x=>`<li>${inline(x)}</li>`).join("")+"</ol>");continue;}
    if(/^<(table|h\d|ul|ol|blockquote|hr)/.test(ln)){out.push(ln);i2++;continue;}
    let para=[ln];i2++;
    while(i2<lines.length&&!/^\s*$/.test(lines[i2])&&!new RegExp("^(?:#{1,6}\\s|\\s*[-*+]\\s|\\s*\\d+[.)]\\s|\\s*&gt;|<(?:table|h\\d|ul|ol)|\\s*"+S+"B)").test(lines[i2])){para.push(lines[i2]);i2++;}
    out.push("<p>"+inline(para.join("<br>"))+"</p>");
  }
  src=out.join("\n");
  src=src.replace(new RegExp(S+"I(\\d+)"+S,"g"),(m,i)=>"<code>"+esc(inl[+i])+"</code>");
  src=src.replace(new RegExp(S+"B(\\d+)"+S,"g"),(m,i)=>{
    const b=_cb[+i];const id="cb"+Math.random().toString(36).slice(2,8);
    return `<div class="codeblock"><div class="cbh"><span>${esc(b.lang||"kod")}</span>`+
      `<span class="cp" data-copy="${id}">&#128203; kopiuj</span></div>`+
      `<pre id="${id}"><code>${hl(esc(b.code))}</code></pre></div>`;});
  return src;
}

/* ---------- narzedzia: ikony i podsumowania ---------- */
function toolIcon(name){const n=(name||"").toLowerCase();
  if(/bash|shell|exec|command|terminal/.test(n))return "&#9002;_";
  if(/write|create/.test(n))return "&#9999;&#65039;";
  if(/edit|patch|apply/.test(n))return "&#9998;";
  if(/read|cat|view/.test(n))return "&#128196;";
  if(/glob|grep|search|find|ls/.test(n))return "&#128269;";
  if(/task|agent/.test(n))return "&#129302;";
  if(/web|fetch|http|url|browser/.test(n))return "&#127760;";
  if(/plan|todo/.test(n))return "&#128221;";
  if(/question|ask/.test(n))return "&#10067;";
  return "&#128295;";}
function firstStr(...xs){for(const x of xs)if(typeof x==="string"&&x.trim())return x;
  for(const x of xs)if(Array.isArray(x))return x.map(String).join(" ");return "";}
function toolSummary(name,inp){inp=inp||{};const n=(name||"").toLowerCase();
  if(/bash|shell|exec|command/.test(n))return firstStr(inp.command,inp.cmd,inp.script).split("\n")[0];
  if(/read|write|edit/.test(n))return firstStr(inp.file_path,inp.path,inp.filePath,inp.file);
  if(/glob|grep|search/.test(n))return firstStr(inp.pattern,inp.query,inp.q,inp.regex);
  if(/task|agent/.test(n))return firstStr(inp.description,inp.subagent_type,inp.prompt);
  if(/web|fetch/.test(n))return firstStr(inp.url,inp.query,inp.prompt);
  const keys=Object.keys(inp);if(keys.length===1&&typeof inp[keys[0]]==="string")return inp[keys[0]].split("\n")[0];
  return keys.length?JSON.stringify(inp).slice(0,140):"";}
function toolInputHtml(name,inp){inp=inp||{};const n=(name||"").toLowerCase();
  if(inp._raw)return `<div class="tin">${esc(inp._raw)}</div>`;
  const bash=firstStr(inp.command,inp.cmd,inp.script);
  if(/bash|shell|exec|command/.test(n)&&bash){
    let extra="";const wd=inp.workdir||inp.cwd;if(wd)extra=`<div class="tlabel">katalog</div><div class="tin">${esc(wd)}</div>`;
    return `<div class="tin">${esc(bash)}</div>${extra}`;}
  if(/edit|patch/.test(n)&&(inp.old_string!=null||inp.new_string!=null)){
    return `<div class="tlabel">- bylo</div><div class="tin" style="border-left:3px solid var(--danger)">${esc(inp.old_string||"")}</div>`+
           `<div class="tlabel">+ jest</div><div class="tin" style="border-left:3px solid var(--ok)">${esc(inp.new_string||"")}</div>`;}
  if(/write/.test(n)&&inp.content!=null){
    return `<div class="tlabel">${esc(inp.file_path||inp.path||"")}</div><div class="tin">${esc(String(inp.content).slice(0,4000))}</div>`;}
  const keys=Object.keys(inp);if(!keys.length)return "";
  return `<div class="tin">${esc(JSON.stringify(inp,null,2))}</div>`;}

/* ---------- render eventow ---------- */
function avatar(role){
  if(role==="user")return `<span class="av" style="background:var(--user)">&#128100;</span>`;
  return `<span class="av" style="background:linear-gradient(135deg,var(--claude),var(--codex))">&#10022;</span>`;}
function renderEvent(e){
  if(e.t==="user"||e.t==="assistant"){
    const who=e.t==="user"?"Ty":(e.agent?`Asystent &middot; ${esc(e.agent)}`:"Asystent");
    let h=`<div class="turn"><div class="msg ${e.t}"><div class="who">${avatar(e.t)}${who}`;
    if(e.model)h+=`<span style="margin-left:auto;font-weight:500;text-transform:none;color:var(--dim2)">${esc(e.model)}</span>`;
    h+=`</div><div class="bubble">${md(e.text)}</div>`;
    if(e.text_trunc)h+=`<div class="trunc-note">&#9986; tekst przyciety</div>`;
    return h+`</div></div>`;}
  if(e.t==="thinking"){
    return `<details class="blk think"><summary><span class="chev">&#9654;</span>&#128173; Myslenie${e.agent?" &middot; "+esc(e.agent):""}</summary><div class="body bubble">${md(e.text)}</div></details>`;}
  if(e.t==="tool_call"){
    const sum=toolSummary(e.tool,e.input);
    const dot=e.ok===false?"err":(e.ok?"ok":"");
    let h=`<details class="blk tool"><summary><span class="chev">&#9654;</span><span class="tico">${toolIcon(e.tool)}</span>`+
      `<span class="tn">${esc(e.tool||"narzedzie")}</span><span class="ts">${esc(sum)}</span><span class="sdot ${dot}"></span></summary><div class="body">`;
    const inH=toolInputHtml(e.tool,e.input);if(inH)h+=`<div class="tlabel">wejscie</div>${inH}`;
    if(e.images)for(const im of e.images)h+=`<div class="imgwrap"><img src="${im}"></div>`;
    if(e.output!=null){h+=`<div class="tlabel">wynik</div><div class="tout">${esc(e.output)}</div>`;
      if(e.out_trunc)h+=`<div class="trunc-note">&#9986; wynik przyciety (${fmtNum(e.out_len)} znakow)</div>`;}
    return h+`</div></details>`;}
  if(e.t==="image"){
    return `<div class="turn ev-img"><div class="who">${avatar(e.who||"user")}${e.who==="assistant"?"Asystent":"Ty"} &middot; obraz</div>`+
      `<div class="imgwrap"><img src="${e.img}"></div></div>`;}
  if(e.t==="system"){
    return `<div class="ev-meta sys">&#9201; ${esc(e.text)}</div>`;}
  if(e.t==="meta"){
    return `<details class="blk meta"><summary><span class="chev">&#9654;</span>${esc(e.label||"systemowe")}</summary><div class="body bubble">${md(e.text)}</div></details>`;}
  return "";
}

/* ---------- sidebar ---------- */
function counts(){const c={all:state.sessions.length,claude:0,codex:0};
  for(const s of state.sessions)c[s.tool]++;return c;}
function renderFilters(){const c=counts();
  $("#filters").innerHTML=[
    ["all","Wszystkie","",c.all],["claude","Claude","var(--claude)",c.claude],["codex","Codex","var(--codex)",c.codex]
  ].map(([k,lbl,col,n])=>`<button class="chip ${state.filter===k?"on":""}" data-f="${k}">`+
    (col?`<span class="dot" style="background:${col}"></span>`:"")+`${lbl} <span class="cnt">${n}</span></button>`).join("");
  $$("#filters .chip").forEach(b=>b.onclick=()=>{state.filter=b.dataset.f;renderFilters();renderList();});}
function matches(s){const q=state.q.toLowerCase();
  if(state.filter!=="all"&&s.tool!==state.filter)return false;
  if(!q)return true;
  return (s.title+" "+s.folder+" "+s.cwd+" "+s.model).toLowerCase().includes(q);}
function timeBucket(iso){
  if(!iso)return {k:"x",label:"Nieznana data"};
  const d=new Date(iso);if(isNaN(d))return {k:"x",label:"Nieznana data"};
  const now=new Date();
  const st=new Date(now.getFullYear(),now.getMonth(),now.getDate());
  const sy=new Date(st);sy.setDate(sy.getDate()-1);
  const s7=new Date(st);s7.setDate(s7.getDate()-6);
  const s30=new Date(st);s30.setDate(s30.getDate()-29);
  if((now-d)/60000<60)return {k:"h",label:"Ostatnia godzina"};
  if(d>=st)return {k:"t",label:"Dzisiaj"};
  if(d>=sy)return {k:"y",label:"Wczoraj"};
  if(d>=s7)return {k:"w",label:"Ostatnie 7 dni"};
  if(d>=s30)return {k:"m",label:"Ostatnie 30 dni"};
  const l=d.toLocaleDateString("pl-PL",{month:"long",year:"numeric"});
  return {k:"ym"+d.getFullYear()+"."+d.getMonth(),label:l.charAt(0).toUpperCase()+l.slice(1)};
}
function renderList(){
  const list=$("#list");const items=state.sessions.filter(matches);
  if(!items.length){list.innerHTML=`<div style="padding:30px;text-align:center;color:var(--dim2)">Brak sesji${state.q?" dla \""+esc(state.q)+"\"":""}.</div>`;return;}
  // sesje sa juz posortowane malejaco po 'updated' -> kubelki czasowe sa ciagle
  const buckets=[];let cur=null;
  for(const s of items){const b=timeBucket(s.updated);
    if(!cur||cur.k!==b.k){cur={k:b.k,label:b.label,arr:[]};buckets.push(cur);}
    cur.arr.push(s);}
  let h="";
  for(const g of buckets){
    h+=`<div class="group-h">&#128337; ${esc(g.label)}<span class="gc">${g.arr.length}</span></div>`;
    for(const s of g.arr){
      const model=s.model?esc(s.model.replace(/^claude-/,"").replace(/-\d{8}$/,"")):"";
      const parts=[`&#128193; ${esc(s.folder||"?")}`];
      if(model)parts.push(model);
      parts.push(`${s.messages} wiad.`,`${fmtNum(s.tokens_total)} tok`);
      if(s.subagents)parts.push(`&#129302;${s.subagents}`);
      const left=parts.join(' <span class="sep">&middot;</span> ');
      h+=`<div class="item ${state.current&&state.current.path===s.path?"on":""}" data-path="${esc(s.path)}" title="${esc(s.cwd||"")}">`+
        `<div class="row1"><span class="badge ${s.tool}">${s.tool}</span><span class="tl">${esc(s.title)}</span></div>`+
        `<div class="row2"><span class="r2l">${left}</span><span class="r2r">${fmtDate(s.updated)}</span></div></div>`;}
  }
  list.innerHTML=h;
  $$("#list .item").forEach(it=>it.onclick=()=>openSession(it.dataset.path));
}

/* ---------- otwieranie sesji ---------- */
async function openSession(path,isSub){
  try{
    const r=await fetch("/api/session?path="+encodeURIComponent(path));
    const d=await r.json();
    if(!d.ok){toast(d.error||"Blad wczytywania","err");return;}
    if(!isSub){state.current=d;renderList();}
    renderSession(d);
  }catch(e){toast("Blad: "+e.message,"err");}
}
function renderSession(d){
  const m=d.meta;const skip=state.skip;
  const cmd=buildCmd(m.tool,m.id,skip,m.model);
  const pills=[];
  if(m.model)pills.push(`<span class="pill">&#129504; <b>${esc(m.model)}</b></span>`);
  pills.push(`<span class="pill">&#128172; <b>${m.messages}</b> wiadomosci</span>`);
  pills.push(`<span class="pill" title="wejscie ${fmtNum(m.tokens_in)} / wyjscie ${fmtNum(m.tokens_out)}">&#127993; <b>${fmtNum(m.tokens_total)}</b> tok</span>`);
  if(m.cost)pills.push(`<span class="pill">&#8776; <b>$${m.cost}</b></span>`);
  if(m.created)pills.push(`<span class="pill">&#128197; ${esc(new Date(m.created).toLocaleString("pl-PL"))}</span>`);
  let subBtns="";
  if(d.subagents&&d.subagents.length){
    subBtns=`<div class="subhead">&#129302; Subagenci (${d.subagents.length})</div><div id="subs"></div>`;}
  $("#view").innerHTML=`
    <div class="shead">
      <div class="t1"><span class="badge ${m.tool}">${m.tool}</span><h1 title="${esc(m.title)}">${esc(m.title)}</h1></div>
      <div class="path" id="cwdpath" title="Kliknij, by skopiowac">&#128193; ${esc(m.cwd||"(nieznany folder)")}</div>
      <div class="metarow">${pills.join("")}</div>
      <div class="actions">
        <button class="btn primary" id="resume">&#9654; Wznow w terminalu</button>
        <button class="btn" id="copycmd">&#128203; Kopiuj polecenie</button>
        <button class="btn" id="openf">&#128193; Otworz folder</button>
        <label class="skipwrap" id="skiplbl"><input type="checkbox" id="skipchk" ${skip?"checked":""}>
          skip permissions</label>
        <div class="toolbar">
          <button class="btn ghost sm" id="togglemeta">Pokaz systemowe</button>
          <button class="btn ghost sm" id="expand">Rozwin wszystko</button>
        </div>
      </div>
    </div>
    <div class="stream hide-meta" id="stream"><div class="wrap" id="wrap"></div></div>`;
  const wrap=$("#wrap");
  wrap.innerHTML=d.events.map(renderEvent).join("")+subBtns;
  if(d.subagents&&d.subagents.length){
    $("#subs").innerHTML=d.subagents.map((s,i)=>
      `<details class="blk meta" data-sp="${esc(s.path)}"><summary><span class="chev">&#9654;</span>&#129302; ${esc(s.label)}</summary><div class="body" id="sub${i}">...</div></details>`).join("");
    $$("#subs details").forEach((det,i)=>{det.addEventListener("toggle",async()=>{
      if(det.open&&!det._loaded){det._loaded=true;
        const r=await fetch("/api/session?path="+encodeURIComponent(det.dataset.sp));const sd=await r.json();
        $("#sub"+i).innerHTML=sd.ok?sd.events.map(renderEvent).join(""):"(blad)";bindCopies($("#sub"+i));}
    });});
  }
  bindCopies(wrap);
  // akcje
  $("#cwdpath").onclick=()=>{navigator.clipboard.writeText(m.cwd||"");toast("Skopiowano sciezke");};
  $("#skipchk").onchange=e=>{state.skip=e.target.checked;localStorage.setItem("skip",JSON.stringify(state.skip));renderSession(d);};
  $("#resume").onclick=()=>doLaunch(m.tool,m.cwd,m.id,$("#skipchk").checked,m.model);
  $("#copycmd").onclick=async()=>{const c=await fetch("/api/launch",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({tool:m.tool,cwd:m.cwd,sessionId:m.id,skip:$("#skipchk").checked,model:m.model,dryRun:true})}).then(r=>r.json());
    navigator.clipboard.writeText(c.full||c.cmd);toast("Skopiowano: "+(c.cmd||""),"ok");};
  $("#openf").onclick=async()=>{const r=await fetch("/api/open-folder",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:m.cwd})}).then(r=>r.json());toast(r.ok?"Otwarto folder":"Nie udalo sie otworzyc folderu",r.ok?"ok":"err");};
  let metaOn=false;$("#togglemeta").onclick=()=>{metaOn=!metaOn;$("#stream").classList.toggle("hide-meta",!metaOn);
    $("#togglemeta").textContent=metaOn?"Ukryj systemowe":"Pokaz systemowe";};
  let exp=false;$("#expand").onclick=()=>{exp=!exp;$$("#stream details.blk").forEach(x=>x.open=exp);
    $("#expand").textContent=exp?"Zwin wszystko":"Rozwin wszystko";};
}
function bindCopies(root){$$(".cp",root).forEach(c=>c.onclick=()=>{
  const el=document.getElementById(c.dataset.copy);if(el){navigator.clipboard.writeText(el.textContent);toast("Skopiowano kod","ok");}});}

/* ---------- launcher ---------- */
function buildCmd(tool,sid,skip,model){
  let p=tool==="codex"?["codex"]:["claude"];
  if(tool==="codex"){if(sid)p.push("resume",sid);if(model)p.push("-m",model);if(skip)p.push("--dangerously-bypass-approvals-and-sandbox");}
  else{if(sid)p.push("--resume",sid);if(model)p.push("--model",model);if(skip)p.push("--dangerously-skip-permissions");}
  return p.join(" ");
}
async function doLaunch(tool,cwd,sid,skip,model){
  toast("Uruchamiam terminal...");
  try{
    const r=await fetch("/api/launch",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tool,cwd,sessionId:sid,skip,model})}).then(r=>r.json());
    if(r.ok)toast("&#9654; Terminal otwarty: "+r.cmd,"ok");
    else toast(r.error||"Nie udalo sie uruchomic","err");
  }catch(e){toast("Blad: "+e.message,"err");}
}

/* ---------- nowa sesja (modal) ---------- */
function openModal(){
  const dl=$("#folders");const seen=new Set();
  dl.innerHTML=state.sessions.map(s=>s.cwd).filter(c=>c&&!seen.has(c)&&seen.add(c)).map(c=>`<option value="${esc(c)}">`).join("");
  if(state.current&&!$("#nfolder").value)$("#nfolder").value=state.current.meta.cwd||"";
  $("#nskip").checked=state.skip;updateNcmd();$("#overlay").classList.add("on");$("#nfolder").focus();
}
function updateNcmd(){const tool=state.newTool;const model=$("#nmodel").value.trim();
  $("#ncmd").textContent="> "+buildCmd(tool,null,$("#nskip").checked,model);}
function setupModal(){
  $("#newbtn").onclick=openModal;
  $("#ncancel").onclick=()=>$("#overlay").classList.remove("on");
  $("#overlay").onclick=e=>{if(e.target.id==="overlay")$("#overlay").classList.remove("on");};
  $$("#toolsel .opt").forEach(o=>o.onclick=()=>{state.newTool=o.dataset.tool;
    $$("#toolsel .opt").forEach(x=>x.classList.toggle("on",x===o));updateNcmd();});
  $("#nmodel").oninput=updateNcmd;$("#nskip").onchange=updateNcmd;
  $("#browse").onclick=async()=>{
    const r=await fetch("/api/pick-folder",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({initial:$("#nfolder").value})}).then(r=>r.json());
    if(r.ok&&r.path)$("#nfolder").value=r.path;
    else if(r.unsupported)toast("Wpisz sciezke recznie (okno wyboru niedostepne)","err");};
  $("#nlaunch").onclick=async()=>{const cwd=$("#nfolder").value.trim();
    if(!cwd){toast("Podaj folder","err");return;}
    await doLaunch(state.newTool,cwd,null,$("#nskip").checked,$("#nmodel").value.trim());
    $("#overlay").classList.remove("on");};
}

/* ---------- skan / start ---------- */
async function poll(){
  try{
    const d=await fetch("/api/sessions").then(r=>r.json());
    state.sessions=d.sessions;renderFilters();
    if(!$("#list").dataset.touched||document.activeElement!==$("#q"))renderList();
    const bar=$("#scanbar");
    if(!d.done){bar.style.display="flex";$("#scantxt").textContent=`Skanuje ${d.scanned}/${d.total}`;
      $("#scanfill").style.width=(d.total?100*d.scanned/d.total:0)+"%";setTimeout(poll,700);}
    else{$("#scanfill").style.width="100%";setTimeout(()=>bar.style.display="none",500);}
  }catch(e){setTimeout(poll,1500);}
}
async function init(){
  state.cfg=await fetch("/api/config").then(r=>r.json()).catch(()=>({}));
  $("#q").oninput=e=>{state.q=e.target.value;renderList();};
  $("#refresh").onclick=async()=>{await fetch("/api/refresh");toast("Odswiezam...");poll();};
  setupModal();
  document.addEventListener("keydown",e=>{
    if(e.key==="/"&&document.activeElement!==$("#q")){e.preventDefault();$("#q").focus();}
    if(e.key==="Escape")$("#overlay").classList.remove("on");
    if(e.key==="n"&&e.target.tagName!=="INPUT"&&e.target.tagName!=="TEXTAREA")openModal();});
  renderFilters();poll();
}
init();
</script>
</body>
</html>'''


# ============================================================================
#  START
# ============================================================================
def find_port(start=8765):
    for p in range(start, start + 60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def find_browser():
    """Znajdz Edge/Chrome/Brave (do trybu --app okna desktopowego)."""
    if sys.platform == "win32":
        bases = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("LOCALAPPDATA", "")]
        rels = [r"Microsoft\Edge\Application\msedge.exe",
                r"Google\Chrome\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        for base in bases:
            for rel in rels:
                if base:
                    p = os.path.join(base, rel)
                    if os.path.exists(p):
                        return p
    for n in ("msedge", "google-chrome", "chrome", "chromium", "chromium-browser", "brave"):
        w = shutil.which(n)
        if w:
            return w
    return None


def open_app_window(url, browser=None):
    """Otworz UI jako osobne okno aplikacji (chromium --app, bez paska adresu)."""
    browser = browser or find_browser()
    if not browser:
        return None
    profile = os.path.join(tempfile.gettempdir(), "przegladaczka_app")
    a = [browser, f"--app={url}", f"--user-data-dir={profile}",
         "--window-size=1320,880", "--no-first-run", "--no-default-browser-check"]
    try:
        return subprocess.Popen(a)
    except Exception:
        return None


def main():
    port = 8765
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except Exception:
            pass
    port = find_port(port)
    load_cache()
    ensure_scan()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    browser = find_browser()
    use_window = ("--no-open" not in sys.argv) and ("--browser" not in sys.argv) and bool(browser)
    print("=" * 58)
    print("  Przegladaczka sesji Claude Code & Codex")
    print("  Adres:  " + url)
    print(f"  Claude: {'OK' if CLAUDE_BIN else 'brak'}   "
          f"Codex: {'OK' if CODEX_BIN else 'brak'}   "
          f"Windows Terminal: {'OK' if WT_BIN else 'brak (uzyje cmd)'}")
    if use_window:
        print("  Tryb: okno aplikacji (" + os.path.basename(browser) + ") - zamkniecie okna konczy program")
    else:
        print("  Tryb: przegladarka. Zatrzymanie: Ctrl+C")
    print("=" * 58)

    if use_window:
        proc = open_app_window(url, browser)
        if proc is not None:
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                proc.wait()
            except KeyboardInterrupt:
                pass
            print("\nZamknieto okno - koncze.")
            httpd.shutdown()
            return
        webbrowser.open(url)  # fallback gdyby okno sie nie otworzylo
    elif "--no-open" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDo zobaczenia.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
