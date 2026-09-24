"""
App Switchboard: start and stop your local Flask, Streamlit and Django apps from a browser.

    pip install -r requirements.txt
    python run.py              # opens http://127.0.0.1:5050

Works on Windows, macOS and Linux. Apps keep running if you close the switchboard,
and it picks them back up the next time it starts.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import psutil
from flask import Flask, jsonify, request, send_from_directory

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"
CONFIG_FILE = ROOT_DIR / "config" / "apps.json"   # your app list (not committed)
STATE_FILE = ROOT_DIR / "data" / "state.json"     # PIDs of running apps (not committed)
LOG_DIR = ROOT_DIR / "logs"

ADMIN_HOST = os.environ.get("SWITCHBOARD_HOST", "127.0.0.1")
ADMIN_PORT = int(os.environ.get("SWITCHBOARD_PORT", "5050"))
IS_WINDOWS = os.name == "nt"
APP_TYPES = ("flask", "streamlit", "django", "custom")
MAX_LOG_BYTES = 5 * 1024 * 1024
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

for _d in (LOG_DIR, CONFIG_FILE.parent, STATE_FILE.parent):
    _d.mkdir(parents=True, exist_ok=True)
lock = threading.RLock()
popen_handles: dict[str, subprocess.Popen] = {}  # kept so finished children get reaped


# ---------------------------------------------------------------- storage

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_apps() -> list[dict]:
    data = _read_json(CONFIG_FILE, {"apps": []})
    return data.get("apps", []) if isinstance(data, dict) else []


def save_apps(apps: list[dict]) -> None:
    _write_json(CONFIG_FILE, {"apps": apps})


def find_app(app_id: str) -> dict:
    for a in load_apps():
        if a["id"] == app_id:
            return a
    raise LookupError(f"No app with id '{app_id}'.")


def _state() -> dict:
    return _read_json(STATE_FILE, {})


def _set_state(app_id: str, entry: dict | None) -> None:
    state = _state()
    if entry is None:
        state.pop(app_id, None)
    else:
        state[app_id] = entry
    _write_json(STATE_FILE, state)


# ---------------------------------------------------------------- processes

def _tracked_process(app_id: str) -> psutil.Process | None:
    """The live process we launched for this app, or None."""
    handle = popen_handles.get(app_id)
    if handle is not None and handle.poll() is not None:
        popen_handles.pop(app_id, None)  # reap it so it doesn't linger as a zombie

    entry = _state().get(app_id)
    if not entry:
        return None
    try:
        proc = psutil.Process(entry["pid"])
        # Guard against the OS reusing the PID for an unrelated program.
        if abs(proc.create_time() - entry["create_time"]) > 1:
            return None
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _kill_tree(proc: psutil.Process, timeout: float = 6) -> None:
    """Stop a process and everything it spawned (reloaders, venv launchers, workers)."""
    try:
        family = proc.children(recursive=True) + [proc]
    except psutil.NoSuchProcess:
        return
    for p in family:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(family, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=3)


def _tree_memory_mb(proc: psutil.Process) -> int | None:
    total = 0
    try:
        for p in [proc] + proc.children(recursive=True):
            try:
                total += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except psutil.NoSuchProcess:
        return None
    return round(total / (1024 * 1024))


def _probe_host(app: dict) -> str:
    host = app.get("host") or "127.0.0.1"
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, int(port))) == 0


# ---------------------------------------------------------------- commands

def _venv_dir(app: dict) -> Path | None:
    venv = (app.get("venv") or "").strip()
    if not venv:
        return None
    p = Path(venv).expanduser()
    return p if p.is_absolute() else Path(app["path"]) / p


def resolve_python(app: dict) -> str:
    venv = _venv_dir(app)
    if venv is None:
        return sys.executable
    py = venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    if not py.exists():
        raise ValueError(f"No Python found in the virtualenv. Expected {py}")
    return str(py)


def build_command(app: dict) -> list[str]:
    py = resolve_python(app)
    port = str(app["port"])
    host = app.get("host") or "127.0.0.1"
    entry = (app.get("entry") or "").strip()
    kind = app["type"]

    if kind == "flask":
        return [py, "-m", "flask", "--app", entry or "app", "run", "--host", host, "--port", port]
    if kind == "streamlit":
        return [py, "-m", "streamlit", "run", entry or "app.py",
                "--server.port", port, "--server.address", host, "--server.headless", "true"]
    if kind == "django":
        return [py, entry or "manage.py", "runserver", f"{host}:{port}"]

    raw = (app.get("command") or "").replace("{port}", port).replace("{host}", host)
    parts = shlex.split(raw, posix=not IS_WINDOWS)
    if IS_WINDOWS:
        parts = [p.strip('"') for p in parts]
    parts = [py if p == "{python}" else p for p in parts]
    if not parts:
        raise ValueError("Custom apps need a command.")
    return parts


def _command_preview(app: dict) -> str:
    try:
        return subprocess.list2cmdline(build_command(app)) if IS_WINDOWS else shlex.join(build_command(app))
    except ValueError as e:
        return f"(cannot build command: {e})"


# ---------------------------------------------------------------- logs

def _log_path(app_id: str) -> Path:
    return LOG_DIR / f"{app_id}.log"


def _append_log(app_id: str, line: str) -> None:
    with open(_log_path(app_id), "a", encoding="utf-8") as f:
        f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")


def _rotate_log(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
    except OSError:
        pass


def tail_log(app_id: str, lines: int = 300) -> str:
    path = _log_path(app_id)
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 256 * 1024))
        text = f.read().decode("utf-8", errors="replace")
    return ANSI_RE.sub("", "\n".join(text.splitlines()[-lines:]))


# ---------------------------------------------------------------- actions

def start_app(app: dict) -> None:
    with lock:
        if _tracked_process(app["id"]):
            raise ValueError(f"{app['name']} is already running.")
        if not Path(app["path"]).is_dir():
            raise ValueError(f"Folder not found: {app['path']}")
        if port_in_use(app["port"], _probe_host(app)):
            raise ValueError(f"Port {app['port']} is already used by another program. "
                             "Close it or give this app a different port.")

        cmd = build_command(app)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"  # so logs appear immediately
        venv = _venv_dir(app)
        if venv:
            env["VIRTUAL_ENV"] = str(venv)
            env["PATH"] = str(venv / ("Scripts" if IS_WINDOWS else "bin")) + os.pathsep + env.get("PATH", "")
            env.pop("PYTHONHOME", None)
        env.update({k: str(v) for k, v in (app.get("env") or {}).items()})

        # Detach from the switchboard so apps survive it being closed.
        kwargs: dict = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        log_path = _log_path(app["id"])
        _rotate_log(log_path)
        _append_log(app["id"], "starting: " + _command_preview(app))
        try:
            with open(log_path, "ab") as log:
                proc = subprocess.Popen(cmd, cwd=app["path"], stdout=log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env, **kwargs)
        except FileNotFoundError as e:
            raise ValueError(f"Could not launch: {e}") from e

        popen_handles[app["id"]] = proc
        try:
            create_time = psutil.Process(proc.pid).create_time()
        except psutil.NoSuchProcess:
            create_time = time.time()
        _set_state(app["id"], {"pid": proc.pid, "create_time": create_time, "started_at": time.time()})


def stop_app(app_id: str) -> None:
    with lock:
        proc = _tracked_process(app_id)
        if proc:
            _kill_tree(proc)
            _append_log(app_id, "stopped from the switchboard")
        handle = popen_handles.pop(app_id, None)
        if handle:
            try:
                handle.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        _set_state(app_id, None)


def app_status(app: dict) -> dict:
    proc = _tracked_process(app["id"])
    entry = _state().get(app["id"])
    host = "localhost" if _probe_host(app) == "127.0.0.1" else _probe_host(app)
    info = {
        **app,
        "status": "stopped",
        "pid": None,
        "uptime": None,
        "memory_mb": None,
        "port_busy": False,
        "url": f"http://{host}:{app['port']}{app.get('url_path') or '/'}",
        "command_preview": _command_preview(app),
    }
    if proc:
        listening = port_in_use(app["port"], _probe_host(app))
        info.update(
            status="running" if listening else "starting",
            pid=proc.pid,
            uptime=int(time.time() - entry["started_at"]) if entry else None,
            memory_mb=_tree_memory_mb(proc),
        )
    elif entry:
        info["status"] = "exited"  # we started it, and it stopped on its own
    else:
        info["port_busy"] = port_in_use(app["port"], _probe_host(app))
    return info


# ---------------------------------------------------------------- validation

def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _parse_env(value) -> dict:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    env = {}
    for line in str(value or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Environment line needs KEY=VALUE: {line}")
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def validate_app(payload: dict, existing_id: str | None = None) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("Give the app a name.")

    kind = payload.get("type")
    if kind not in APP_TYPES:
        raise ValueError("Choose Flask, Streamlit, Django or Custom.")

    raw_path = str(payload.get("path", "")).strip().strip('"')
    path = Path(raw_path).expanduser()
    if not raw_path or not path.is_dir():
        raise ValueError(f"Folder not found: {raw_path or '(empty)'}")

    try:
        port = int(payload.get("port"))
        assert 1 <= port <= 65535
    except (TypeError, ValueError, AssertionError):
        raise ValueError("Port must be a number between 1 and 65535.")
    for other in load_apps():
        if other["id"] != existing_id and int(other["port"]) == port:
            raise ValueError(f"Port {port} is already assigned to {other['name']}.")

    command = str(payload.get("command", "")).strip()
    if kind == "custom" and not command:
        raise ValueError("Custom apps need a command, for example: {python} server.py --port {port}")

    app = {
        "name": name,
        "type": kind,
        "path": str(path.resolve()),
        "port": port,
        "host": str(payload.get("host", "")).strip() or "127.0.0.1",
        "entry": str(payload.get("entry", "")).strip(),
        "venv": str(payload.get("venv", "")).strip(),
        "command": command if kind == "custom" else "",
        "url_path": str(payload.get("url_path", "")).strip() or "/",
        "env": _parse_env(payload.get("env")),
    }
    resolve_python(app)  # fail early if the virtualenv path is wrong
    return app


# ---------------------------------------------------------------- web

server = Flask(__name__, static_folder=None)


@server.errorhandler(ValueError)
def _bad_request(e):
    return jsonify(error=str(e)), 400


@server.errorhandler(LookupError)
def _not_found(e):
    return jsonify(error=str(e)), 404


@server.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@server.get("/api/apps")
def list_apps():
    return jsonify(apps=[app_status(a) for a in load_apps()])


@server.post("/api/apps")
def add_app():
    with lock:
        apps = load_apps()
        app = validate_app(request.get_json(force=True))
        app = {"id": _slug(app["name"], {a["id"] for a in apps}), **app}
        apps.append(app)
        save_apps(apps)
    return jsonify(app=app_status(app)), 201


@server.put("/api/apps/<app_id>")
def edit_app(app_id):
    with lock:
        find_app(app_id)
        if _tracked_process(app_id):
            raise ValueError("Stop the app before editing it.")
        updated = {"id": app_id, **validate_app(request.get_json(force=True), existing_id=app_id)}
        save_apps([updated if a["id"] == app_id else a for a in load_apps()])
    return jsonify(app=app_status(updated))


@server.delete("/api/apps/<app_id>")
def delete_app(app_id):
    with lock:
        find_app(app_id)
        if _tracked_process(app_id):
            raise ValueError("Stop the app before removing it.")
        save_apps([a for a in load_apps() if a["id"] != app_id])
        _set_state(app_id, None)
    return jsonify(ok=True)


@server.post("/api/apps/<app_id>/<action>")
def act(app_id, action):
    app = find_app(app_id)
    if action == "start":
        start_app(app)
    elif action == "stop":
        stop_app(app_id)
    elif action == "restart":
        stop_app(app_id)
        start_app(app)
    else:
        raise LookupError(f"Unknown action '{action}'.")
    return jsonify(app=app_status(app))


@server.post("/api/stop-all")
def stop_all():
    for a in load_apps():
        stop_app(a["id"])
    return jsonify(ok=True)


# Runs in a child process: Tk must own its main thread (macOS insists), which Flask's
# request threads can't give it. Prints the chosen folder as JSON, "" if cancelled.
_PICK_FOLDER_SCRIPT = """
import json, sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)  # show above the browser window
path = filedialog.askdirectory(parent=root, initialdir=sys.argv[1] or None, title="Choose the app folder")
print(json.dumps(path or ""))
"""
picker_lock = threading.Lock()


@server.post("/api/pick-folder")
def pick_folder():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        raise ValueError("The folder picker opens on the computer running the switchboard, "
                         "so it only works from that computer. Type the path instead.")
    if not picker_lock.acquire(blocking=False):
        raise ValueError("A folder picker is already open. Check your taskbar.")
    try:
        start = str((request.get_json(silent=True) or {}).get("initial", "")).strip().strip('"')
        start = str(Path(start).expanduser()) if start and Path(start).expanduser().is_dir() else ""
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        result = subprocess.run([sys.executable, "-c", _PICK_FOLDER_SCRIPT, start],
                                capture_output=True, text=True, **kwargs)
        try:
            path = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return jsonify(error="No folder picker is available on this system (Python's tkinter "
                                 "is missing). Type the path instead."), 501
    finally:
        picker_lock.release()
    return jsonify(path=str(Path(path)) if path else None)


@server.get("/api/apps/<app_id>/logs")
def logs(app_id):
    find_app(app_id)
    lines = min(int(request.args.get("lines", 300)), 2000)
    return jsonify(log=tail_log(app_id, lines))


def main() -> None:
    if not CONFIG_FILE.exists():
        save_apps([])
    url = f"http://{ADMIN_HOST}:{ADMIN_PORT}"
    print(f"App Switchboard is running at {url}  (Ctrl+C to quit; your apps keep running)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.run(host=ADMIN_HOST, port=ADMIN_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
