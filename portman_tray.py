#!/usr/bin/env python3
"""
portman — Dev Port Manager
Linux system tray app.
Click the tray icon to see all registered projects,
start/stop/restart them with one click, and add new ones.

Author: Statotech Systems
"""

__version__ = "1.0.0"
__author__ = "Statotech Systems"

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
gi.require_version('Notify', '0.7')

from gi.repository import Gtk, AppIndicator3, GLib, Notify, Gdk, Pango
import cairo
import json
import os
import re
import sys
import subprocess
import shlex
import signal
import threading
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

APP_ID      = "portman"
APP_NAME    = "portman"
REGISTRY    = Path.home() / ".portman.json"
ICON_DIR    = Path("/tmp/portman")
REFRESH_SEC = 4   # how often to refresh port status

# ── Registry ──────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1
BACKUP = REGISTRY.with_suffix(REGISTRY.suffix + ".bak")

# The CLI (portman.js) writes this same file. Both writers must be atomic, and
# neither may treat a corrupt registry as an empty one: that used to make the
# next write silently destroy every entry.
registry_error: str | None = None

def load_registry() -> dict:
    global registry_error
    if not REGISTRY.exists():
        registry_error = None
        return {"schemaVersion": SCHEMA_VERSION, "projects": {}}
    try:
        raw = REGISTRY.read_text()
    except Exception as e:
        registry_error = f"cannot read {REGISTRY}: {e}"
        return {"projects": {}, "_unreadable": True}
    if not raw.strip():
        registry_error = None
        return {"schemaVersion": SCHEMA_VERSION, "projects": {}}
    try:
        reg = json.loads(raw)
    except Exception as e:
        registry_error = f"{REGISTRY} is not valid JSON ({e}). Restore: cp {BACKUP} {REGISTRY}"
        return {"projects": {}, "_unreadable": True}
    if not isinstance(reg, dict) or not isinstance(reg.get("projects"), dict):
        registry_error = f"{REGISTRY} has no 'projects' object"
        return {"projects": {}, "_unreadable": True}
    registry_error = None
    return reg

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
    tmp = REGISTRY.with_suffix(REGISTRY.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        f.write(json.dumps(reg, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, REGISTRY)

# ── Port detection ─────────────────────────────────────────────────────────────

def probe_port(port: int) -> str:
    """'free' | 'busy' | 'unknown'. 'unknown' means the query itself failed —
    folding that into 'free' is what made the tray show live servers as stopped
    and report kills that did nothing as successful."""
    r = subprocess.run(f'ss -tlnp "sport = :{port}"', shell=True,
                       capture_output=True, text=True)
    if r.returncode == 0:
        rows = [l for i, l in enumerate((r.stdout or "").strip().splitlines())
                if i > 0 and l.strip()]
        return "busy" if rows else "free"
    r = subprocess.run(f'lsof -i :{port} -sTCP:LISTEN -Fpc', shell=True,
                       capture_output=True, text=True)
    if r.returncode in (0, 1):
        return "busy" if (r.stdout or "").strip() else "free"
    return "unknown"

def port_in_use(port: int) -> bool:
    return probe_port(port) == "busy"

def get_port_info(port: int) -> dict | None:
    """Return {pid, cmd} or None if port is free."""
    try:
        out = subprocess.check_output(
            f'ss -tlnp "sport = :{port}" 2>/dev/null',
            shell=True, text=True
        ).strip()
        lines = [l for i, l in enumerate(out.splitlines()) if i > 0 and l.strip()]
        if not lines:
            return None
        line = "\n".join(lines)
        pid = None
        cmd = None
        import re
        m = re.search(r'pid=(\d+)', line)
        if m:
            pid = int(m.group(1))
            try:
                cmd = subprocess.check_output(
                    f'ps -p {pid} -o comm= 2>/dev/null',
                    shell=True, text=True
                ).strip()
            except Exception:
                pass
        if pid is None:
            # maybe docker
            try:
                docker_out = subprocess.check_output(
                    "docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null",
                    shell=True, text=True
                ).strip()
                for dline in docker_out.splitlines():
                    if f':{port}->' in dline:
                        cmd = f"docker: {dline.split(chr(9))[0]}"
                        break
            except Exception:
                pass
        return {"pid": pid, "cmd": cmd}
    except Exception:
        return None

# Ports belonging to the OS or a database. The tray is one mis-click away from
# killing these, so it refuses them outright.
PROTECTED_PORTS = {
    22, 25, 53, 67, 68, 111, 123, 139, 143, 389, 445, 465, 587, 631, 993, 995,
    1433, 3306, 5353, 5432, 6379, 9200, 11211, 27017,
}

def kill_port(port: int) -> bool:
    """Kill whatever is on a port. Returns True if freed."""
    if port in PROTECTED_PORTS:
        return False
    # docker?
    try:
        docker_out = subprocess.check_output(
            "docker ps --format '{{.ID}}\t{{.Ports}}' 2>/dev/null",
            shell=True, text=True
        ).strip()
        for line in docker_out.splitlines():
            if f':{port}->' in line:
                container_id = line.split('\t')[0].strip()
                subprocess.run(f'docker stop {container_id}', shell=True,
                               capture_output=True)
                return True
    except Exception:
        pass

    state = probe_port(port)
    if state == "unknown":
        return False  # cannot see the port; saying "freed" here would be a lie
    if state == "free":
        return True
    info = get_port_info(port)
    if not info:
        return False
    pid = info.get('pid')
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(15):
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
            os.kill(pid, signal.SIGKILL)
            return True
        except Exception:
            pass
    # Last resort. fuser -k SIGKILLs every process on the port with no grace
    # period, so it is only for a holder we could not identify a pid for — and
    # we verify afterwards instead of assuming it worked.
    try:
        subprocess.run(f'fuser -k {port}/tcp 2>/dev/null', shell=True,
                       capture_output=True)
    except Exception:
        return False
    time.sleep(0.3)
    return probe_port(port) == "free"

# ── Icon generation ────────────────────────────────────────────────────────────

def write_icon(has_running: bool, has_error: bool = False) -> str:
    ICON_DIR.mkdir(exist_ok=True)
    icon_path = ICON_DIR / "portman.png"

    size = 32
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)

    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)

    # Color: green if something running, gray otherwise
    if has_error:
        r, g, b = 0.42, 0.45, 0.50
    elif has_running:
        r, g, b = 0.13, 0.77, 0.37
    else:
        r, g, b = 0.39, 0.45, 0.55

    # Background circle
    ctx.set_source_rgba(r, g, b, 0.2)
    ctx.arc(size/2, size/2, 13, 0, 2 * 3.14159)
    ctx.fill()

    # Border circle
    ctx.set_source_rgb(r, g, b)
    ctx.set_line_width(2)
    ctx.arc(size/2, size/2, 13, 0, 2 * 3.14159)
    ctx.stroke()

    # "P" letter
    ctx.set_source_rgb(r, g, b)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(20)
    text = "P"
    xb, yb, w, h, _, _ = ctx.text_extents(text)
    ctx.move_to(size/2 - w/2 - xb, size/2 - h/2 - yb)
    ctx.show_text(text)

    surface.write_to_png(str(icon_path))
    return str(icon_path)

# ── Running processes (managed by tray) ───────────────────────────────────────

# name → subprocess.Popen
managed_procs: dict[str, subprocess.Popen] = {}

def inject_port(cmd: str, port: int) -> str:
    """Mirror of injectPort() in portman.js. The tray used to set only the PORT
    env var, so a Django or vite project started from the tray bound the
    framework default instead of its registered port — and then the tray showed
    the row as stopped, because the registered port really was free."""
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

def start_project(name: str, proj: dict) -> tuple[bool, str]:
    """Start a project. Returns (success, message)."""
    if name in managed_procs and managed_procs[name].poll() is None:
        return False, "Already running"

    start_cmd = proj.get("startCmd")
    if not start_cmd:
        return False, "No start command. Edit the project to set one."

    work_dir = proj.get("dir", str(Path.home()))
    if not Path(work_dir).exists():
        return False, f"Directory not found: {work_dir}"

    port = proj.get("port")

    # Free port first
    if port and port_in_use(port):
        kill_port(port)

    try:
        env = os.environ.copy()
        if port:
            env["PORT"] = str(port)

        # Use shell=True so nvm/pyenv/etc. commands resolve correctly
        proc = subprocess.Popen(
            inject_port(start_cmd, port),
            shell=True,
            cwd=work_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        managed_procs[name] = proc

        # Give it 1s to fail fast (e.g. command not found)
        try:
            proc.wait(timeout=1)
            if proc.returncode != 0:
                err = (proc.stderr.read() or b"").decode().strip()
                del managed_procs[name]
                return False, err or f"Process exited immediately (code {proc.returncode})"
        except subprocess.TimeoutExpired:
            pass  # Still running — good

        return True, f"Started (pid {proc.pid})"
    except Exception as e:
        return False, str(e)

def stop_project(name: str, proj: dict) -> tuple[bool, str]:
    """Stop a project."""
    proc = managed_procs.get(name)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
        del managed_procs[name]
        return True, "Stopped"

    # Fall back to killing by port
    port = proj.get("port")
    if port and kill_port(port):
        return True, "Stopped (killed by port)"
    return False, "Not running"

def is_managed_running(name: str) -> bool:
    proc = managed_procs.get(name)
    return proc is not None and proc.poll() is None

# ── Detail window ──────────────────────────────────────────────────────────────

class ProjectDetailWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="portman — Dev Ports")
        self.app = app
        self.set_default_size(460, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.MOUSE)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_keep_above(True)
        self._suppress_focus_out = False
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("key-press-event", self._on_key)
        self._apply_css()
        self._build_ui()
        self.show_all()

    def _apply_css(self):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window { background-color: #0f1117; }
            .title-lbl  { color: #06b6d4; font-size: 16px; font-weight: bold; }
            .section-lbl{ color: #64748b; font-size: 10px; font-weight: bold; letter-spacing: 2px; }
            .proj-name  { color: #e2e8f0; font-size: 13px; font-weight: bold; }
            .proj-port  { color: #06b6d4; font-size: 12px; font-weight: bold; font-family: monospace; }
            .proj-cmd   { color: #eab308; font-size: 11px; font-family: monospace; }
            .proj-dir   { color: #64748b; font-size: 11px; font-family: monospace; }
            .status-run { color: #22c55e; font-size: 11px; }
            .status-stop{ color: #64748b; font-size: 11px; }
            .status-docker { color: #a855f7; font-size: 11px; }
            .btn-start  { background: #22c55e; color: #000; border-radius: 5px; padding: 3px 10px; font-size: 11px; font-weight: bold; border: none; }
            .btn-stop   { background: #ef4444; color: #fff; border-radius: 5px; padding: 3px 10px; font-size: 11px; font-weight: bold; border: none; }
            .btn-restart{ background: #1e2333; color: #94a3b8; border-radius: 5px; padding: 3px 10px; font-size: 11px; border: 1px solid #2a3050; }
            .btn-add    { background: #3b82f6; color: #fff; border-radius: 6px; padding: 5px 14px; font-size: 12px; font-weight: bold; border: none; }
            .btn-edit   { background: #1e2333; color: #64748b; border-radius: 5px; padding: 3px 6px; font-size: 11px; border: 1px solid #2a3050; }
            .card       { background-color: #181c27; border-radius: 8px; }
            .separator  { background-color: #1e2333; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _build_ui(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        title = Gtk.Label(label="⚡ portman")
        title.get_style_context().add_class("title-lbl")
        title.set_halign(Gtk.Align.START)
        header.pack_start(title, True, True, 0)

        add_btn = Gtk.Button(label="+ Add Project")
        add_btn.get_style_context().add_class("btn-add")
        add_btn.connect("clicked", lambda _: self._open_add_dialog())
        header.pack_end(add_btn, False, False, 0)
        outer.pack_start(header, False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 10)

        # Project list (scrollable)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_max_content_height(420)
        scroll.set_propagate_natural_height(True)

        self.proj_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._populate_projects()

        scroll.add(self.proj_box)
        outer.pack_start(scroll, True, True, 0)

        # Footer
        outer.pack_start(Gtk.Separator(), False, False, 8)
        footer = Gtk.Label(label=f"portman v{__version__}  •  registry: ~/.portman.json")
        footer.get_style_context().add_class("section-lbl")
        footer.set_halign(Gtk.Align.CENTER)
        outer.pack_start(footer, False, False, 0)

        self.add(outer)

    def _populate_projects(self):
        for child in self.proj_box.get_children():
            self.proj_box.remove(child)

        reg = load_registry()
        projects = reg.get("projects", {})

        # An unreadable registry must never render as "no projects" — that is
        # what made a corrupt file look like an empty one.
        if reg.get("_unreadable"):
            lbl = Gtk.Label()
            lbl.set_markup(
                "<b>Registry unreadable — nothing is being shown.</b>\n"
                + GLib.markup_escape_text(registry_error or "unknown error")
                + "\n\nAdding or starting anything is disabled until this is fixed."
            )
            lbl.get_style_context().add_class("proj-dir")
            lbl.set_justify(Gtk.Justification.CENTER)
            lbl.set_line_wrap(True)
            lbl.set_margin_top(20)
            self.proj_box.pack_start(lbl, False, False, 0)
            self.proj_box.show_all()
            return

        if not projects:
            lbl = Gtk.Label(label="No projects registered.\nClick '+ Add Project' to get started.")
            lbl.get_style_context().add_class("proj-dir")
            lbl.set_justify(Gtk.Justification.CENTER)
            lbl.set_margin_top(20)
            self.proj_box.pack_start(lbl, False, False, 0)
        else:
            # Group projects
            groups = {}
            standalone = []
            for name, proj in projects.items():
                g = proj.get("group")
                if g:
                    groups.setdefault(g, []).append((name, proj))
                else:
                    standalone.append((name, proj))

            # Render grouped projects as a single combined card
            for group_name, members in groups.items():
                self.proj_box.pack_start(self._make_group_card(group_name, members), False, False, 0)

            # Render standalone projects
            for name, proj in standalone:
                self.proj_box.pack_start(self._make_card(name, proj), False, False, 0)

        self.proj_box.show_all()

    def _make_group_card(self, group_name: str, members: list) -> Gtk.Box:
        """Card for a group of services (frontend + backend etc)."""
        all_running = all(port_in_use(p.get("port", 0)) for _, p in members)
        any_running = any(port_in_use(p.get("port", 0)) for _, p in members)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.get_style_context().add_class("card")
        outer.set_margin_bottom(2)

        # Group header row
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(10)
        header.set_margin_start(14)
        header.set_margin_end(14)

        dot = "●" if all_running else ("◑" if any_running else "○")
        dot_lbl = Gtk.Label(label=dot)
        dot_lbl.get_style_context().add_class("status-run" if all_running else ("status-stop" if not any_running else "status-docker"))
        header.pack_start(dot_lbl, False, False, 0)

        name_lbl = Gtk.Label(label=group_name)
        name_lbl.get_style_context().add_class("proj-name")
        header.pack_start(name_lbl, False, False, 0)

        count_lbl = Gtk.Label(label=f"({len(members)} services)")
        count_lbl.get_style_context().add_class("proj-dir")
        header.pack_start(count_lbl, False, False, 0)

        # Group-level start/stop all buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_box.set_valign(Gtk.Align.CENTER)

        if any_running:
            stop_all = Gtk.Button(label="■ Stop all")
            stop_all.get_style_context().add_class("btn-stop")
            stop_all.connect("clicked", lambda _, g=group_name, m=members: self._group_action(g, m, "stop"))
            btn_box.pack_start(stop_all, False, False, 0)
        else:
            start_all = Gtk.Button(label="▶ Start all")
            start_all.get_style_context().add_class("btn-start")
            start_all.connect("clicked", lambda _, g=group_name, m=members: self._group_action(g, m, "start"))
            btn_box.pack_start(start_all, False, False, 0)

        restart_all = Gtk.Button(label="↺")
        restart_all.get_style_context().add_class("btn-restart")
        restart_all.set_tooltip_text("Restart all")
        restart_all.connect("clicked", lambda _, g=group_name, m=members: self._group_action(g, m, "restart"))
        btn_box.pack_start(restart_all, False, False, 0)

        header.pack_end(btn_box, False, False, 0)
        outer.pack_start(header, False, False, 0)

        # Separator
        outer.pack_start(Gtk.Separator(), False, False, 2)

        # Individual service rows
        for name, proj in members:
            service_label = name.replace(f"{group_name}:", "")
            row = self._make_service_row(name, service_label, proj)
            outer.pack_start(row, False, False, 0)

        outer.set_margin_bottom(6)
        return outer

    def _make_service_row(self, name: str, label: str, proj: dict) -> Gtk.Box:
        """A compact row for a service inside a group card."""
        port = proj.get("port")
        start_cmd = proj.get("startCmd") or ""
        info = get_port_info(port) if port else None
        running = info is not None

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(28)
        row.set_margin_end(14)
        row.set_margin_top(4)
        row.set_margin_bottom(4)

        svc_lbl = Gtk.Label(label=label)
        svc_lbl.get_style_context().add_class("proj-cmd")
        svc_lbl.set_halign(Gtk.Align.START)
        row.pack_start(svc_lbl, False, False, 0)

        port_lbl = Gtk.Label(label=f":{port}" if port else "")
        port_lbl.get_style_context().add_class("proj-port")
        row.pack_start(port_lbl, False, False, 0)

        status_lbl = Gtk.Label(label="● running" if running else "○ stopped")
        status_lbl.get_style_context().add_class("status-run" if running else "status-stop")
        row.pack_start(status_lbl, False, False, 0)

        # Per-service buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_valign(Gtk.Align.CENTER)

        if running:
            s = Gtk.Button(label="■")
            s.get_style_context().add_class("btn-stop")
            s.set_tooltip_text(f"Stop {label}")
            s.connect("clicked", lambda _, n=name, p=proj: self._action(n, p, "stop"))
            btn_box.pack_start(s, False, False, 0)
        else:
            s = Gtk.Button(label="▶")
            s.get_style_context().add_class("btn-start")
            s.set_tooltip_text(f"Start {label}")
            s.set_sensitive(bool(start_cmd))
            s.connect("clicked", lambda _, n=name, p=proj: self._action(n, p, "start"))
            btn_box.pack_start(s, False, False, 0)

        edit_btn = Gtk.Button(label="⋮")
        edit_btn.get_style_context().add_class("btn-edit")
        edit_btn.set_tooltip_text("Edit / Remove")
        edit_btn.connect("clicked", lambda _, n=name, p=proj, b=edit_btn: self._show_context_menu(n, p, b))
        btn_box.pack_start(edit_btn, False, False, 0)

        row.pack_end(btn_box, False, False, 0)
        return row

    def _group_action(self, group_name: str, members: list, action: str):
        def run():
            for name, proj in members:
                if action == "start":
                    start_project(name, proj)
                elif action == "stop":
                    stop_project(name, proj)
                elif action == "restart":
                    stop_project(name, proj)
                    time.sleep(0.5)
                    start_project(name, proj)
            GLib.idle_add(self._refresh)
            GLib.idle_add(self.app.refresh_icon)
        threading.Thread(target=run, daemon=True).start()

    def _make_card(self, name: str, proj: dict) -> Gtk.Box:
        port = proj.get("port")
        start_cmd = proj.get("startCmd") or ""
        work_dir = (proj.get("dir") or "").replace(str(Path.home()), "~")

        info = get_port_info(port) if port else None
        running = info is not None
        managed = is_managed_running(name)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.get_style_context().add_class("card")
        card.set_margin_bottom(2)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        inner.set_margin_top(10)
        inner.set_margin_bottom(10)
        inner.set_margin_start(14)
        inner.set_margin_end(14)

        # Left: info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)

        # Name + port row
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_lbl = Gtk.Label(label=name)
        name_lbl.get_style_context().add_class("proj-name")
        name_lbl.set_halign(Gtk.Align.START)
        name_row.pack_start(name_lbl, False, False, 0)

        port_lbl = Gtk.Label(label=f":{port}" if port else "")
        port_lbl.get_style_context().add_class("proj-port")
        name_row.pack_start(port_lbl, False, False, 0)

        # Status indicator
        if running:
            docker_name = info.get("cmd", "") if info else ""
            is_docker = docker_name and docker_name.startswith("docker:")
            if is_docker:
                status_lbl = Gtk.Label(label=f"● {docker_name}")
                status_lbl.get_style_context().add_class("status-docker")
            else:
                pid_str = f"pid {info['pid']}" if info and info.get('pid') else "running"
                status_lbl = Gtk.Label(label=f"● {pid_str}")
                status_lbl.get_style_context().add_class("status-run")
        else:
            status_lbl = Gtk.Label(label="○ stopped")
            status_lbl.get_style_context().add_class("status-stop")
        name_row.pack_end(status_lbl, False, False, 0)
        info_box.pack_start(name_row, False, False, 0)

        # Command
        if start_cmd:
            cmd_lbl = Gtk.Label(label=start_cmd)
            cmd_lbl.get_style_context().add_class("proj-cmd")
            cmd_lbl.set_halign(Gtk.Align.START)
            cmd_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            cmd_lbl.set_max_width_chars(38)
            info_box.pack_start(cmd_lbl, False, False, 0)
        else:
            no_cmd = Gtk.Label(label="no start command set")
            no_cmd.get_style_context().add_class("proj-dir")
            no_cmd.set_halign(Gtk.Align.START)
            info_box.pack_start(no_cmd, False, False, 0)

        # Dir
        dir_lbl = Gtk.Label(label=work_dir)
        dir_lbl.get_style_context().add_class("proj-dir")
        dir_lbl.set_halign(Gtk.Align.START)
        dir_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        dir_lbl.set_max_width_chars(38)
        info_box.pack_start(dir_lbl, False, False, 0)

        inner.pack_start(info_box, True, True, 0)

        # Right: buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_box.set_valign(Gtk.Align.CENTER)

        if running:
            stop_btn = Gtk.Button(label="■ Stop")
            stop_btn.get_style_context().add_class("btn-stop")
            stop_btn.connect("clicked", lambda _, n=name, p=proj: self._action(n, p, "stop"))
            btn_box.pack_start(stop_btn, False, False, 0)

            restart_btn = Gtk.Button(label="↺")
            restart_btn.get_style_context().add_class("btn-restart")
            restart_btn.set_tooltip_text("Restart")
            restart_btn.connect("clicked", lambda _, n=name, p=proj: self._action(n, p, "restart"))
            btn_box.pack_start(restart_btn, False, False, 0)
        else:
            start_btn = Gtk.Button(label="▶ Start")
            start_btn.get_style_context().add_class("btn-start")
            start_btn.set_sensitive(bool(start_cmd))
            start_btn.connect("clicked", lambda _, n=name, p=proj: self._action(n, p, "start"))
            btn_box.pack_start(start_btn, False, False, 0)

        # Edit menu button
        edit_btn = Gtk.Button(label="⋮")
        edit_btn.get_style_context().add_class("btn-restart")
        edit_btn.set_tooltip_text("Edit / Remove")
        edit_btn.connect("clicked", lambda _, n=name, p=proj, b=edit_btn: self._show_context_menu(n, p, b))
        btn_box.pack_start(edit_btn, False, False, 0)

        inner.pack_end(btn_box, False, False, 0)
        card.pack_start(inner, False, False, 0)
        return card

    def _action(self, name: str, proj: dict, action: str):
        # Reload proj from registry so we always have fresh data
        reg = load_registry()
        proj = reg.get("projects", {}).get(name, proj)

        def run():
            if action == "start":
                ok, msg = start_project(name, proj)
            elif action == "stop":
                ok, msg = stop_project(name, proj)
            elif action == "restart":
                stop_project(name, proj)
                time.sleep(0.7)
                ok, msg = start_project(name, proj)
            else:
                return
            GLib.idle_add(self._refresh)
            GLib.idle_add(self.app.refresh_icon)
            if not ok:
                GLib.idle_add(self._show_error, msg)

        threading.Thread(target=run, daemon=True).start()

    def _refresh(self):
        self._populate_projects()
        return False  # don't repeat

    def _show_context_menu(self, name: str, proj: dict, widget: Gtk.Widget):
        menu = Gtk.Menu()

        edit_item = Gtk.MenuItem(label="Edit")
        edit_item.connect("activate", lambda _: self._open_edit_dialog(name, proj))
        menu.append(edit_item)

        remove_item = Gtk.MenuItem(label="Remove")
        remove_item.connect("activate", lambda _: self._remove_project(name))
        menu.append(remove_item)

        # Suppress focus-out while menu is open so the window doesn't destroy itself
        self._suppress_focus_out = True
        menu.connect("hide", lambda _: setattr(self, '_suppress_focus_out', False))

        menu.show_all()
        menu.popup_at_widget(widget, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def _remove_project(self, name: str):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f'Remove "{name}" from portman?'
        )
        resp = dialog.run()
        dialog.destroy()
        if resp == Gtk.ResponseType.YES:
            reg = load_registry()
            proj = reg["projects"].get(name)
            if proj:
                port = proj.get("port")
                if port and port_in_use(port):
                    stop_project(name, proj)
                del reg["projects"][name]
                save_registry(reg)
            self._refresh()
            self.app.refresh_icon()

    def _open_add_dialog(self):
        self._open_project_dialog(None, None)

    def _open_edit_dialog(self, name: str, proj: dict):
        self._open_project_dialog(name, proj)

    def _open_project_dialog(self, name, proj):
        dialog = Gtk.Dialog(
            title="Add Project" if not name else f"Edit {name}",
            transient_for=self,
            flags=Gtk.DialogFlags.MODAL,
        )
        dialog.set_default_size(380, -1)
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK,     Gtk.ResponseType.OK,
        )

        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_margin_top(16)
        box.set_margin_bottom(8)
        box.set_margin_start(16)
        box.set_margin_end(16)

        def field(label_text, value="", editable=True):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            lbl = Gtk.Label(label=label_text)
            lbl.set_halign(Gtk.Align.START)
            entry = Gtk.Entry()
            entry.set_text(value or "")
            entry.set_sensitive(editable)
            row.pack_start(lbl, False, False, 0)
            row.pack_start(entry, False, False, 0)
            box.pack_start(row, False, False, 0)
            return entry

        name_entry  = field("Name",          name or "",                    not bool(name))
        port_entry  = field("Port",           str(proj["port"]) if proj else "")
        dir_entry   = field("Directory",      proj.get("dir","") if proj else str(Path.home()))
        cmd_entry   = field("Start Command",  proj.get("startCmd","") if proj else "npm run dev")

        err_lbl = Gtk.Label(label="")
        err_lbl.set_halign(Gtk.Align.START)
        box.pack_start(err_lbl, False, False, 0)

        dialog.show_all()
        resp = dialog.run()

        if resp == Gtk.ResponseType.OK:
            n    = name_entry.get_text().strip()
            p    = port_entry.get_text().strip()
            d    = dir_entry.get_text().strip()
            cmd  = cmd_entry.get_text().strip()

            if not n or not p:
                dialog.destroy()
                self._show_error("Name and port are required.")
                return

            try:
                p = int(p)
            except ValueError:
                dialog.destroy()
                self._show_error("Port must be a number.")
                return

            reg = load_registry()
            # Check port conflict
            for existing_name, existing_proj in reg["projects"].items():
                if existing_proj["port"] == p and existing_name != n:
                    dialog.destroy()
                    self._show_error(f'Port {p} already used by "{existing_name}".')
                    return

            reg["projects"][n] = {
                "port": p,
                "dir": d or str(Path.home()),
                "startCmd": cmd or None,
                "addedAt": reg["projects"].get(n, {}).get("addedAt"),
            }
            save_registry(reg)
            self._refresh()
            self.app.refresh_icon()

        dialog.destroy()

    def _show_error(self, msg: str):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=msg,
        )
        dlg.run()
        dlg.destroy()

    def _on_focus_out(self, widget, event):
        if not self._suppress_focus_out:
            self.destroy()

    def _on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()

# ── Tray indicator ─────────────────────────────────────────────────────────────

class PortmanTray:
    def __init__(self):
        self._detail_window = None
        self._icon_path = write_icon(has_running=False)

        self.indicator = AppIndicator3.Indicator.new(
            APP_ID,
            self._icon_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self._build_menu())

        Notify.init(APP_NAME)

        # Periodic refresh
        GLib.timeout_add_seconds(REFRESH_SEC, self._periodic_refresh)
        self.refresh_icon()

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        self._menu = menu
        self._proj_items = []  # track dynamic items so we can remove them cleanly

        self._open_item = Gtk.MenuItem(label="⚡ Open portman")
        self._open_item.connect("activate", self._on_open)
        menu.append(self._open_item)

        menu.append(Gtk.SeparatorMenuItem())
        # project items inserted here dynamically
        menu.append(Gtk.SeparatorMenuItem())

        self._quit_item = Gtk.MenuItem(label="Quit")
        self._quit_item.connect("activate", self._on_quit)
        menu.append(self._quit_item)

        menu.show_all()
        return menu

    def _rebuild_menu_projects(self):
        """Rebuild dynamic project items between the two separators."""
        # Remove previously inserted project items
        for item in self._proj_items:
            self._menu.remove(item)
        self._proj_items = []

        # Insert position = after first separator (index 2)
        insert_pos = 2

        reg = load_registry()
        projects = reg.get("projects", {})

        if not projects:
            item = Gtk.MenuItem(label="  No projects registered")
            item.set_sensitive(False)
            self._menu.insert(item, insert_pos)
            self._proj_items.append(item)
        else:
            for idx, (name, proj) in enumerate(projects.items()):
                running = port_in_use(proj.get("port", 0))
                dot = "●" if running else "○"
                port = proj.get("port", "?")
                item = Gtk.MenuItem(label=f"  {dot} {name}  :{port}")
                sub = Gtk.Menu()

                start_i = Gtk.MenuItem(label="▶  Start")
                start_i.connect("activate", lambda _, n=name, p=proj: self._quick_action(n, p, "start"))
                start_i.set_sensitive(not running and bool(proj.get("startCmd")))
                sub.append(start_i)

                stop_i = Gtk.MenuItem(label="■  Stop")
                stop_i.connect("activate", lambda _, n=name, p=proj: self._quick_action(n, p, "stop"))
                stop_i.set_sensitive(running)
                sub.append(stop_i)

                restart_i = Gtk.MenuItem(label="↺  Restart")
                restart_i.connect("activate", lambda _, n=name, p=proj: self._quick_action(n, p, "restart"))
                restart_i.set_sensitive(bool(proj.get("startCmd")))
                sub.append(restart_i)

                item.set_submenu(sub)
                self._menu.insert(item, insert_pos + idx)
                self._proj_items.append(item)

        self._menu.show_all()

    def _quick_action(self, name: str, proj: dict, action: str):
        reg = load_registry()
        proj = reg.get("projects", {}).get(name, proj)

        def run():
            if action == "start":
                start_project(name, proj)
            elif action == "stop":
                stop_project(name, proj)
            elif action == "restart":
                stop_project(name, proj)
                time.sleep(0.7)
                start_project(name, proj)
            GLib.idle_add(self.refresh_icon)

        threading.Thread(target=run, daemon=True).start()

    def refresh_icon(self):
        reg = load_registry()
        has_running = any(
            port_in_use(p.get("port", 0))
            for p in reg.get("projects", {}).values()
            if p.get("port")
        )
        icon_path = write_icon(has_running=has_running)
        self.indicator.set_icon_full(icon_path, "portman")
        self._rebuild_menu_projects()
        return False  # for GLib.timeout_add

    def _periodic_refresh(self):
        self.refresh_icon()
        return True  # keep repeating

    def _on_open(self, _):
        if self._detail_window and self._detail_window.get_visible():
            self._detail_window.destroy()
            self._detail_window = None
            return
        self._detail_window = ProjectDetailWindow(self)
        self._detail_window.connect("destroy", lambda _: setattr(self, '_detail_window', None))

    def _on_quit(self, _):
        for name, proc in list(managed_procs.items()):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            Path("/tmp/portman-tray.pid").unlink(missing_ok=True)
        except Exception:
            pass
        Notify.uninit()
        Gtk.main_quit()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    tray = PortmanTray()
    Gtk.main()
