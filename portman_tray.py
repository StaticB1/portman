#!/usr/bin/env python3
"""
portman — Dev Port Manager, Linux tray app.

The tray menu stays short: what is running, what you pinned, what you started
last. Everything else lives in a searchable window (Open portman, or
`portman ui`).

Author: Statotech Systems
"""

__version__ = "2.0.0"
__author__ = "Statotech Systems"

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Notify', '0.7')
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3

from gi.repository import Gtk, Gdk, Gio, GLib, Notify, Pango
import cairo
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

APP_ID      = "portman"
APP_NAME    = "portman"
DBUS_ID     = "io.github.staticb1.portman"
HOME        = Path.home()
REGISTRY    = HOME / ".portman.json"
BACKUP      = REGISTRY.with_suffix(REGISTRY.suffix + ".bak")
STATE_FILE  = HOME / ".portman-tray.json"      # pins and recents; tray-only
LOG_DIR     = HOME / ".portman-logs"           # shared with `portman start --detach`
ICON_DIR    = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "portman"
CLI         = Path(__file__).resolve().with_name("portman.js")

POLL_SEC       = 3     # how often port state is read when nothing is happening
POLL_BUSY_SEC  = 1     # ... and while something is starting or stopping
START_TIMEOUT  = 90    # seconds to wait for a started server to open its port
MENU_STOPPED   = 8     # most pinned + recent rows the tray menu will show
LOG_KEEP       = 256 * 1024
LOG_TRIM_AT    = 1024 * 1024
SCHEMA_VERSION = 1

GREEN, AMBER, RED = "#2ec27e", "#e5a50a", "#e01b24"

# ── Registry ──────────────────────────────────────────────────────────────────
# The CLI (portman.js) writes this same file. Both writers must be atomic, and
# neither may treat a corrupt registry as an empty one: that used to make the
# next write silently destroy every entry.

def load_registry() -> tuple[dict, str | None]:
    """(registry, error). An unreadable registry carries '_unreadable'."""
    if not REGISTRY.exists():
        return {"schemaVersion": SCHEMA_VERSION, "projects": {}}, None
    try:
        raw = REGISTRY.read_text()
    except Exception as e:
        return {"projects": {}, "_unreadable": True}, f"cannot read {REGISTRY}: {e}"
    if not raw.strip():
        return {"schemaVersion": SCHEMA_VERSION, "projects": {}}, None
    try:
        reg = json.loads(raw)
    except Exception as e:
        return ({"projects": {}, "_unreadable": True},
                f"{REGISTRY} is not valid JSON ({e}). Restore: cp {BACKUP} {REGISTRY}")
    if not isinstance(reg, dict) or not isinstance(reg.get("projects"), dict):
        return {"projects": {}, "_unreadable": True}, f"{REGISTRY} has no 'projects' object"
    return reg, None

def write_atomic(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def save_registry(reg: dict):
    # Never write over a registry we could not parse — that is data loss.
    if reg.get("_unreadable"):
        raise RuntimeError("refusing to write: the registry on disk is unreadable")
    reg = {k: v for k, v in reg.items() if k != "_unreadable"}
    reg["schemaVersion"] = SCHEMA_VERSION
    if REGISTRY.exists():
        try:
            BACKUP.write_text(REGISTRY.read_text())
        except Exception:
            pass
    write_atomic(REGISTRY, json.dumps(reg, indent=2) + "\n")

def load_state() -> dict:
    try:
        s = json.loads(STATE_FILE.read_text())
        if isinstance(s, dict):
            return {"pinned": [x for x in s.get("pinned", []) if isinstance(x, str)],
                    "recent": [x for x in s.get("recent", []) if isinstance(x, str)]}
    except Exception:
        pass
    return {"pinned": [], "recent": []}

def save_state(state: dict):
    try:
        write_atomic(STATE_FILE, json.dumps(state, indent=2) + "\n")
    except Exception:
        pass

# ── Port detection ────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float = 5, env: dict | None = None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)
    except Exception:
        return None

def listening() -> dict[int, dict] | None:
    """Every listening TCP port → {'pid', 'cmd'} in ONE call. None means the
    query itself failed, which must never be folded into "nothing is running":
    that is what made the tray show live servers as stopped."""
    r = _run(["ss", "-H", "-tlnp"])
    if r is not None and r.returncode == 0:
        out: dict[int, dict] = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                port = int(parts[3].rsplit(":", 1)[1])
            except (IndexError, ValueError):
                continue
            m = re.search(r'\(\("([^"]*)",pid=(\d+)', line)
            entry = {"pid": int(m.group(2)), "cmd": m.group(1)} if m else {"pid": None, "cmd": None}
            if port not in out or (out[port]["pid"] is None and entry["pid"]):
                out[port] = entry
        return out
    r = _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"])
    if r is not None and r.returncode in (0, 1):
        out, pid, cmd = {}, None, None
        for line in r.stdout.splitlines():
            if line.startswith("p"):
                pid, cmd = int(line[1:]), None
            elif line.startswith("c"):
                cmd = line[1:]
            elif line.startswith("n"):
                try:
                    out.setdefault(int(line.rsplit(":", 1)[1]), {"pid": pid, "cmd": cmd})
                except (IndexError, ValueError):
                    pass
        return out
    return None

def docker_ports() -> dict[int, dict]:
    """Published port → {'id', 'name'} for running containers."""
    r = _run(["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Ports}}"], timeout=4)
    out: dict[int, dict] = {}
    if r is None or r.returncode != 0:
        return out
    for line in r.stdout.splitlines():
        bits = line.split("\t")
        if len(bits) < 3:
            continue
        for m in re.finditer(r':(\d+)->', bits[2]):
            out[int(m.group(1))] = {"id": bits[0], "name": bits[1]}
    return out

def cwd_of(pid: int | None) -> str | None:
    if not pid:
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

# Ports belonging to the OS or a database. The tray is one mis-click away from
# killing these, so it refuses them outright.
PROTECTED_PORTS = {
    22, 25, 53, 67, 68, 111, 123, 139, 143, 389, 445, 465, 587, 631, 993, 995,
    1433, 3306, 5353, 5432, 6379, 9200, 11211, 27017,
}

def kill_port(port: int) -> tuple[bool, str]:
    """End whatever listens on a port. (freed, why not)."""
    if port in PROTECTED_PORTS:
        return False, f"port {port} is a system or database port; portman will not end it"
    for _ in range(3):
        snap = listening()
        if snap is None:
            return False, "cannot inspect ports (install iproute2 or lsof)"
        holder = snap.get(port)
        if holder is None:
            return True, ""
        pid = holder["pid"]
        if pid is None:
            box = docker_ports().get(port)
            if not box:
                return False, (f"port {port} is held by a process portman cannot see "
                               f"(another user's, or root's)")
            _run(["docker", "stop", box["id"]], timeout=30)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(15):
                time.sleep(0.2)
                if not alive(pid):
                    break
            else:
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.3)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False, f"not allowed to end pid {pid} on port {port}"
    snap = listening()
    if snap is not None and port not in snap:
        return True, ""
    return False, f"port {port} is still in use"

# ── Starting things ───────────────────────────────────────────────────────────

def inject_port(cmd: str, port: int) -> str:
    """Mirror of injectPort() in portman.js: frameworks that ignore the PORT
    env var get the port as a flag, so a server binds its registered port."""
    if not port:
        return cmd
    if re.search(r'--port[ =]|\s-p\s+\d|runserver\s+\S*:\d+', cmd):
        return cmd
    if re.search(r'\bnext\s+dev\b', cmd):         return f'{cmd} --port {port}'
    if re.search(r'manage\.py\s+runserver', cmd):  return f'{cmd} 0.0.0.0:{port}'
    if re.search(r'\bflask\s+run\b', cmd):        return f'{cmd} --port {port}'
    if re.search(r'\bvite\b', cmd):                return f'{cmd} --port {port}'
    if re.search(r'\bartisan\s+serve\b', cmd):    return f'{cmd} --port={port}'
    return cmd

def shell_env() -> dict:
    """The environment of the user's interactive shell. A tray started at login
    has the desktop session's PATH, which knows nothing of nvm, pyenv, cargo or
    ~/.local tools set up in a shell rc file — so `npm run dev` started from the
    tray failed with "npm: not found" while working in every terminal."""
    env = dict(os.environ)
    shell = os.environ.get("SHELL") or "/bin/bash"
    marker = "__PORTMAN_ENV__"
    try:
        r = subprocess.run([shell, "-lic", f"printf '%s\\0' {marker}; env -0"],
                           capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
                           start_new_session=True)
        blob = r.stdout.split(marker.encode() + b"\0", 1)[1]
    except Exception:
        return env
    for pair in blob.split(b"\0"):
        key, sep, val = pair.partition(b"=")
        if not sep:
            continue
        key = key.decode(errors="replace")
        if key in ("_", "SHLVL", "PWD", "OLDPWD"):
            continue
        env[key] = val.decode(errors="replace")
    return env

def log_path(name: str) -> Path:
    return LOG_DIR / (re.sub(r'[^\w.-]', '_', name) + ".log")

def log_tail(name: str, lines: int = 6) -> str:
    try:
        with open(log_path(name), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4000))
            text = f.read().decode(errors="replace")
    except OSError:
        return ""
    text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', text)
    text = text.rsplit("\n── ", 1)[-1]          # only what the last start printed
    keep = [l for l in text.splitlines()[1:] if l.strip()]
    return "\n".join(keep[-lines:])

def short(path: str) -> str:
    home = str(HOME)
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path

def url_of(proj: dict) -> str:
    return f"http://localhost:{proj.get('port')}"

# ── Core: state, polling and actions (no widgets) ─────────────────────────────

ACTIVE = ("running", "foreign", "starting", "stopping", "noport")

class Core:
    """Owns the registry snapshot, each project's live status and the actions.
    Widgets subscribe; they never probe ports themselves."""

    def __init__(self):
        self.reg: dict = {"projects": {}}
        self.reg_error: str | None = None
        self.status: dict[str, dict] = {}
        self.state = load_state()
        self.env = dict(os.environ)
        self.managed: dict[str, subprocess.Popen] = {}
        self.pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._sig = None
        self.on_change = []     # fn(registry_changed: bool)
        self.on_event = []      # fn(level, name, text)   level: ok | warn | error
        self.confirm = lambda text, detail, parent=None: False   # set by the UI

    def run(self):
        threading.Thread(target=self._load_env, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _load_env(self):
        self.env = shell_env()

    # ── reading ──

    @property
    def projects(self) -> dict:
        return self.reg.get("projects", {})

    def running_count(self) -> int:
        return sum(1 for s in self.status.values() if s["state"] in ("running", "foreign"))

    def is_pinned(self, name: str) -> bool:
        return name in self.state["pinned"]

    def refresh(self):
        self._wake.set()

    # ── polling ──

    def _poll_loop(self):
        while True:
            try:
                self._poll_once()
            except Exception as e:      # a bad poll must not end the thread
                print(f"portman: poll failed: {e}", file=sys.stderr)
            with self._lock:
                busy = bool(self.pending)
            self._wake.wait(POLL_BUSY_SEC if busy else POLL_SEC)
            self._wake.clear()

    def _classify(self, name: str, proj: dict, snap, docker) -> dict:
        port = proj.get("port")
        proc = self.managed.get(name)
        ours = proc is not None and proc.poll() is None
        st = {"state": "stopped", "pid": None, "cmd": None, "detail": ""}
        if snap is None:
            st.update(state="unknown", detail="cannot inspect ports (install iproute2 or lsof)")
            return st
        holder = snap.get(port)
        if holder is None:
            if ours:
                st.update(state="noport", pid=proc.pid,
                          detail=f"process is running but nothing listens on :{port}")
            return st
        pid, cmd = holder["pid"], holder["cmd"]
        st.update(state="running", pid=pid, cmd=cmd)
        if pid is None:
            box = docker().get(port)
            st["detail"] = f"docker: {box['name']}" if box else "held by a system process"
            return st
        st["detail"] = f"pid {pid} · {cmd}" if cmd else f"pid {pid}"
        cwd = cwd_of(pid)
        own = os.path.realpath(proj["dir"]) if proj.get("dir") else ""
        if ours or not cwd or not own or cwd == own or cwd.startswith(own + os.sep):
            return st
        st.update(state="foreign",
                  detail=f"port held by {cmd or 'a process'} (pid {pid}) in {short(cwd)}, "
                         f"not this project")
        return st

    def _poll_once(self):
        reg, err = load_registry()
        snap = listening()
        cache: dict = {}
        def docker():
            if "d" not in cache:
                cache["d"] = docker_ports()
            return cache["d"]

        status, events = {}, []
        for name, proj in reg.get("projects", {}).items():
            status[name] = self._classify(name, proj, snap, docker)

        now = time.time()
        with self._lock:
            for name, p in list(self.pending.items()):
                st = status.get(name)
                if st is None:
                    del self.pending[name]
                    continue
                if p["what"] == "stopping":
                    st["state"] = "stopping"
                    continue
                proc = self.managed.get(name)
                if not p.get("spawned"):
                    st["state"] = "starting"
                elif st["state"] == "running":
                    del self.pending[name]
                    events.append(("ok", name, f"{name} is up on :{reg['projects'][name].get('port')}",
                                   p.get("open")))
                elif proc is None or proc.poll() is not None:
                    del self.pending[name]
                    self.managed.pop(name, None)
                    code = proc.returncode if proc else "?"
                    tail = log_tail(name)
                    events.append(("error", name,
                                   f"{name} stopped right after starting (exit {code})."
                                   + (f"\n{tail}" if tail else ""), False))
                elif now - p["since"] > START_TIMEOUT:
                    del self.pending[name]
                    events.append(("warn", name,
                                   f"{name} is running but nothing opened port "
                                   f"{reg['projects'][name].get('port')} after {START_TIMEOUT} s. "
                                   f"It may be listening on another port.", False))
                else:
                    st["state"] = "starting"
        GLib.idle_add(self._apply, reg, err, status, events)

    def _apply(self, reg, err, status, events):
        reg_changed = (reg != self.reg) or (err != self.reg_error)
        sig = (json.dumps(status, sort_keys=True), err)
        changed = reg_changed or sig != self._sig
        self.reg, self.reg_error, self.status, self._sig = reg, err, status, sig
        if changed:
            for fn in self.on_change:
                fn(reg_changed)
        for level, name, text, open_after in events:
            if open_after and level == "ok":
                self.open_browser(name)
            self.emit(level, name, text)
        return False

    def emit(self, level: str, name: str | None, text: str):
        for fn in self.on_event:
            fn(level, name, text)

    def _emit_later(self, level, name, text):
        GLib.idle_add(lambda: self.emit(level, name, text) and False)

    # ── actions (called on the main thread) ──

    def why_not_startable(self, name: str) -> str | None:
        proj = self.projects.get(name) or {}
        if not proj.get("startCmd"):
            return "No start command. Set one in Edit."
        d = proj.get("dir")
        if not d or not Path(d).is_dir():
            return f"Directory missing: {short(d or '?')}"
        return None

    def start(self, name: str, open_after: bool = False, parent=None):
        proj = self.projects.get(name)
        if not proj:
            return
        st = self.status.get(name, {})
        if st.get("state") in ("running", "starting"):
            if open_after and st.get("state") == "running":
                self.open_browser(name)
            return
        why = self.why_not_startable(name)
        if why:
            self.emit("error", name, f"{name}: {why}")
            return
        if st.get("state") == "foreign" and not self._confirm_foreign(name, proj, st, parent):
            return
        self._begin(name, "starting", open=open_after)
        threading.Thread(target=self._start_job, args=(name, dict(proj)), daemon=True).start()

    def stop(self, name: str, parent=None):
        proj = self.projects.get(name)
        if not proj:
            return
        st = self.status.get(name, {})
        if st.get("state") == "foreign" and not self._confirm_foreign(name, proj, st, parent):
            return
        self._begin(name, "stopping")
        threading.Thread(target=self._stop_job, args=(name, dict(proj)), daemon=True).start()

    def restart(self, name: str, parent=None):
        proj = self.projects.get(name)
        if not proj:
            return
        why = self.why_not_startable(name)
        if why:
            self.emit("error", name, f"{name}: {why}")
            return
        st = self.status.get(name, {})
        if st.get("state") == "foreign" and not self._confirm_foreign(name, proj, st, parent):
            return
        self._begin(name, "stopping")
        def job():
            if self._stop_job(name, dict(proj), keep_pending=True):
                self._begin(name, "starting")
                self._start_job(name, dict(proj))
        threading.Thread(target=job, daemon=True).start()

    def group_members(self, group: str) -> list[str]:
        return [n for n, p in self.projects.items() if p.get("group") == group]

    def _confirm_foreign(self, name, proj, st, parent) -> bool:
        return self.confirm(
            f"Port {proj.get('port')} is in use by something else",
            f"{st.get('detail', '').capitalize()}.\n\n"
            f"Continuing ends that process so {name} can have the port.", parent)

    def _begin(self, name: str, what: str, **extra):
        with self._lock:
            self.pending[name] = {"what": what, "since": time.time(), **extra}
        if name in self.status:
            self.status[name]["state"] = what
            self._sig = None
            for fn in self.on_change:
                fn(False)
        self._wake.set()

    def _end(self, name: str):
        with self._lock:
            self.pending.pop(name, None)
        self._wake.set()

    def _start_job(self, name: str, proj: dict):
        port = proj.get("port")
        if port:
            ok, why = kill_port(port)       # a foreign holder was confirmed above
            if not ok:
                self._end(name)
                self._emit_later("error", name, f"{name} not started: {why}.")
                return
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            lp = log_path(name)
            if lp.exists() and lp.stat().st_size > LOG_TRIM_AT:
                data = lp.read_bytes()[-LOG_KEEP:]
                lp.write_bytes(data[data.find(b"\n") + 1:])
            cmd = inject_port(proj["startCmd"], port)
            env = dict(self.env)
            if port:
                env["PORT"] = str(port)
            # Output goes to a file, never a pipe: a pipe nobody reads fills up
            # and freezes the server. A new session keeps the server alive when
            # the tray quits or is updated.
            with open(lp, "ab") as out:
                out.write(f"\n── {time.strftime('%Y-%m-%d %H:%M:%S')}  {cmd}\n".encode())
                out.flush()
                proc = subprocess.Popen(cmd, shell=True, cwd=proj["dir"], env=env,
                                        stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        except Exception as e:
            self._end(name)
            self._emit_later("error", name, f"{name} not started: {e}")
            return
        self.managed[name] = proc
        with self._lock:
            if name in self.pending:
                self.pending[name].update(spawned=True, since=time.time())
        self.state["recent"] = ([name] + [n for n in self.state["recent"] if n != name])[:12]
        save_state(self.state)
        self._wake.set()

    def _stop_job(self, name: str, proj: dict, keep_pending: bool = False) -> bool:
        ok, why = True, ""
        proc = self.managed.pop(name, None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        port = proj.get("port")
        if port:
            ok, why = kill_port(port)
        if not ok:
            self._end(name)
            self._emit_later("error", name, f"{name} not stopped: {why}.")
            return False
        if not keep_pending:
            self._end(name)
        return True

    # ── small helpers the UI shares ──

    def open_browser(self, name: str):
        proj = self.projects.get(name)
        if proj:
            self.open_path(url_of(proj))

    def open_path(self, target: str):
        try:
            subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception as e:
            self.emit("error", None, f"Could not open {target}: {e}")

    def open_terminal(self, name: str):
        d = (self.projects.get(name) or {}).get("dir")
        if not d or not Path(d).is_dir():
            self.emit("error", name, f"{name}: directory missing")
            return
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term], cwd=d, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return
        self.emit("error", name, "No terminal program found")

    def toggle_pin(self, name: str):
        pins = self.state["pinned"]
        pins.remove(name) if name in pins else pins.append(name)
        save_state(self.state)
        self._sig = None
        for fn in self.on_change:
            fn(False)

    def cli(self, *args: str, timeout: float = 60):
        """Run the CLI next to this file. (returncode, stdout, stderr)."""
        node = shutil.which("node", path=self.env.get("PATH"))
        if not node:
            return 127, "", "node was not found on your PATH"
        r = _run([node, str(CLI), *args], timeout=timeout,
                 env={**self.env, "NO_COLOR": "1", "FORCE_COLOR": "0"})
        if r is None:
            return 1, "", "the command did not finish"
        strip = lambda s: re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', s or '')
        return r.returncode, strip(r.stdout), strip(r.stderr)

# ── Icons ─────────────────────────────────────────────────────────────────────

def write_icon(kind: str) -> str:
    """kind: idle | running | warn. One file per state: reusing a single path
    leaves the panel showing a cached image."""
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    path = ICON_DIR / f"portman-{kind}.png"
    size = 32
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    r, g, b = {"running": (0.18, 0.76, 0.49), "warn": (0.90, 0.65, 0.04)}.get(kind, (0.62, 0.66, 0.72))
    ctx.set_source_rgba(r, g, b, 0.22)
    ctx.arc(size / 2, size / 2, 13, 0, 2 * math.pi)
    ctx.fill()
    ctx.set_source_rgb(r, g, b)
    ctx.set_line_width(2)
    ctx.arc(size / 2, size / 2, 13, 0, 2 * math.pi)
    ctx.stroke()
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(20)
    xb, yb, w, h, _, _ = ctx.text_extents("P")
    ctx.move_to(size / 2 - w / 2 - xb, size / 2 - h / 2 - yb)
    ctx.show_text("P")
    surface.write_to_png(str(path))
    return str(path)

# ── Window ────────────────────────────────────────────────────────────────────

def esc(text) -> str:
    return GLib.markup_escape_text(str(text))

def icon_button(icon: str, tip: str, label: str | None = None) -> Gtk.Button:
    btn = Gtk.Button()
    img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    if label:
        box = Gtk.Box(spacing=6)
        box.pack_start(img, False, False, 0)
        box.pack_start(Gtk.Label(label=label), False, False, 0)
        btn.add(box)
    else:
        btn.add(img)
    btn.get_child().show_all()      # the button itself may be hidden by toggled()
    btn.set_tooltip_text(tip)
    btn.set_valign(Gtk.Align.CENTER)
    return btn

def toggled(widget: Gtk.Widget, on: bool):
    widget.set_no_show_all(True)
    widget.set_visible(on)

SECTIONS = {0: "Running", 1: "Pinned", 2: "Not running"}

# Scoped to our own class names: a bare `window {}` rule here used to restyle
# every dialog the tray opened.
CSS = b"""
.portman-list row { border-bottom: 1px solid alpha(@theme_fg_color, 0.06); }
.portman-list row:selected { background-color: alpha(@theme_fg_color, 0.09); color: @theme_fg_color; }
.portman-list row:selected label { color: @theme_fg_color; }
"""

def install_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

class ProjectRow(Gtk.ListBoxRow):
    def __init__(self, win, name: str):
        super().__init__()
        self.win, self.core, self.name = win, win.core, name
        self.section = 2
        self._menu = None

        box = Gtk.Box(spacing=10)
        box.set_border_width(8)
        box.set_margin_start(6)
        box.set_margin_end(6)

        self.dot = Gtk.Label()
        self.dot.set_width_chars(2)
        self.spinner = Gtk.Spinner()
        box.pack_start(self.dot, False, False, 0)
        box.pack_start(self.spinner, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.title = Gtk.Label(xalign=0)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail = Gtk.Label(xalign=0)
        self.detail.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        text.pack_start(self.title, False, False, 0)
        text.pack_start(self.detail, False, False, 0)
        box.pack_start(text, True, True, 0)

        self.open_btn = icon_button("web-browser-symbolic", "Open in browser")
        self.open_btn.connect("clicked", lambda _: self.core.open_browser(self.name))
        self.start_btn = icon_button("media-playback-start-symbolic", "Start", "Start")
        self.start_btn.connect("clicked", lambda _: self.core.start(self.name, parent=self.win))
        self.stop_btn = icon_button("media-playback-stop-symbolic", "Stop", "Stop")
        self.stop_btn.connect("clicked", lambda _: self.core.stop(self.name, parent=self.win))
        self.more_btn = icon_button("view-more-symbolic", "More")
        self.more_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.more_btn.connect("clicked", self._popup)
        for b in (self.open_btn, self.start_btn, self.stop_btn, self.more_btn):
            box.pack_start(b, False, False, 0)

        self.add(box)
        self.update()

    @property
    def proj(self) -> dict:
        return self.core.projects.get(self.name, {})

    @property
    def state(self) -> str:
        return self.core.status.get(self.name, {}).get("state", "stopped")

    def haystack(self) -> str:
        p = self.proj
        return f"{self.name} {p.get('port', '')} {p.get('dir', '')} {p.get('group', '')}".lower()

    def update(self):
        p, st = self.proj, self.core.status.get(self.name, {})
        state = st.get("state", "stopped")
        busy = state in ("starting", "stopping")
        pinned = self.core.is_pinned(self.name)
        self.section = 0 if state in ACTIVE else (1 if pinned else 2)

        dots = {"running": f'<span foreground="{GREEN}">●</span>',
                "foreign": f'<span foreground="{AMBER}">◆</span>',
                "noport":  f'<span foreground="{AMBER}">◐</span>',
                "unknown": f'<span foreground="{AMBER}">?</span>'}
        self.dot.set_markup(dots.get(state, '<span alpha="45%">○</span>'))
        toggled(self.dot, not busy)
        toggled(self.spinner, busy)
        self.spinner.start() if busy else self.spinner.stop()

        pin = '  <span alpha="55%" size="small">pinned</span>' if pinned else ""
        self.title.set_markup(
            f'<b>{esc(self.name)}</b>  '
            f'<span font_family="monospace" alpha="70%">:{esc(p.get("port", "?"))}</span>{pin}')

        why = self.core.why_not_startable(self.name)
        if state == "starting":
            detail = "starting…"
        elif state == "stopping":
            detail = "stopping…"
        elif state in ("running", "foreign", "noport", "unknown"):
            detail = st.get("detail", "")
        else:
            detail = why or short(p.get("dir") or "")
        colour = f' foreground="{AMBER}"' if state in ("foreign", "noport", "unknown") else ' alpha="60%"'
        self.detail.set_markup(f'<span size="small"{colour}>{esc(detail)}</span>')

        tip = f"{p.get('startCmd') or 'no start command'}\n{short(p.get('dir') or '')}"
        self.title.set_tooltip_text(tip)
        self.detail.set_tooltip_text(st.get("detail") or tip)

        toggled(self.open_btn, state in ("running", "foreign"))
        toggled(self.stop_btn, state in ("running", "foreign", "noport", "starting"))
        toggled(self.start_btn, state in ("stopped", "unknown"))
        self.start_btn.set_sensitive(why is None)
        self.start_btn.set_tooltip_text(why or f"Start {self.name}")
        self.changed()

    def activate_default(self):
        """Enter or double-click: open what is running, start what is not."""
        if self.state in ("running", "foreign"):
            self.core.open_browser(self.name)
        elif self.state == "stopped":
            self.core.start(self.name, parent=self.win)

    def _popup(self, button):
        core, name, state, p = self.core, self.name, self.state, self.proj
        menu = Gtk.Menu()
        def add(label, fn, enabled=True):
            item = Gtk.MenuItem(label=label)
            item.set_sensitive(enabled)
            item.connect("activate", lambda _: fn())
            menu.append(item)
        startable = core.why_not_startable(name) is None
        if state == "stopped":
            add("Start and open in browser",
                lambda: core.start(name, open_after=True, parent=self.win), startable)
        else:
            add("Restart", lambda: core.restart(name, parent=self.win), startable)
        add("View log", lambda: self.win.show_log(name), log_path(name).exists())
        menu.append(Gtk.SeparatorMenuItem())
        add("Copy address", lambda: self.win.copy(url_of(p)))
        add("Open folder", lambda: core.open_path(p.get("dir", "")),
            bool(p.get("dir")) and Path(p["dir"]).is_dir())
        add("Open terminal here", lambda: core.open_terminal(name),
            bool(p.get("dir")) and Path(p["dir"]).is_dir())
        group = p.get("group")
        if group:
            menu.append(Gtk.SeparatorMenuItem())
            add(f"Start all of {group}",
                lambda: [core.start(n, parent=self.win) for n in core.group_members(group)])
            add(f"Stop all of {group}",
                lambda: [core.stop(n, parent=self.win) for n in core.group_members(group)
                         if core.status.get(n, {}).get("state") in ACTIVE])
        menu.append(Gtk.SeparatorMenuItem())
        add("Unpin from tray menu" if core.is_pinned(name) else "Pin to tray menu",
            lambda: core.toggle_pin(name))
        add("Edit…", lambda: self.win.edit_project(name))
        add("Remove…", lambda: self.win.remove_project(name))
        menu.show_all()
        self._menu = menu       # keep a reference or it is collected mid-popup
        menu.popup_at_widget(button, Gdk.Gravity.SOUTH_EAST, Gdk.Gravity.NORTH_EAST, None)


class MainWindow(Gtk.Window):
    def __init__(self, core: Core):
        super().__init__(title="portman")
        self.core = core
        self.rows: dict[str, ProjectRow] = {}
        self.only_running = False
        self._msg_log = None
        self._doctor = None
        self._logs = {}
        self.set_default_size(580, 680)
        self.set_icon_name("network-server")

        bar = Gtk.HeaderBar(title="portman")
        bar.set_show_close_button(True)
        add_btn = icon_button("list-add-symbolic", "Add a project")
        add_btn.connect("clicked", lambda _: self.edit_project(None))
        bar.pack_start(add_btn)
        self.tools_btn = Gtk.MenuButton()
        self.tools_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        self.tools_btn.set_popup(self._tools_menu())
        bar.pack_end(self.tools_btn)
        self.bar = bar
        self.set_titlebar(bar)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.problem_bar = Gtk.InfoBar()
        self.problem_bar.set_message_type(Gtk.MessageType.WARNING)
        self.problem_lbl = Gtk.Label(xalign=0)
        self.problem_lbl.set_line_wrap(True)
        self.problem_bar.get_content_area().add(self.problem_lbl)
        self.problem_bar.add_button("Details", 1)
        self.problem_bar.connect("response", lambda *_: self.show_doctor())
        outer.pack_start(self.problem_bar, False, False, 0)

        top = Gtk.Box(spacing=8)
        top.set_border_width(10)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search by name, port or folder")
        self.search.connect("search-changed", lambda _: self._refilter())
        self.search.connect("activate", lambda _: self._activate_first())
        self.search.connect("key-press-event", self._on_search_key)
        top.pack_start(self.search, True, True, 0)
        seg = Gtk.Box()
        seg.get_style_context().add_class("linked")
        self.all_btn = Gtk.RadioButton.new_with_label(None, "All")
        self.run_btn = Gtk.RadioButton.new_with_label_from_widget(self.all_btn, "Running")
        for b in (self.all_btn, self.run_btn):
            b.set_mode(False)
            seg.pack_start(b, False, False, 0)
        self.run_btn.connect("toggled", self._on_filter)
        top.pack_start(seg, False, False, 0)
        outer.pack_start(top, False, False, 0)

        self.list = Gtk.ListBox()
        self.list.get_style_context().add_class("portman-list")
        self.list.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.list.set_activate_on_single_click(False)
        self.list.set_sort_func(self._sort)
        self.list.set_filter_func(self._filter)
        self.list.set_header_func(self._header)
        self.list.connect("row-activated", lambda _l, row: row.activate_default())
        self.placeholder = Gtk.Label()
        self.placeholder.set_line_wrap(True)
        self.placeholder.set_justify(Gtk.Justification.CENTER)
        self.placeholder.set_margin_top(48)
        self.placeholder.set_margin_start(24)
        self.placeholder.set_margin_end(24)
        self.placeholder.show()
        self.list.set_placeholder(self.placeholder)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.list)
        outer.pack_start(scroll, True, True, 0)

        self.msg_bar = Gtk.InfoBar()
        self.msg_bar.set_show_close_button(True)
        self.msg_lbl = Gtk.Label(xalign=0)
        self.msg_lbl.set_line_wrap(True)
        self.msg_lbl.set_selectable(True)
        self.msg_lbl.set_max_width_chars(60)
        self.msg_bar.get_content_area().add(self.msg_lbl)
        self.msg_log_btn = self.msg_bar.add_button("View log", 1)
        self.msg_bar.connect("response", self._on_msg_response)
        outer.pack_start(self.msg_bar, False, False, 0)

        self.add(outer)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", self._on_destroy)
        core.on_change.append(self._on_change)
        core.on_event.append(self._on_event)
        self._rebuild()
        self.show_all()
        self.problem_bar.hide()
        self.msg_bar.hide()
        self.search.grab_focus()
        self.check_registry()

    # ── list ──

    def _rebuild(self):
        for row in list(self.rows.values()):
            self.list.remove(row)
        self.rows = {}
        for name in self.core.projects:
            row = ProjectRow(self, name)
            self.rows[name] = row
            self.list.add(row)
            row.show_all()
        self._after_update()

    def _on_change(self, registry_changed: bool):
        if registry_changed or set(self.rows) != set(self.core.projects):
            self._rebuild()
            if registry_changed:
                self.check_registry()
            return
        for row in self.rows.values():
            row.update()
        self._after_update()

    def _after_update(self):
        n, total = self.core.running_count(), len(self.core.projects)
        self.bar.set_subtitle(f"{n} running · {total} project{'' if total == 1 else 's'}")
        self.run_btn.set_label(f"Running ({n})" if n else "Running")
        if self.core.reg_error:
            self.placeholder.set_markup(
                "<b>The registry cannot be read, so nothing is shown.</b>\n\n"
                + esc(self.core.reg_error)
                + "\n\nAdding and starting are off until it is fixed.")
        elif not self.core.projects:
            self.placeholder.set_markup(
                "<b>No projects yet.</b>\n\nAdd one with the + button, or run "
                "<tt>portman sync</tt> to load them from your inventory file.")
        elif self.only_running and not n:
            self.placeholder.set_text("Nothing is running.")
        else:
            self.placeholder.set_text(f"Nothing matches “{self.search.get_text().strip()}”.")
        self.list.invalidate_sort()
        self.list.invalidate_filter()
        self.list.invalidate_headers()

    def _sort(self, a, b):
        ka, kb = (a.section, a.name.lower()), (b.section, b.name.lower())
        return (ka > kb) - (ka < kb)

    def _filter(self, row) -> bool:
        if self.only_running and row.section != 0:
            return False
        words = self.search.get_text().lower().split()
        hay = row.haystack()
        return all(w in hay for w in words)

    def _header(self, row, before):
        if before is None or before.section != row.section:
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup(f'<span size="small" weight="bold" alpha="60%">'
                           f'{esc(SECTIONS[row.section].upper())}</span>')
            lbl.set_margin_start(16)
            lbl.set_margin_top(12)
            lbl.set_margin_bottom(4)
            row.set_header(lbl)
        else:
            row.set_header(None)

    def _visible_rows(self) -> list:
        rows = [r for r in self.rows.values() if self._filter(r)]
        return sorted(rows, key=lambda r: (r.section, r.name.lower()))

    def _refilter(self):
        self._after_update()
        rows = self._visible_rows()
        if rows:
            self.list.select_row(rows[0])

    def _on_filter(self, btn):
        self.only_running = btn.get_active()
        self._refilter()

    def _activate_first(self):
        row = self.list.get_selected_row()
        rows = self._visible_rows()
        if row is None or row not in rows:
            row = rows[0] if rows else None
        if row:
            row.activate_default()

    # ── keys ──

    def _on_search_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Down:
            rows = self._visible_rows()
            if rows:
                self.list.select_row(rows[0])
                rows[0].grab_focus()
            return True
        return False

    def _on_key(self, _w, ev):
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        if ev.keyval == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text("")
                self.search.grab_focus()
            else:
                self.close()
            return True
        if ctrl and ev.keyval in (Gdk.KEY_f, Gdk.KEY_l):
            self.search.grab_focus()
            return True
        if ctrl and ev.keyval == Gdk.KEY_w:
            self.close()
            return True
        focus = self.get_focus()
        if isinstance(focus, Gtk.Entry) or ctrl or ev.state & Gdk.ModifierType.MOD1_MASK:
            return False
        ch = Gdk.keyval_to_unicode(ev.keyval)
        if ch and chr(ch).isprintable() and chr(ch) != " ":
            self.search.grab_focus_without_selecting()
            self.search.set_position(-1)
            return self.search.handle_event(ev)
        return False

    # ── messages ──

    def _on_event(self, level: str, name, text: str):
        if level == "ok":
            if self._msg_log == name:
                self.msg_bar.hide()
            return
        self.say(text, error=(level == "error"), log_of=name)

    def say(self, text: str, error: bool = False, log_of: str | None = None):
        self.msg_bar.set_message_type(Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO)
        self.msg_lbl.set_text(text)
        self._msg_log = log_of
        toggled(self.msg_log_btn, bool(log_of) and log_path(log_of).exists())
        self.msg_bar.show()

    def _on_msg_response(self, _bar, response):
        if response == 1 and self._msg_log:
            self.show_log(self._msg_log)
        else:
            self.msg_bar.hide()

    def copy(self, text: str):
        clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clip.set_text(text, -1)
        clip.store()
        self.say(f"Copied {text}")

    # ── tools ──

    def _tools_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        for label, fn in (("Check the registry", lambda: self.show_doctor()),
                          ("Load ports from the inventory file", lambda: self.run_sync()),
                          (None, None),
                          ("Open the logs folder", self._open_logs),
                          ("Open the registry file", lambda: self.core.open_path(str(REGISTRY)))):
            if label is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _i, f=fn: f())
            menu.append(item)
        about = Gtk.MenuItem(label=f"portman {__version__}")
        about.set_sensitive(False)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(about)
        menu.show_all()
        return menu

    def _open_logs(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.core.open_path(str(LOG_DIR))

    def check_registry(self):
        """Ask `portman doctor` in the background; surface only what breaks a start."""
        def job():
            code, out, _err = self.core.cli("doctor", "--json")
            try:
                report = json.loads(out)
            except Exception:
                report = None
            GLib.idle_add(self._show_problems, report)
        threading.Thread(target=job, daemon=True).start()

    def _show_problems(self, report):
        self._doctor = report
        fails = (report or {}).get("counts", {}).get("fail", 0)
        if fails:
            self.problem_lbl.set_text(
                f"{fails} problem{'' if fails == 1 else 's'} in the registry will break a start.")
            self.problem_bar.show()
        else:
            self.problem_bar.hide()
        return False

    def show_doctor(self):
        dlg = Gtk.Dialog(title="Registry check", transient_for=self, modal=True)
        dlg.set_default_size(560, 420)
        text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text.set_border_width(12)
        buf = text.get_buffer()
        scroll = Gtk.ScrolledWindow()
        scroll.add(text)
        dlg.get_content_area().pack_start(scroll, True, True, 0)
        sync_btn = dlg.add_button("Load from inventory", 1)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)

        def render(report, note=""):
            if not report:
                buf.set_text(note or "Could not run `portman doctor`. Is node on your PATH?")
                return False
            lines = [note] if note else []
            problems = sorted(report.get("problems", []), key=lambda p: p.get("level") != "fail")
            if not problems:
                lines.append("Nothing to fix. The registry matches the inventory.")
            for p in problems:
                mark = "MUST FIX" if p.get("level") == "fail" else "worth a look"
                lines.append(f"{mark}: {p.get('subject')}  [{p.get('kind')}]")
                lines.append(f"    {p.get('detail')}")
                if p.get("fix"):
                    lines.append(f"    fix: {p['fix']}")
                lines.append("")
            fleet = report.get("fleet")
            lines.append(f"registry:  {short(str(report.get('registry')))}")
            lines.append(f"inventory: {short(fleet) if fleet else 'none found'}")
            sync_btn.set_sensitive(bool(fleet))
            buf.set_text("\n".join(lines))
            return False

        def reload(note=""):
            buf.set_text("Checking…")
            def job():
                _c, out, _e = self.core.cli("doctor", "--json")
                try:
                    report = json.loads(out)
                except Exception:
                    report = None
                GLib.idle_add(self._show_problems, report)
                GLib.idle_add(render, report, note)
            threading.Thread(target=job, daemon=True).start()

        def on_response(_d, response):
            if response != 1:
                dlg.destroy()
                return
            buf.set_text("Loading from the inventory…")
            def job():
                code, out, err = self.core.cli("sync")
                self.core.refresh()
                GLib.idle_add(reload, (out + err).strip() + "\n\n" + "─" * 40 + "\n")
            threading.Thread(target=job, daemon=True).start()

        dlg.connect("response", on_response)
        dlg.show_all()
        reload()

    def run_sync(self):
        self.say("Loading ports from the inventory file…")
        def job():
            code, out, err = self.core.cli("sync")
            self.core.refresh()
            last = [l.strip() for l in (out + err).splitlines() if l.strip()]
            GLib.idle_add(lambda: self.say("\n".join(last[-4:]) or "Done.", error=code != 0) and False)
        threading.Thread(target=job, daemon=True).start()

    def show_log(self, name: str):
        old = self._logs.get(name)
        if old:
            old.present()
            return
        win = LogWindow(self.core, name)
        self._logs[name] = win
        win.connect("destroy", lambda _w: self._logs.pop(name, None))

    # ── add, edit, remove ──

    def edit_project(self, name: str | None):
        if self.core.reg_error:
            self.say(self.core.reg_error, error=True)
            return
        proj = dict(self.core.projects.get(name, {})) if name else {}
        from_inventory = proj.get("source") == "FLEET.json"
        dlg = Gtk.Dialog(title=f"Edit {name}" if name else "Add a project",
                         transient_for=self, modal=True)
        dlg.set_default_size(460, -1)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = dlg.add_button("Save", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        dlg.set_default_response(Gtk.ResponseType.OK)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_border_width(16)
        def field(row, label, value, editable=True):
            lbl = Gtk.Label(label=label, xalign=1)
            lbl.get_style_context().add_class("dim-label")
            entry = Gtk.Entry(text=value or "", hexpand=True, activates_default=True)
            entry.set_sensitive(editable)
            grid.attach(lbl, 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            return entry
        name_e = field(0, "Name", name or "", not name)
        port_e = field(1, "Port", str(proj.get("port", "")), not from_inventory)
        dir_e = field(2, "Folder", proj.get("dir", ""), not from_inventory)
        pick = icon_button("folder-open-symbolic", "Choose a folder")
        pick.set_sensitive(not from_inventory)
        grid.attach(pick, 2, 2, 1, 1)
        cmd_e = field(3, "Start command", proj.get("startCmd") or ("" if name else "npm run dev"))
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_max_width_chars(52)
        if from_inventory:
            note.set_markup('<span size="small" alpha="65%">The port and folder come from the '
                            'inventory file. Change them there, then load from the inventory.</span>')
        grid.attach(note, 1, 4, 2, 1)
        dlg.get_content_area().add(grid)

        def choose(_b):
            fc = Gtk.FileChooserNative.new("Project folder", dlg,
                                           Gtk.FileChooserAction.SELECT_FOLDER, "Choose", "Cancel")
            start = dir_e.get_text().strip() or str(HOME)
            if Path(start).is_dir():
                fc.set_current_folder(start)
            if fc.run() == Gtk.ResponseType.ACCEPT:
                dir_e.set_text(fc.get_filename())
                if not name_e.get_text().strip():
                    name_e.set_text(Path(fc.get_filename()).name)
        pick.connect("clicked", choose)

        def fail(msg):
            note.set_markup(f'<span foreground="{RED}">{esc(msg)}</span>')

        dlg.show_all()
        while dlg.run() == Gtk.ResponseType.OK:
            n, d = name_e.get_text().strip(), dir_e.get_text().strip()
            cmd = cmd_e.get_text().strip()
            try:
                port = int(port_e.get_text().strip())
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                fail("The port must be a number from 1 to 65535.")
                continue
            if not n:
                fail("Give it a name.")
                continue
            reg, err = load_registry()
            if err:
                fail(err)
                continue
            if not name and n in reg["projects"]:
                fail(f"“{n}” already exists.")
                continue
            clash = next((o for o, p in reg["projects"].items()
                          if p.get("port") == port and o != n), None)
            if clash:
                fail(f"Port {port} already belongs to “{clash}”.")
                continue
            d = os.path.expanduser(d) if d else str(HOME)
            if not Path(d).is_dir():
                fail(f"That folder does not exist: {d}")
                continue
            # Start from the existing entry: group, scope and source are the
            # CLI's, and rewriting the entry from four fields used to drop them.
            entry = dict(reg["projects"].get(n, {}))
            now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            entry.update(port=port, dir=d, startCmd=cmd or None)
            entry.setdefault("addedAt", now)
            if name:
                entry["updatedAt"] = now
            else:
                entry.setdefault("scope", "personal")
            reg["projects"][n] = entry
            try:
                save_registry(reg)
            except Exception as e:
                fail(str(e))
                continue
            self.core.refresh()
            break
        dlg.destroy()

    def remove_project(self, name: str):
        proj = self.core.projects.get(name, {})
        extra = ""
        if proj.get("source") == "FLEET.json":
            extra = "\n\nIt is listed in the inventory file, so the next load from the inventory adds it back."
        if self.core.status.get(name, {}).get("state") == "running":
            extra = "\n\nIt is running, and stays running." + extra
        if not self.core.confirm(f"Remove “{name}” from portman?",
                                 "Only the registry entry is removed. The folder is not touched." + extra,
                                 self):
            return
        reg, err = load_registry()
        if err:
            self.say(err, error=True)
            return
        reg["projects"].pop(name, None)
        try:
            save_registry(reg)
        except Exception as e:
            self.say(str(e), error=True)
        self.core.refresh()

    def _on_destroy(self, _w):
        for lst, fn in ((self.core.on_change, self._on_change), (self.core.on_event, self._on_event)):
            if fn in lst:
                lst.remove(fn)


class LogWindow(Gtk.Window):
    """Follows one service's log file."""

    def __init__(self, core: Core, name: str):
        super().__init__(title=f"{name} — log")
        self.core, self.path, self._size = core, log_path(name), -1
        self.set_default_size(820, 520)
        bar = Gtk.HeaderBar(title=f"{name} log", subtitle=short(str(self.path)))
        bar.set_show_close_button(True)
        open_btn = Gtk.Button(label="Open file")
        open_btn.connect("clicked", lambda _: core.open_path(str(self.path)))
        bar.pack_end(open_btn)
        self.set_titlebar(bar)
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_border_width(10)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.add(self.view)
        self.add(self.scroll)
        self.connect("key-press-event",
                     lambda _w, ev: ev.keyval == Gdk.KEY_Escape and (self.close() or True))
        self._timer = GLib.timeout_add(1000, self._tick)
        self.connect("destroy", lambda _w: GLib.source_remove(self._timer))
        self._tick()
        self.show_all()

    def _tick(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size == self._size:
            return True
        self._size = size
        try:
            with open(self.path, "rb") as f:
                f.seek(max(0, size - 200_000))
                text = f.read().decode(errors="replace")
        except OSError:
            text = "No log yet. It is written when the service is started from portman."
        text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', text).replace("\r", "")
        adj = self.scroll.get_vadjustment()
        at_end = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40
        self.view.get_buffer().set_text(text)
        if at_end or self._size == size:
            GLib.idle_add(self._to_end)
        return True

    def _to_end(self):
        buf = self.view.get_buffer()
        self.view.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 1)
        return False

# ── Tray ──────────────────────────────────────────────────────────────────────

class Tray:
    """The panel icon. Its menu lists what is running, then pins and recents —
    never the whole registry — and is rebuilt only when that list changes:
    rebuilding on a timer closed the menu under the pointer."""

    def __init__(self, core: Core, show_window, quit_app):
        self.core, self.show_window, self.quit_app = core, show_window, quit_app
        self._sig = None
        self.icons = {k: write_icon(k) for k in ("idle", "running", "warn")}
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID, self.icons["idle"], AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("portman")
        Notify.init(APP_NAME)
        core.on_change.append(lambda _r: self.rebuild())
        core.on_event.append(self._on_event)
        self.rebuild(force=True)

    def _rows(self):
        core = self.core
        active = sorted((n for n, s in core.status.items() if s["state"] in ACTIVE), key=str.lower)
        stopped = [n for n in core.state["pinned"] + core.state["recent"]
                   if n in core.projects and n not in active]
        stopped = list(dict.fromkeys(stopped))[:MENU_STOPPED]
        return active, stopped

    def rebuild(self, force: bool = False):
        core = self.core
        active, stopped = self._rows()
        sig = (tuple((n, core.status[n]["state"], core.projects[n].get("port")) for n in active),
               tuple((n, core.projects[n].get("port"), core.why_not_startable(n)) for n in stopped),
               core.reg_error, len(core.projects))
        if sig == self._sig and not force:
            return
        self._sig = sig

        kind = "warn" if core.reg_error else ("running" if core.running_count() else "idle")
        self.indicator.set_icon_full(self.icons[kind], "portman")

        menu = Gtk.Menu()
        def add(label, fn=None, parent=menu, enabled=True):
            item = Gtk.MenuItem(label=label)
            item.set_sensitive(enabled and fn is not None)
            if fn:
                item.connect("activate", lambda _i: fn())
            parent.append(item)
            return item

        n = core.running_count()
        add(f"{n} running" if n else "Nothing running")
        if core.reg_error:
            add("Registry unreadable — open portman", self.show_window)
        if active:
            menu.append(Gtk.SeparatorMenuItem())
        marks = {"running": "●", "foreign": "◆", "noport": "◐", "starting": "…", "stopping": "…"}
        for name in active:
            state, port = core.status[name]["state"], core.projects[name].get("port")
            item = add(f"{marks.get(state, '●')}  {name}   :{port}", lambda: None)
            sub = Gtk.Menu()
            add("Open in browser", lambda n=name: core.open_browser(n), sub,
                state in ("running", "foreign"))
            add("Stop", lambda n=name: core.stop(n), sub)
            add("Restart", lambda n=name: core.restart(n), sub,
                core.why_not_startable(name) is None)
            add("View log", lambda n=name: self.show_window(log=n), sub, log_path(name).exists())
            item.set_submenu(sub)
        if stopped:
            menu.append(Gtk.SeparatorMenuItem())
        for name in stopped:
            port, why = core.projects[name].get("port"), core.why_not_startable(name)
            item = add(f"○  {name}   :{port}", lambda: None)
            sub = Gtk.Menu()
            add("Start", lambda n=name: core.start(n), sub, why is None)
            add("Start and open in browser", lambda n=name: core.start(n, open_after=True), sub,
                why is None)
            if why:
                add(why, None, sub)
            add("View log", lambda n=name: self.show_window(log=n), sub, log_path(name).exists())
            item.set_submenu(sub)
        menu.append(Gtk.SeparatorMenuItem())
        total = len(core.projects)
        opener = add(f"Open portman…   ({total} project{'' if total == 1 else 's'})",
                     self.show_window)
        add("Quit", self.quit_app)
        menu.show_all()
        # Drop the middle-click target first: it belongs to the menu being replaced.
        self.indicator.set_secondary_activate_target(None)
        self.menu = menu
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(opener)

    def _on_event(self, level: str, name, text: str):
        if self.window_visible():
            return
        try:
            icon = "dialog-error" if level == "error" else "network-server"
            Notify.Notification.new("portman", text, icon).show()
        except Exception:
            pass

    window_visible = lambda self: False     # replaced by the app

# ── App ───────────────────────────────────────────────────────────────────────

def confirm_dialog(text: str, detail: str, parent=None) -> bool:
    dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                            message_type=Gtk.MessageType.QUESTION,
                            buttons=Gtk.ButtonsType.NONE, text=text)
    dlg.format_secondary_text(detail)
    dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
    go = dlg.add_button("Continue", Gtk.ResponseType.OK)
    go.get_style_context().add_class("destructive-action")
    dlg.set_default_response(Gtk.ResponseType.CANCEL)
    if parent is None:
        dlg.set_keep_above(True)
    answer = dlg.run()
    dlg.destroy()
    return answer == Gtk.ResponseType.OK


class App(Gtk.Application):
    """One instance per session. Launching it again (`portman ui`, or the app
    launcher) opens the window of the copy already in the tray."""

    def __init__(self, unique: bool = True):
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if not unique:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=DBUS_ID, flags=flags)
        self.core = self.tray = self.window = None

    def do_command_line(self, command_line):
        first = self.core is None
        if first:
            install_css()
            self.core = Core()
            self.core.confirm = confirm_dialog
            self.tray = Tray(self.core, self.show_window, self.quit)
            self.tray.window_visible = lambda: bool(self.window and self.window.is_active())
            self.core.run()
            self.hold()
        if "--show" in command_line.get_arguments()[1:] or not first:
            self.show_window()
        return 0

    def show_window(self, log: str | None = None):
        if self.window is None:
            self.window = MainWindow(self.core)
            self.window.connect("destroy", lambda _w: setattr(self, "window", None))
        self.window.present_with_time(Gtk.get_current_event_time() or int(time.time()))
        if log:
            self.window.show_log(log)


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = App()
    try:
        app.register(None)
    except GLib.Error:
        app = App(unique=False)     # no session bus: run without the single-instance check
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
