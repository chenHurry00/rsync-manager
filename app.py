#!/usr/bin/env python3
"""
CodeSync - Python Web UI for rsync-based code repository synchronization
Run: python app.py
Then open: http://localhost:7788
"""

import base64
import json
import os
import posixpath
import shlex
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request

# Simple reversible obfuscation so passwords aren't stored in plaintext.
# Not cryptographically secure — use SSH keys for production.
def _obfuscate(s: str) -> str:
    return base64.b64encode(s.encode()).decode() if s else ""

def _deobfuscate(s: str) -> str:
    try:
        return base64.b64decode(s.encode()).decode() if s else ""
    except Exception:
        return s  # already plaintext (legacy)

app = Flask(__name__)

# ── Config persistence ────────────────────────────────────────────────────────
CONFIG_FILE = Path.home() / ".codesync" / "config.json"

def load_config() -> dict:
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {"servers": [], "repos": [], "sync_history": []}

def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

# ── SSE sync log stream ───────────────────────────────────────────────────────
sync_streams: dict[str, list[str]] = {}   # job_id -> list of log lines
sync_lock = threading.Lock()

def push_log(job_id: str, msg: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = json.dumps({"ts": ts, "msg": msg, "level": level})
    with sync_lock:
        sync_streams.setdefault(job_id, []).append(line)

def build_transport(server: dict) -> tuple[list[str], list[str], dict]:
    """Build the SSH transport pieces shared by push and pull operations."""
    auth_mode = server.get("auth_mode", "key")   # "key" | "password"
    password = _deobfuscate(server.get("password_enc", ""))

    ssh_parts = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=no"]
    if auth_mode == "key" and server.get("key"):
        ssh_parts += ["-i", str(Path(server["key"]).expanduser())]
    if server.get("port") and str(server["port"]) != "22":
        ssh_parts += ["-p", str(server["port"])]
    # Use sshpass for password auth
    prefix = []
    extra_env = {}
    if auth_mode == "password" and password:
        prefix = ["sshpass", "-e"]          # -e reads from env SSHPASS
        extra_env["SSHPASS"] = password

    return prefix, ssh_parts, extra_env

def normalize_relative_remote_path(raw_path: str, allow_root: bool = False) -> str:
    """Keep remote paths anchored inside repo.remote."""
    candidate = str(raw_path or "").strip().replace("\\", "/")
    if not candidate:
        if allow_root:
            return ""
        raise ValueError("远程相对路径不能为空")
    if candidate.startswith("/"):
        raise ValueError("远程路径必须相对于仓库远端根目录")

    normalized = posixpath.normpath(candidate)
    if normalized in {"", "."}:
        if allow_root:
            return ""
        raise ValueError("远程相对路径不能为空")
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("远程路径不能跳出仓库远端根目录")
    return normalized

def get_repo_roots(repo: dict) -> tuple[str, str]:
    local_root = str(Path(repo["local"]).expanduser())
    remote_root = repo["remote"].rstrip("/") or "/"
    return local_root, remote_root

def build_ssh_cmd(server: dict, remote_command: str) -> tuple[list[str], dict]:
    prefix, ssh_parts, extra_env = build_transport(server)
    target = f"{server['user']}@{server['host']}"
    return prefix + ssh_parts + [target, remote_command], extra_env

def record_sync_history(repo: dict, server: dict, opts: dict):
    cfg = load_config()
    cfg.setdefault("sync_history", []).insert(0, {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repo": repo["name"],
        "server": server["name"],
        "status": "success",
        "dry_run": opts.get("dry_run", False),
        "mode": opts.get("mode", "push"),
        "path": opts.get("relative_path", ""),
    })
    cfg["sync_history"] = cfg["sync_history"][:50]
    save_config(cfg)

def build_rsync_cmd(repo: dict, server: dict, opts: dict) -> tuple[list[str], dict]:
    """Returns (cmd_list, extra_env). Password is passed via env so it never
    appears in process listings or log output."""
    mode = opts.get("mode", "push")
    prefix, ssh_parts, extra_env = build_transport(server)
    ssh_str = " ".join(shlex.quote(part) for part in ssh_parts)
    local_root, remote_root = get_repo_roots(repo)

    cmd = prefix + ["rsync", "-avz", "--checksum"]
    if opts.get("dry_run"):
        cmd.append("--dry-run")
    if not opts.get("compress"):
        # swap -avz → -av
        idx = cmd.index("-avz")
        cmd[idx] = "-av"
    cmd += ["-e", ssh_str]

    if mode == "push":
        local = local_root.rstrip("/") + "/"
        remote = f"{server['user']}@{server['host']}:{remote_root}"
        if opts.get("delete"):
            cmd.append("--delete")

        # .gitignore filter
        gitignore_path = Path(local_root) / ".gitignore"
        if opts.get("gitignore") and gitignore_path.exists():
            cmd += ["--filter=:- .gitignore"]

        # always exclude .git
        cmd += ["--exclude=.git/", "--exclude=*.log"]

        # custom excludes
        for ex in repo.get("excludes", []):
            ex = ex.strip()
            if ex:
                cmd += [f"--exclude={ex}"]

        cmd += [local, remote]
        return cmd, extra_env

    if mode == "pull":
        relative_path = normalize_relative_remote_path(opts.get("relative_path", ""))
        remote = f"{server['user']}@{server['host']}:{remote_root}/./{relative_path}"
        local = local_root.rstrip("/") + "/"
        cmd.append("--relative")
        cmd += [remote, local]
        return cmd, extra_env

    raise ValueError(f"Unsupported sync mode: {mode}")

def browse_remote_entries(repo: dict, server: dict, relative_path: str) -> dict:
    current_path = normalize_relative_remote_path(relative_path, allow_root=True)
    _, remote_root = get_repo_roots(repo)
    remote_abs = remote_root if not current_path else posixpath.join(remote_root, current_path)
    remote_cmd = f"""TARGET={shlex.quote(remote_abs)}
if [ ! -d "$TARGET" ]; then
  printf '__CODESYNC_ERROR__\\tnot_directory\\n'
  exit 12
fi
find "$TARGET" -mindepth 1 -maxdepth 1 \\( -type d -o -type f -o -type l \\) -printf '%P\\t%y\\t%s\\n' 2>/dev/null | LC_ALL=C sort
"""
    cmd, extra_env = build_ssh_cmd(server, remote_cmd)
    env = {**os.environ, **extra_env}
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    if proc.returncode != 0:
        error_text = (proc.stderr or proc.stdout or "").strip()
        if "__CODESYNC_ERROR__\tnot_directory" in proc.stdout:
            raise ValueError("当前远程路径不是文件夹")
        if proc.returncode == 127 and "sshpass" in " ".join(cmd):
            raise RuntimeError("sshpass 未安装，无法使用密码模式浏览远程目录")
        raise RuntimeError(error_text or f"远程目录浏览失败，退出码 {proc.returncode}")

    entries = []
    for raw_line in proc.stdout.splitlines():
        if not raw_line:
            continue
        name, kind, size = (raw_line.split("\t", 2) + ["", "", ""])[:3]
        if not name:
            continue
        entry_type = "directory" if kind == "d" else "file"
        entry_path = name if not current_path else f"{current_path}/{name}"
        entries.append({
            "name": name,
            "path": entry_path,
            "type": entry_type,
            "size": int(size) if size.isdigit() else None,
        })

    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
    parent_path = ""
    if current_path:
        parent_path = current_path.rsplit("/", 1)[0] if "/" in current_path else ""
    return {
        "current_path": current_path,
        "parent_path": parent_path,
        "entries": entries,
    }

def run_sync_job(job_id: str, repo: dict, server: dict, opts: dict):
    mode = opts.get("mode", "push")
    if mode == "pull":
        push_log(job_id, f"Starting pull: {server['name']}:{opts.get('relative_path', '')} → {repo['name']}", "start")
    else:
        push_log(job_id, f"Starting sync: {repo['name']} → {server['name']}", "start")

    auth_mode = server.get("auth_mode", "key")
    if auth_mode == "password":
        push_log(job_id, "Auth mode: password  (via sshpass)", "info")

    try:
        cmd, extra_env = build_rsync_cmd(repo, server, opts)
        display_cmd = shlex.join(cmd)
        push_log(job_id, "$ " + display_cmd, "cmd")

        env = {**os.environ, **extra_env}
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                push_log(job_id, line, "output")
        proc.wait()
        if proc.returncode == 0:
            push_log(job_id, "Pull completed successfully." if mode == "pull" else "Sync completed successfully.", "success")
            record_sync_history(repo, server, opts)
        else:
            push_log(job_id, f"rsync exited with code {proc.returncode}", "error")
    except ValueError as e:
        push_log(job_id, f"Error: {e}", "error")
    except FileNotFoundError as e:
        if "sshpass" in str(e):
            push_log(job_id, "Error: sshpass not found. Install it: brew install sshpass  /  apt install sshpass", "error")
        else:
            push_log(job_id, "Error: rsync not found. Please install rsync.", "error")
    except Exception as e:
        push_log(job_id, f"Error: {e}", "error")

    push_log(job_id, "__DONE__", "done")

# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    # Strip sensitive data before sending to browser
    safe_servers = []
    for s in cfg.get("servers", []):
        sc = {k: v for k, v in s.items() if k != "password_enc"}
        sc["has_password"] = bool(s.get("password_enc"))
        safe_servers.append(sc)
    cfg["servers"] = safe_servers
    return jsonify(cfg)

@app.route("/api/servers", methods=["POST"])
def api_add_server():
    cfg = load_config()
    data = request.json
    auth_mode = data.get("auth_mode", "key")
    server = {
        "id": str(int(time.time() * 1000)),
        "name": data["name"],
        "host": data["host"],
        "port": data.get("port", 22),
        "user": data.get("user", "root"),
        "auth_mode": auth_mode,
        "key": data.get("key", "") if auth_mode == "key" else "",
        "password_enc": _obfuscate(data.get("password", "")) if auth_mode == "password" else "",
    }
    cfg["servers"].append(server)
    save_config(cfg)
    # Never return the encoded password to the client
    safe = {k: v for k, v in server.items() if k != "password_enc"}
    safe["has_password"] = bool(server.get("password_enc"))
    return jsonify(safe)

@app.route("/api/servers/<sid>", methods=["DELETE"])
def api_delete_server(sid):
    cfg = load_config()
    cfg["servers"] = [s for s in cfg["servers"] if s["id"] != sid]
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/servers/<sid>", methods=["PUT"])
def api_edit_server(sid):
    cfg = load_config()
    data = request.json
    auth_mode = data.get("auth_mode", "key")
    for s in cfg["servers"]:
        if s["id"] == sid:
            s["name"]      = data["name"]
            s["host"]      = data["host"]
            s["port"]      = data.get("port", 22)
            s["user"]      = data.get("user", "root")
            s["auth_mode"] = auth_mode
            s["key"]       = data.get("key", "") if auth_mode == "key" else ""
            new_pw = data.get("password", "")
            if auth_mode == "password" and new_pw:
                s["password_enc"] = _obfuscate(new_pw)
            elif auth_mode == "key":
                s["password_enc"] = ""
            break
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/repos", methods=["POST"])
def api_add_repo():
    cfg = load_config()
    data = request.json
    repo = {
        "id": str(int(time.time() * 1000)),
        "name": data["name"],
        "local": data["local"],
        "remote": data["remote"],
        "excludes": [e.strip() for e in data.get("excludes", "").split(",") if e.strip()],
    }
    cfg["repos"].append(repo)
    save_config(cfg)
    return jsonify(repo)

@app.route("/api/repos/<rid>", methods=["DELETE"])
def api_delete_repo(rid):
    cfg = load_config()
    cfg["repos"] = [r for r in cfg["repos"] if r["id"] != rid]
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/repos/<rid>", methods=["PUT"])
def api_edit_repo(rid):
    cfg = load_config()
    data = request.json
    for r in cfg["repos"]:
        if r["id"] == rid:
            r["name"]     = data["name"]
            r["local"]    = data["local"]
            r["remote"]   = data["remote"]
            r["excludes"] = [e.strip() for e in data.get("excludes", "").split(",") if e.strip()]
            break
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/sync", methods=["POST"])
def api_sync():
    data = request.json
    cfg = load_config()
    repo = next((r for r in cfg["repos"] if r["id"] == data["repo_id"]), None)
    server = next((s for s in cfg["servers"] if s["id"] == data["server_id"]), None)
    if not repo or not server:
        return jsonify({"error": "repo or server not found"}), 404

    job_id = str(int(time.time() * 1000))
    opts = {
        "mode": "push",
        "delete": data.get("delete", True),
        "dry_run": data.get("dry_run", False),
        "compress": data.get("compress", True),
        "gitignore": data.get("gitignore", True),
    }
    with sync_lock:
        sync_streams[job_id] = []

    t = threading.Thread(target=run_sync_job, args=(job_id, repo, server, opts), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/api/pull", methods=["POST"])
def api_pull():
    data = request.json
    cfg = load_config()
    repo = next((r for r in cfg["repos"] if r["id"] == data["repo_id"]), None)
    server = next((s for s in cfg["servers"] if s["id"] == data["server_id"]), None)
    if not repo or not server:
        return jsonify({"error": "repo or server not found"}), 404

    try:
        relative_path = normalize_relative_remote_path(data.get("relative_path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = str(int(time.time() * 1000))
    opts = {
        "mode": "pull",
        "relative_path": relative_path,
        "dry_run": data.get("dry_run", False),
        "compress": data.get("compress", True),
    }
    with sync_lock:
        sync_streams[job_id] = []

    t = threading.Thread(target=run_sync_job, args=(job_id, repo, server, opts), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/api/remote/browse", methods=["POST"])
def api_remote_browse():
    data = request.json
    cfg = load_config()
    repo = next((r for r in cfg["repos"] if r["id"] == data["repo_id"]), None)
    server = next((s for s in cfg["servers"] if s["id"] == data["server_id"]), None)
    if not repo or not server:
        return jsonify({"error": "repo or server not found"}), 404

    try:
        result = browse_remote_entries(repo, server, data.get("relative_path", ""))
        result["remote_root"] = get_repo_roots(repo)[1]
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        if "sshpass" in str(exc):
            return jsonify({"error": "sshpass 未安装，无法使用密码模式浏览远程目录"}), 500
        if "ssh" in str(exc):
            return jsonify({"error": "ssh 未安装，无法浏览远程目录"}), 500
        return jsonify({"error": str(exc)}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "远程目录浏览超时"}), 504
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/sync/stream/<job_id>")
def api_sync_stream(job_id):
    def generate():
        sent = 0
        while True:
            with sync_lock:
                lines = sync_streams.get(job_id, [])
                new_lines = lines[sent:]
            for line in new_lines:
                yield f"data: {line}\n\n"
                sent += 1
                data = json.loads(line)
                if data.get("level") == "done":
                    return
            time.sleep(0.2)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/generate-script", methods=["POST"])
def api_generate_script():
    data = request.json
    cfg = load_config()
    repo = next((r for r in cfg["repos"] if r["id"] == data["repo_id"]), None)
    server = next((s for s in cfg["servers"] if s["id"] == data["server_id"]), None)
    if not repo or not server:
        return jsonify({"error": "not found"}), 404

    excludes = "\n".join(f'    --exclude="{e}" \\' for e in repo.get("excludes", []) if e)
    script = f'''#!/bin/bash
# CodeSync — Auto-generated sync script
# Repo   : {repo["name"]}
# Server : {server["name"]} ({server["user"]}@{server["host"]})
# Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

set -euo pipefail

REPO_NAME="{repo["name"]}"
LOCAL_PATH="{repo["local"].rstrip("/")}/"
REMOTE_PATH="{server["user"]}@{server["host"]}:{repo["remote"]}"
SSH_KEY="{server.get("key", "~/.ssh/id_rsa")}"
SSH_PORT="{server.get("port", 22)}"

log() {{ echo "[$(date '+%H:%M:%S')] $1"; }}

log "Starting sync: $REPO_NAME"

# Read .gitignore exclusion rules if present
GITIGNORE_OPTS=""
if [ -f "$LOCAL_PATH/.gitignore" ]; then
    GITIGNORE_OPTS="--filter=:- .gitignore"
fi

rsync -avz --checksum --delete \\
    -e "ssh -i $SSH_KEY -p $SSH_PORT -o StrictHostKeyChecking=no" \\
    --exclude=".git/" \\
    --exclude="*.log" \\
{excludes}
    $GITIGNORE_OPTS \\
    "$LOCAL_PATH" \\
    "$REMOTE_PATH"

STATUS=$?
if [ $STATUS -eq 0 ]; then
    log "[✓] Sync successful: $REPO_NAME"
else
    log "[✗] Sync failed (exit $STATUS)" >&2
    exit $STATUS
fi
'''
    return jsonify({"script": script})

# ── HTML frontend ──────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CodeSync</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Syne:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0d0f0e;
  --bg2: #141614;
  --bg3: #1a1d1a;
  --border: rgba(255,255,255,0.07);
  --border2: rgba(255,255,255,0.13);
  --text: #e8ede9;
  --text2: #8a9e8b;
  --text3: #556057;
  --green: #3dd68c;
  --green2: #1a6640;
  --green-dim: rgba(61,214,140,0.12);
  --amber: #f5a623;
  --amber-dim: rgba(245,166,35,0.12);
  --red: #f05555;
  --red-dim: rgba(240,85,85,0.12);
  --blue: #5b9cf6;
  --blue-dim: rgba(91,156,246,0.12);
  --radius: 10px;
  --radius-sm: 6px;
  --mono: 'JetBrains Mono', monospace;
  --sans: 'Syne', sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--sans); }
::-webkit-scrollbar { width: 6px; } ::-webkit-scrollbar-track { background: transparent; } ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }

.layout { display: grid; grid-template-columns: 220px 1fr; height: 100vh; }

/* Sidebar */
.sidebar { border-right: 1px solid var(--border); padding: 24px 0; display: flex; flex-direction: column; background: var(--bg2); }
.brand { padding: 0 20px 24px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
.brand-icon { width: 36px; height: 36px; background: var(--green); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-bottom: 10px; }
.brand-name { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; }
.brand-sub { font-size: 11px; color: var(--text3); font-family: var(--mono); margin-top: 2px; }
.nav { flex: 1; padding: 0 10px; }
.nav-item { display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: var(--radius-sm); cursor: pointer; font-size: 13px; font-weight: 500; color: var(--text2); transition: all 0.15s; margin-bottom: 2px; border: none; background: none; width: 100%; text-align: left; }
.nav-item:hover { background: var(--bg3); color: var(--text); }
.nav-item.active { background: var(--green-dim); color: var(--green); }
.nav-item svg { width: 15px; height: 15px; flex-shrink: 0; }
.sidebar-footer { padding: 16px 20px 0; border-top: 1px solid var(--border); }
.status-dot { width: 7px; height: 7px; background: var(--green); border-radius: 50%; display: inline-block; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.status-text { font-size: 11px; color: var(--text3); font-family: var(--mono); margin-left: 7px; }

/* Main */
.main { overflow-y: auto; padding: 32px 36px; }
.page { display: none; }
.page.active { display: block; }
.page-header { margin-bottom: 28px; }
.page-title { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
.page-sub { font-size: 13px; color: var(--text3); font-family: var(--mono); margin-top: 4px; }

/* Cards */
.card { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px 22px; margin-bottom: 16px; }
.card-title { font-size: 13px; font-weight: 500; color: var(--text2); margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.08em; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }

/* Stat */
.stat { background: var(--bg3); border-radius: var(--radius-sm); padding: 16px; text-align: center; }
.stat-num { font-size: 28px; font-weight: 700; font-family: var(--mono); color: var(--green); }
.stat-label { font-size: 11px; color: var(--text3); margin-top: 3px; }

/* List items */
.item { display: flex; align-items: center; gap: 12px; padding: 11px 0; border-bottom: 1px solid var(--border); }
.item:last-child { border-bottom: none; padding-bottom: 0; }
.item:first-child { padding-top: 0; }
.item-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.item-body { flex: 1; min-width: 0; }
.item-name { font-size: 13px; font-weight: 500; }
.item-sub { font-size: 11px; color: var(--text3); font-family: var(--mono); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.badge { font-size: 10px; padding: 3px 8px; border-radius: 20px; font-family: var(--mono); font-weight: 500; }
.badge-green { background: var(--green-dim); color: var(--green); }
.badge-gray { background: var(--bg3); color: var(--text3); }
.badge-amber { background: var(--amber-dim); color: var(--amber); }
.badge-red { background: var(--red-dim); color: var(--red); }
.badge-blue { background: var(--blue-dim); color: var(--blue); }

/* Buttons */
.btn { padding: 8px 16px; border-radius: var(--radius-sm); font-size: 12px; font-family: var(--sans); font-weight: 500; cursor: pointer; border: 1px solid var(--border2); background: transparent; color: var(--text); transition: all 0.15s; }
.btn:hover { background: var(--bg3); border-color: var(--border2); }
.btn:active { transform: scale(0.97); }
.btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
.btn-green { background: var(--green); color: #000; border-color: var(--green); }
.btn-green:hover { background: #2fbd7a; border-color: #2fbd7a; }
.btn-blue { background: var(--blue); color: #081019; border-color: var(--blue); }
.btn-blue:hover { background: #4d89de; border-color: #4d89de; }
.btn-sm { padding: 5px 11px; font-size: 11px; }
.btn-icon { width: 28px; height: 28px; padding: 0; display: flex; align-items: center; justify-content: center; font-size: 13px; border-radius: var(--radius-sm); }
.btn-icon:hover.danger { background: var(--red-dim); border-color: var(--red); color: var(--red); }
.row-actions { display: flex; gap: 6px; }

/* Forms */
.form-row { display: grid; gap: 12px; margin-bottom: 14px; }
.form-row.cols2 { grid-template-columns: 1fr 1fr; }
.form-row.cols3 { grid-template-columns: 1fr 1fr 1fr; }
.field label { display: block; font-size: 11px; color: var(--text3); margin-bottom: 5px; font-family: var(--mono); }
.field input, .field select, .field textarea {
  width: 100%; padding: 8px 11px; background: var(--bg3); border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text); font-size: 12px; font-family: var(--mono);
  outline: none; transition: border 0.15s;
}
.field input:focus, .field select:focus, .field textarea:focus { border-color: var(--green); }
.field textarea { resize: vertical; min-height: 60px; }
.field select option { background: var(--bg2); }
.field-help { font-size: 10px; color: var(--text3); margin-top: 6px; font-family: var(--mono); line-height: 1.6; }
.checkbox-row { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; }
.checkbox-row input[type=checkbox] { accent-color: var(--green); width: 14px; height: 14px; }
.checkbox-row label { font-size: 12px; color: var(--text2); cursor: pointer; }
.inline-note { font-size: 11px; color: var(--text3); font-family: var(--mono); }

/* Log terminal */
.terminal { background: #080a09; border: 1px solid var(--border); border-radius: var(--radius); padding: 14px 16px; font-family: var(--mono); font-size: 11.5px; line-height: 1.8; min-height: 100px; max-height: 320px; overflow-y: auto; }
.log-start { color: var(--blue); }
.log-cmd { color: var(--text3); }
.log-output { color: var(--text2); }
.log-success { color: var(--green); }
.log-error { color: var(--red); }
.log-info { color: var(--amber); }
.log-done { color: var(--text3); }
.log-placeholder { color: var(--text3); }

/* Progress */
.progress { height: 2px; background: var(--bg3); border-radius: 1px; margin: 10px 0; overflow: hidden; }
.progress-bar { height: 100%; width: 0; background: var(--green); border-radius: 1px; transition: width 0.5s ease; }

/* Script box */
.script-box { background: #080a09; border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; font-family: var(--mono); font-size: 11.5px; line-height: 1.8; color: var(--text2); position: relative; white-space: pre; overflow-x: auto; max-height: 360px; overflow-y: auto; }
.copy-btn { position: absolute; top: 10px; right: 10px; padding: 4px 10px; font-size: 10px; background: var(--bg3); border: 1px solid var(--border2); border-radius: 4px; color: var(--text2); cursor: pointer; font-family: var(--sans); }
.copy-btn:hover { color: var(--text); }

/* Modal */
.modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); z-index: 100; align-items: center; justify-content: center; }
.modal-overlay.open { display: flex; }
.modal { background: var(--bg2); border: 1px solid var(--border2); border-radius: var(--radius); padding: 24px; width: 440px; max-width: 95vw; }
.modal-title { font-size: 15px; font-weight: 700; margin-bottom: 20px; }
.modal-footer { display: flex; gap: 8px; justify-content: flex-end; margin-top: 20px; }

/* History table */
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { font-size: 10px; color: var(--text3); text-transform: uppercase; letter-spacing: 0.08em; padding: 0 0 10px; text-align: left; border-bottom: 1px solid var(--border); }
td { padding: 10px 0; border-bottom: 1px solid var(--border); color: var(--text2); font-family: var(--mono); }
tr:last-child td { border-bottom: none; }

.empty { color: var(--text3); font-size: 12px; font-family: var(--mono); padding: 20px 0; text-align: center; }
.section-actions { display: flex; justify-content: flex-end; margin-bottom: 14px; }
.topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 28px; }
.topbar-right { display: flex; gap: 8px; }
.stack { display: flex; flex-direction: column; gap: 12px; }
.browser-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 12px; }
.browser-toolbar-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.browser-breadcrumb { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin: 10px 0; font-family: var(--mono); font-size: 11px; color: var(--text2); }
.crumb-btn { border: none; background: transparent; color: inherit; cursor: pointer; font: inherit; padding: 0; }
.crumb-btn:hover { color: var(--green); }
.browser-panel { background: #080a09; border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.browser-list { max-height: 320px; overflow-y: auto; padding: 0 16px; }
.browser-row.selected { background: var(--blue-dim); margin: 0 -16px; padding-left: 16px; padding-right: 16px; }
.browser-row .item-sub { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.browser-tag { font-size: 10px; color: var(--text3); }
.text-danger { color: var(--red); }
</style>
</head>
<body>

<div class="layout">
  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-icon">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
          <path d="M4 10h12M10 4v12M6 6l8 8M14 6l-8 8" stroke="#000" stroke-width="1.8" stroke-linecap="round"/>
        </svg>
      </div>
      <div class="brand-name">CodeSync</div>
      <div class="brand-sub">rsync web manager</div>
    </div>
    <nav class="nav">
      <button class="nav-item active" onclick="showPage('overview')">
        <svg viewBox="0 0 15 15" fill="none"><rect x="1" y="1" width="5.5" height="5.5" rx="1" stroke="currentColor" stroke-width="1.3"/><rect x="8.5" y="1" width="5.5" height="5.5" rx="1" stroke="currentColor" stroke-width="1.3"/><rect x="1" y="8.5" width="5.5" height="5.5" rx="1" stroke="currentColor" stroke-width="1.3"/><rect x="8.5" y="8.5" width="5.5" height="5.5" rx="1" stroke="currentColor" stroke-width="1.3"/></svg>
        概览
      </button>
      <button class="nav-item" onclick="showPage('sync')">
        <svg viewBox="0 0 15 15" fill="none"><path d="M13 7.5A5.5 5.5 0 012 7.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M10.5 5L13 7.5l-2.5 2.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 7.5A5.5 5.5 0 0113 7.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M4.5 10L2 7.5 4.5 5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
        同步
      </button>
      <button class="nav-item" onclick="showPage('servers')">
        <svg viewBox="0 0 15 15" fill="none"><rect x="1" y="2" width="13" height="4" rx="1" stroke="currentColor" stroke-width="1.3"/><rect x="1" y="9" width="13" height="4" rx="1" stroke="currentColor" stroke-width="1.3"/><circle cx="3.5" cy="4" r="0.8" fill="currentColor"/><circle cx="3.5" cy="11" r="0.8" fill="currentColor"/></svg>
        服务器
      </button>
      <button class="nav-item" onclick="showPage('repos')">
        <svg viewBox="0 0 15 15" fill="none"><path d="M3 1h9a1 1 0 011 1v11a1 1 0 01-1 1H3a1 1 0 01-1-1V2a1 1 0 011-1z" stroke="currentColor" stroke-width="1.3"/><path d="M5 5h5M5 7.5h5M5 10h3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
        仓库
      </button>
      <button class="nav-item" onclick="showPage('script')">
        <svg viewBox="0 0 15 15" fill="none"><path d="M5 4.5L2 7.5l3 3M10 4.5l3 3-3 3M8 2l-1 11" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
        生成脚本
      </button>
      <button class="nav-item" onclick="showPage('history')">
        <svg viewBox="0 0 15 15" fill="none"><circle cx="7.5" cy="7.5" r="6" stroke="currentColor" stroke-width="1.3"/><path d="M7.5 4v3.5l2.5 2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
        历史记录
      </button>
    </nav>
    <div class="sidebar-footer">
      <span class="status-dot"></span>
      <span class="status-text">localhost:7788</span>
    </div>
  </aside>

  <!-- Main content -->
  <main class="main">

    <!-- Overview -->
    <div class="page active" id="page-overview">
      <div class="page-header">
        <div class="page-title">概览</div>
        <div class="page-sub">codesync / dashboard</div>
      </div>
      <div class="grid3" style="margin-bottom:16px;">
        <div class="stat"><div class="stat-num" id="ov-servers">0</div><div class="stat-label">服务器</div></div>
        <div class="stat"><div class="stat-num" id="ov-repos">0</div><div class="stat-label">仓库</div></div>
        <div class="stat"><div class="stat-num" id="ov-syncs">0</div><div class="stat-label">同步记录</div></div>
      </div>
      <div class="grid2">
        <div class="card">
          <div class="card-title">服务器</div>
          <div id="ov-server-list"><div class="empty">暂无服务器</div></div>
        </div>
        <div class="card">
          <div class="card-title">仓库</div>
          <div id="ov-repo-list"><div class="empty">暂无仓库</div></div>
        </div>
      </div>
    </div>

    <!-- Sync -->
    <div class="page" id="page-sync">
      <div class="page-header">
        <div class="page-title">同步</div>
        <div class="page-sub">codesync / sync</div>
      </div>
      <div class="card">
        <div class="card-title">选择目标</div>
        <div class="form-row cols2">
          <div class="field"><label>仓库</label><select id="sync-repo" onchange="handleSyncTargetChange(true)"></select></div>
          <div class="field"><label>服务器</label><select id="sync-server" onchange="handleSyncTargetChange(true)"></select></div>
        </div>
        <div class="card-title" style="margin-top:4px;">推送选项</div>
        <div class="checkbox-row"><input type="checkbox" id="opt-delete" checked><label for="opt-delete">删除远端多余文件 (--delete)</label></div>
        <div class="checkbox-row"><input type="checkbox" id="opt-dry"><label for="opt-dry">预演模式 (--dry-run)，不实际传输</label></div>
        <div class="checkbox-row"><input type="checkbox" id="opt-compress" checked><label for="opt-compress">启用传输压缩 (-z)</label></div>
        <div class="checkbox-row"><input type="checkbox" id="opt-gitignore" checked><label for="opt-gitignore">自动读取 .gitignore 排除规则</label></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;">
          <button class="btn btn-green" onclick="startSync()">▶ 开始同步</button>
          <button class="btn" onclick="startSyncAll()">同步所有仓库</button>
          <button class="btn" id="stop-btn" style="display:none;color:var(--red);border-color:var(--red);" onclick="stopSync()">■ 停止</button>
          <button class="btn btn-sm" style="margin-left:auto;" onclick="clearLog()">清除日志</button>
        </div>
        <div class="card-title" style="margin-top:20px;">远程定向回传</div>
        <div class="stack">
          <div class="field">
            <label>已选远程相对路径</label>
            <input id="pull-relative-path" placeholder="请从下方浏览器中选择文件或文件夹" type="text">
            <div class="field-help">回传目标会自动映射为：远端 <仓库远程根目录>/<相对路径> → 本地 <仓库本地根目录>/<相对路径>。浏览器只允许在仓库远端根目录内导航。</div>
          </div>
          <div>
            <div class="browser-toolbar">
              <div>
                <div class="card-title" style="margin-bottom:6px;">远程浏览器</div>
                <div class="inline-note">浏览根目录：<span id="remote-browser-root">—</span></div>
              </div>
              <div class="browser-toolbar-actions">
                <button class="btn btn-sm" type="button" onclick="loadRemoteBrowser(remoteBrowser.path, true)">刷新</button>
                <button class="btn btn-sm" type="button" id="remote-up-btn" onclick="browseRemoteUp()">返回上级</button>
                <button class="btn btn-sm" type="button" id="remote-select-current-btn" onclick="selectCurrentRemoteDir()">选择当前文件夹</button>
              </div>
            </div>
            <div class="browser-breadcrumb" id="remote-browser-path"></div>
            <div class="browser-panel">
              <div class="browser-list" id="remote-browser-list"></div>
            </div>
          </div>
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px;">
          <button class="btn btn-blue" onclick="startPull()">↓ 回传选中文件/目录</button>
          <span class="inline-note">先在浏览器中进入目录并选择文件或文件夹；回传复用当前仓库与服务器，只使用 dry-run 与压缩选项。</span>
        </div>
        <div class="progress"><div class="progress-bar" id="progress-bar"></div></div>
      </div>
      <div class="card" style="padding:0;overflow:hidden;">
        <div style="padding:14px 22px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;">
          <div style="width:10px;height:10px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;" id="log-indicator" style="display:none;"></div>
          <span style="font-size:12px;color:var(--text3);font-family:var(--mono);">sync output</span>
        </div>
        <div class="terminal" id="sync-log"><span class="log-placeholder">— waiting for sync job —</span></div>
      </div>
    </div>

    <!-- Servers -->
    <div class="page" id="page-servers">
      <div class="topbar">
        <div>
          <div class="page-title">服务器</div>
          <div class="page-sub">codesync / servers</div>
        </div>
        <button class="btn btn-green" onclick="openModal('modal-add-server')">+ 添加服务器</button>
      </div>
      <div class="card" id="server-card">
        <div class="empty">暂无服务器 — 点击「添加服务器」开始</div>
      </div>
    </div>

    <!-- Repos -->
    <div class="page" id="page-repos">
      <div class="topbar">
        <div>
          <div class="page-title">仓库</div>
          <div class="page-sub">codesync / repositories</div>
        </div>
        <button class="btn btn-green" onclick="openModal('modal-add-repo')">+ 添加仓库</button>
      </div>
      <div class="card" id="repo-card">
        <div class="empty">暂无仓库 — 点击「添加仓库」开始</div>
      </div>
    </div>

    <!-- Script -->
    <div class="page" id="page-script">
      <div class="page-header">
        <div class="page-title">生成脚本</div>
        <div class="page-sub">codesync / generate shell script</div>
      </div>
      <div class="card">
        <div class="form-row cols2">
          <div class="field"><label>仓库</label><select id="script-repo" onchange="loadScript()"></select></div>
          <div class="field"><label>服务器</label><select id="script-server" onchange="loadScript()"></select></div>
        </div>
        <button class="btn btn-sm" onclick="loadScript()">生成脚本</button>
      </div>
      <div class="card" style="padding:0;overflow:hidden;">
        <div style="padding:14px 22px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;">
          <span style="font-size:11px;color:var(--text3);font-family:var(--mono);">sync.sh</span>
          <button class="btn btn-sm" onclick="downloadScript()">↓ 下载脚本</button>
        </div>
        <div class="script-box" id="script-content">
          <button class="copy-btn" onclick="copyScript()">复制</button>
          <span style="color:var(--text3);">— 选择仓库和服务器后点击「生成脚本」—</span>
        </div>
      </div>
    </div>

    <!-- History -->
    <div class="page" id="page-history">
      <div class="topbar">
        <div>
          <div class="page-title">历史记录</div>
          <div class="page-sub">codesync / sync history</div>
        </div>
        <button class="btn btn-sm" onclick="loadHistory()">刷新</button>
      </div>
      <div class="card">
        <div id="history-table"><div class="empty">暂无同步记录</div></div>
      </div>
    </div>

  </main>
</div>

<!-- Add Server Modal -->
<div class="modal-overlay" id="modal-add-server">
  <div class="modal">
    <div class="modal-title">添加服务器</div>
    <div class="form-row"><div class="field"><label>名称</label><input id="ns-name" placeholder="生产服务器" type="text"></div></div>
    <div class="form-row"><div class="field"><label>IP / 域名</label><input id="ns-host" placeholder="192.168.1.100 或 example.com" type="text"></div></div>
    <div class="form-row cols2">
      <div class="field"><label>端口</label><input id="ns-port" placeholder="22" type="text"></div>
      <div class="field"><label>用户名</label><input id="ns-user" placeholder="ubuntu" type="text"></div>
    </div>
    <div class="form-row">
      <div class="field">
        <label>认证方式</label>
        <select id="ns-auth" onchange="toggleAuthMode()">
          <option value="key">SSH 密钥</option>
          <option value="password">密码</option>
        </select>
      </div>
    </div>
    <div id="ns-key-group" class="form-row"><div class="field"><label>SSH 密钥路径（留空使用默认）</label><input id="ns-key" placeholder="~/.ssh/id_rsa" type="text"></div></div>
    <div id="ns-pass-group" class="form-row" style="display:none;"><div class="field">
      <label>SSH 密码</label>
      <div style="position:relative;">
        <input id="ns-password" placeholder="输入 SSH 密码" type="password" style="padding-right:40px;">
        <button type="button" onclick="togglePassVis()" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text3);cursor:pointer;font-size:12px;font-family:var(--sans);" id="pass-eye">显示</button>
      </div>
      <div style="font-size:10px;color:var(--text3);margin-top:5px;">⚠ 需要本机安装 sshpass。密码经 Base64 混淆存储，建议在局域网内使用。</div>
    </div></div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('modal-add-server')">取消</button>
      <button class="btn btn-green" onclick="addServer()">添加</button>
    </div>
  </div>
</div>

<!-- Add Repo Modal -->
<div class="modal-overlay" id="modal-add-repo">
  <div class="modal">
    <div class="modal-title">添加仓库</div>
    <div class="form-row"><div class="field"><label>仓库名称</label><input id="nr-name" placeholder="my-project" type="text"></div></div>
    <div class="form-row"><div class="field"><label>本地路径</label><input id="nr-local" placeholder="/Users/me/projects/my-project" type="text"></div></div>
    <div class="form-row"><div class="field"><label>远程路径</label><input id="nr-remote" placeholder="/opt/my-project" type="text"></div></div>
    <div class="form-row"><div class="field"><label>额外排除规则（逗号分隔，支持 glob）</label><input id="nr-excludes" placeholder="*.log, .env, node_modules/, vendor/" type="text"></div></div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('modal-add-repo')">取消</button>
      <button class="btn btn-green" onclick="addRepo()">添加</button>
    </div>
  </div>
</div>

<script>
let cfg = { servers: [], repos: [], sync_history: [] };
let currentJobId = null;
let syncESS = null;
let remoteBrowser = {
  repoId: '',
  serverId: '',
  rootRemote: '',
  path: '',
  parentPath: '',
  entries: [],
  loading: false,
  error: '',
  selectedPath: '',
  selectedType: '',
};

async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  });
  return r.json();
}

async function loadAll() {
  cfg = await api('GET', '/api/config');
  renderAll();
}

function renderAll() {
  renderOverview();
  renderServerPage();
  renderRepoPage();
  populateSelects();
  handleSyncTargetChange(false);
}

// Navigation
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (typeof event !== 'undefined' && event && event.currentTarget) {
    event.currentTarget.classList.add('active');
  }
  if (name === 'history') loadHistory();
  if (name === 'sync') ensureRemoteBrowserLoaded();
}

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

// Overview
function renderOverview() {
  document.getElementById('ov-servers').textContent = cfg.servers.length;
  document.getElementById('ov-repos').textContent = cfg.repos.length;
  document.getElementById('ov-syncs').textContent = (cfg.sync_history || []).length;

  const sl = document.getElementById('ov-server-list');
  sl.innerHTML = cfg.servers.length ? cfg.servers.map(s => `
    <div class="item">
      <div class="item-dot" style="background:var(--green)"></div>
      <div class="item-body">
        <div class="item-name">${esc(s.name)}</div>
        <div class="item-sub">${esc(s.user)}@${esc(s.host)}:${s.port}</div>
      </div>
      <span class="badge badge-${s.auth_mode === 'password' ? 'amber' : 'blue'}">${s.auth_mode === 'password' ? '密码' : '密钥'}</span>
    </div>`).join('') : '<div class="empty">暂无服务器</div>';

  const rl = document.getElementById('ov-repo-list');
  rl.innerHTML = cfg.repos.length ? cfg.repos.map(r => `
    <div class="item">
      <div class="item-dot" style="background:var(--blue)"></div>
      <div class="item-body">
        <div class="item-name">${esc(r.name)}</div>
        <div class="item-sub">${esc(r.local)}</div>
      </div>
      <button class="btn btn-sm" onclick="quickSync('${r.id}')">同步</button>
    </div>`).join('') : '<div class="empty">暂无仓库</div>';
}

function renderServerPage() {
  const el = document.getElementById('server-card');
  el.innerHTML = cfg.servers.length ? cfg.servers.map(s => {
    const authBadge = s.auth_mode === 'password'
      ? `<span class="badge badge-amber">密码${s.has_password ? ' ✓' : ' !'}</span>`
      : `<span class="badge badge-blue">密钥${s.key ? ' ✓' : ''}</span>`;
    return `
    <div class="item">
      <div class="item-dot" style="background:var(--green);animation:pulse 2s infinite;"></div>
      <div class="item-body">
        <div class="item-name">${esc(s.name)}</div>
        <div class="item-sub">${esc(s.user)}@${esc(s.host)}:${s.port}${s.auth_mode === 'key' && s.key ? '  ·  key: ' + esc(s.key) : ''}</div>
      </div>
      <div class="row-actions">
        ${authBadge}
        <button class="btn btn-sm" title="编辑" onclick="openEditServer('${s.id}')">编辑</button>
        <button class="btn btn-icon danger" title="删除" onclick="deleteServer('${s.id}')">✕</button>
      </div>
    </div>`;
  }).join('') : '<div class="empty">暂无服务器</div>';
}

function renderRepoPage() {
  const el = document.getElementById('repo-card');
  el.innerHTML = cfg.repos.length ? cfg.repos.map(r => `
    <div class="item">
      <div class="item-dot" style="background:var(--blue)"></div>
      <div class="item-body">
        <div class="item-name">${esc(r.name)}</div>
        <div class="item-sub">${esc(r.local)} → ${esc(r.remote)}</div>
      </div>
      <div class="row-actions">
        <span class="badge badge-gray" style="font-size:9px;">${(r.excludes||[]).length} 排除规则</span>
        <button class="btn btn-sm" title="编辑" onclick="openEditRepo('${r.id}')">编辑</button>
        <button class="btn btn-icon danger" title="删除" onclick="deleteRepo('${r.id}')">✕</button>
      </div>
    </div>`).join('') : '<div class="empty">暂无仓库</div>';
}

function populateSelects() {
  const currentSyncRepo = document.getElementById('sync-repo')?.value || '';
  const currentSyncServer = document.getElementById('sync-server')?.value || '';
  const currentScriptRepo = document.getElementById('script-repo')?.value || '';
  const currentScriptServer = document.getElementById('script-server')?.value || '';

  ['sync-repo','script-repo'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = cfg.repos.length
      ? cfg.repos.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join('')
      : '<option>— 暂无仓库 —</option>';
  });
  ['sync-server','script-server'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = cfg.servers.length
      ? cfg.servers.map(s => `<option value="${s.id}">${esc(s.name)} (${esc(s.host)})</option>`).join('')
      : '<option>— 暂无服务器 —</option>';
  });

  restoreSelectValue('sync-repo', currentSyncRepo);
  restoreSelectValue('sync-server', currentSyncServer);
  restoreSelectValue('script-repo', currentScriptRepo);
  restoreSelectValue('script-server', currentScriptServer);
}

function restoreSelectValue(id, value) {
  const el = document.getElementById(id);
  if (!el || !value) return;
  const hasOption = Array.from(el.options).some(option => option.value === value);
  if (hasOption) el.value = value;
}

function getSyncSelection() {
  const repoId = document.getElementById('sync-repo')?.value || '';
  const serverId = document.getElementById('sync-server')?.value || '';
  return {
    repoId,
    serverId,
    repo: cfg.repos.find(r => r.id === repoId) || null,
    server: cfg.servers.find(s => s.id === serverId) || null,
  };
}

function resetRemoteBrowserState(repoId = '', serverId = '', rootRemote = '') {
  remoteBrowser = {
    repoId,
    serverId,
    rootRemote,
    path: '',
    parentPath: '',
    entries: [],
    loading: false,
    error: '',
    selectedPath: '',
    selectedType: '',
  };
  const input = document.getElementById('pull-relative-path');
  if (input) input.value = '';
}

function handleSyncTargetChange(forceReload = false) {
  const { repoId, serverId, repo, server } = getSyncSelection();
  const targetChanged = remoteBrowser.repoId !== repoId || remoteBrowser.serverId !== serverId;
  if (!repo || !server) {
    resetRemoteBrowserState(repoId, serverId, repo?.remote || '');
    renderRemoteBrowser();
    return;
  }
  if (targetChanged) {
    resetRemoteBrowserState(repoId, serverId, repo.remote);
  } else {
    remoteBrowser.rootRemote = repo.remote;
  }
  renderRemoteBrowser();
  if (forceReload || targetChanged) {
    loadRemoteBrowser('');
  }
}

async function ensureRemoteBrowserLoaded(forceReload = false) {
  const { repoId, serverId, repo, server } = getSyncSelection();
  if (!repo || !server) {
    resetRemoteBrowserState(repoId, serverId, repo?.remote || '');
    renderRemoteBrowser();
    return;
  }
  const targetChanged = remoteBrowser.repoId !== repoId || remoteBrowser.serverId !== serverId;
  if (targetChanged) {
    resetRemoteBrowserState(repoId, serverId, repo.remote);
  } else {
    remoteBrowser.rootRemote = repo.remote;
  }
  if (forceReload || targetChanged || (!remoteBrowser.loading && !remoteBrowser.error && !remoteBrowser.entries.length)) {
    await loadRemoteBrowser(remoteBrowser.path || '');
    return;
  }
  renderRemoteBrowser();
}

async function loadRemoteBrowser(relativePath = '', forceReload = false) {
  const { repoId, serverId, repo, server } = getSyncSelection();
  if (!repo || !server) {
    renderRemoteBrowser();
    return;
  }

  remoteBrowser.repoId = repoId;
  remoteBrowser.serverId = serverId;
  remoteBrowser.rootRemote = repo.remote;
  remoteBrowser.loading = true;
  remoteBrowser.error = '';
  remoteBrowser.path = relativePath || '';
  if (forceReload) {
    remoteBrowser.entries = [];
  }
  renderRemoteBrowser();

  const res = await api('POST', '/api/remote/browse', {
    repo_id: repoId,
    server_id: serverId,
    relative_path: relativePath || '',
  });

  remoteBrowser.loading = false;
  if (res.error) {
    remoteBrowser.error = res.error;
    remoteBrowser.entries = [];
    renderRemoteBrowser();
    return;
  }

  remoteBrowser.rootRemote = res.remote_root || repo.remote;
  remoteBrowser.path = res.current_path || '';
  remoteBrowser.parentPath = res.parent_path || '';
  remoteBrowser.entries = res.entries || [];
  remoteBrowser.error = '';
  renderRemoteBrowser();
}

function renderRemoteBrowser() {
  const rootEl = document.getElementById('remote-browser-root');
  const pathEl = document.getElementById('remote-browser-path');
  const listEl = document.getElementById('remote-browser-list');
  const upBtn = document.getElementById('remote-up-btn');
  const selectCurrentBtn = document.getElementById('remote-select-current-btn');
  if (!rootEl || !pathEl || !listEl || !upBtn || !selectCurrentBtn) return;

  rootEl.textContent = remoteBrowser.rootRemote || '—';
  upBtn.disabled = !remoteBrowser.path || remoteBrowser.loading;
  selectCurrentBtn.disabled = !remoteBrowser.path || remoteBrowser.loading;

  const { repo, server } = getSyncSelection();
  if (!repo || !server) {
    pathEl.innerHTML = '<span class="inline-note">先选择仓库和服务器，再浏览远程目录。</span>';
    listEl.innerHTML = '<div class="empty">暂无可浏览的远程目录</div>';
    return;
  }

  const crumbs = [];
  crumbs.push(`<button class="crumb-btn" type="button" data-remote-action="navigate" data-path="">${esc(remoteBrowser.rootRemote || '/')}</button>`);
  let acc = '';
  for (const segment of (remoteBrowser.path ? remoteBrowser.path.split('/') : [])) {
    acc = acc ? `${acc}/${segment}` : segment;
    crumbs.push('<span>/</span>');
    crumbs.push(`<button class="crumb-btn" type="button" data-remote-action="navigate" data-path="${escAttr(acc)}">${esc(segment)}</button>`);
  }
  if (remoteBrowser.selectedPath) {
    crumbs.push(`<span class="browser-tag">已选：${esc(remoteBrowser.selectedPath)}</span>`);
  }
  pathEl.innerHTML = crumbs.join('');

  if (remoteBrowser.loading) {
    listEl.innerHTML = '<div class="empty">远程目录加载中...</div>';
    return;
  }
  if (remoteBrowser.error) {
    listEl.innerHTML = `<div class="empty text-danger">${esc(remoteBrowser.error)}</div>`;
    return;
  }
  if (!remoteBrowser.entries.length) {
    listEl.innerHTML = '<div class="empty">当前目录为空</div>';
    return;
  }

  listEl.innerHTML = remoteBrowser.entries.map(entry => {
    const selected = entry.path === remoteBrowser.selectedPath ? ' selected' : '';
    const kind = entry.type === 'directory' ? '目录' : '文件';
    const meta = entry.type === 'directory'
      ? '<span class="browser-tag">可进入</span>'
      : `<span class="browser-tag">${formatBytes(entry.size)}</span>`;
    const actions = entry.type === 'directory'
      ? `<button class="btn btn-sm" type="button" data-remote-action="navigate" data-path="${escAttr(entry.path)}">进入</button>
         <button class="btn btn-sm" type="button" data-remote-action="select" data-path="${escAttr(entry.path)}" data-entry-type="directory">选择文件夹</button>`
      : `<button class="btn btn-sm" type="button" data-remote-action="select" data-path="${escAttr(entry.path)}" data-entry-type="file">选择文件</button>`;
    return `
      <div class="item browser-row${selected}">
        <div class="item-dot" style="background:${entry.type === 'directory' ? 'var(--green)' : 'var(--blue)'}"></div>
        <div class="item-body">
          <div class="item-name">${esc(entry.name)}</div>
          <div class="item-sub"><span>${esc(entry.path)}</span>${meta}</div>
        </div>
        <span class="badge badge-${entry.type === 'directory' ? 'green' : 'blue'}">${kind}</span>
        <div class="row-actions">${actions}</div>
      </div>`;
  }).join('');
}

function browseRemoteUp() {
  if (!remoteBrowser.path || remoteBrowser.loading) return;
  loadRemoteBrowser(remoteBrowser.parentPath || '');
}

function selectCurrentRemoteDir() {
  if (!remoteBrowser.path) {
    alert('仓库远端根目录不能直接作为回传目标，请继续进入具体文件夹后再选择。');
    return;
  }
  setSelectedRemotePath(remoteBrowser.path, 'directory');
}

function setSelectedRemotePath(path, entryType) {
  remoteBrowser.selectedPath = path || '';
  remoteBrowser.selectedType = entryType || '';
  const input = document.getElementById('pull-relative-path');
  if (input) input.value = remoteBrowser.selectedPath;
  renderRemoteBrowser();
}

function toggleAuthMode() {
  const mode = document.getElementById('ns-auth').value;
  document.getElementById('ns-key-group').style.display  = mode === 'key'      ? '' : 'none';
  document.getElementById('ns-pass-group').style.display = mode === 'password' ? '' : 'none';
}

function togglePassVis() {
  const inp = document.getElementById('ns-password');
  const btn = document.getElementById('pass-eye');
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = '隐藏'; }
  else { inp.type = 'password'; btn.textContent = '显示'; }
}

// Servers / Repos CRUD
async function addServer() {
  const name = document.getElementById('ns-name').value.trim();
  const host = document.getElementById('ns-host').value.trim();
  if (!name || !host) return alert('名称和主机地址不能为空');
  const auth_mode = document.getElementById('ns-auth').value;
  const payload = {
    name, host,
    port: parseInt(document.getElementById('ns-port').value) || 22,
    user: document.getElementById('ns-user').value.trim() || 'root',
    auth_mode,
    key: auth_mode === 'key' ? document.getElementById('ns-key').value.trim() : '',
    password: auth_mode === 'password' ? document.getElementById('ns-password').value : '',
  };
  await api('POST', '/api/servers', payload);
  closeModal('modal-add-server');
  ['ns-name','ns-host','ns-port','ns-user','ns-key','ns-password'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('ns-auth').value = 'key';
  toggleAuthMode();
  await loadAll();
}

async function addRepo() {
  const name = document.getElementById('nr-name').value.trim();
  const local = document.getElementById('nr-local').value.trim();
  const remote = document.getElementById('nr-remote').value.trim();
  if (!name || !local || !remote) return alert('名称、本地路径、远程路径不能为空');
  await api('POST', '/api/repos', {
    name, local, remote,
    excludes: document.getElementById('nr-excludes').value,
  });
  closeModal('modal-add-repo');
  ['nr-name','nr-local','nr-remote','nr-excludes'].forEach(id => document.getElementById(id).value = '');
  await loadAll();
}

async function deleteServer(id) {
  if (!confirm('确认删除该服务器？')) return;
  await api('DELETE', `/api/servers/${id}`);
  await loadAll();
}

async function deleteRepo(id) {
  if (!confirm('确认删除该仓库？')) return;
  await api('DELETE', `/api/repos/${id}`);
  await loadAll();
}

// Sync
function clearLog() {
  document.getElementById('sync-log').innerHTML = '<span class="log-placeholder">— waiting for sync job —</span>';
}

function addLogLine(ts, msg, level) {
  const log = document.getElementById('sync-log');
  const ph = log.querySelector('.log-placeholder');
  if (ph) ph.remove();
  const line = document.createElement('div');
  line.className = `log-${level}`;
  line.textContent = `[${ts}] ${msg}`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function setProgress(pct) {
  document.getElementById('progress-bar').style.width = pct + '%';
}

function prepareSyncUi() {
  clearLog();
  setProgress(5);
  document.getElementById('stop-btn').style.display = '';
}

function resetSyncUi() {
  document.getElementById('stop-btn').style.display = 'none';
  setProgress(0);
}

async function startJob(path, payload) {
  prepareSyncUi();
  const res = await api('POST', path, payload);
  if (res.error) {
    addLogLine('--:--:--', res.error, 'error');
    resetSyncUi();
    return null;
  }
  currentJobId = res.job_id;
  listenJob(res.job_id);
  return res.job_id;
}

async function startSync() {
  const repoId = document.getElementById('sync-repo').value;
  const serverId = document.getElementById('sync-server').value;
  if (!repoId || !serverId) return alert('请先添加服务器和仓库');
  await startJob('/api/sync', {
    repo_id: repoId,
    server_id: serverId,
    delete: document.getElementById('opt-delete').checked,
    dry_run: document.getElementById('opt-dry').checked,
    compress: document.getElementById('opt-compress').checked,
    gitignore: document.getElementById('opt-gitignore').checked,
  });
}

async function startPull() {
  const repoId = document.getElementById('sync-repo').value;
  const serverId = document.getElementById('sync-server').value;
  const relativePath = document.getElementById('pull-relative-path').value.trim();
  if (!repoId || !serverId) return alert('请先添加服务器和仓库');
  if (!relativePath) return alert('请先在远程浏览器中选择需要回传的文件或文件夹');

  await startJob('/api/pull', {
    repo_id: repoId,
    server_id: serverId,
    relative_path: relativePath,
    dry_run: document.getElementById('opt-dry').checked,
    compress: document.getElementById('opt-compress').checked,
  });
}

function listenJob(jobId) {
  if (syncESS) syncESS.close();
  syncESS = new EventSource(`/api/sync/stream/${jobId}`);
  let pct = 5;
  syncESS.onmessage = e => {
    const d = JSON.parse(e.data);
    addLogLine(d.ts, d.msg, d.level);
    if (d.level === 'output') { pct = Math.min(90, pct + 2); setProgress(pct); }
    if (d.level === 'success') { setProgress(100); cfg.sync_history.unshift({}); document.getElementById('ov-syncs').textContent = cfg.sync_history.length; }
    if (d.level === 'done') {
      syncESS.close(); syncESS = null;
      currentJobId = null;
      document.getElementById('stop-btn').style.display = 'none';
      setTimeout(() => setProgress(0), 2000);
    }
  };
}

function stopSync() {
  if (syncESS) { syncESS.close(); syncESS = null; }
  currentJobId = null;
  addLogLine(new Date().toLocaleTimeString(), 'Stopped by user.', 'error');
  document.getElementById('stop-btn').style.display = 'none';
  setProgress(0);
}

async function startSyncAll() {
  clearLog();
  for (const repo of cfg.repos) {
    const sid = document.getElementById('sync-server').value;
    if (!sid) { alert('请先选择目标服务器'); return; }
    addLogLine(new Date().toLocaleTimeString(), `Queueing: ${repo.name}`, 'info');
    const res = await api('POST', '/api/sync', {
      repo_id: repo.id, server_id: sid,
      delete: document.getElementById('opt-delete').checked,
      dry_run: document.getElementById('opt-dry').checked,
      compress: document.getElementById('opt-compress').checked,
      gitignore: document.getElementById('opt-gitignore').checked,
    });
    if (!res.job_id) continue;
    await new Promise(resolve => {
      const es = new EventSource(`/api/sync/stream/${res.job_id}`);
      es.onmessage = e => {
        const d = JSON.parse(e.data);
        addLogLine(d.ts, d.msg, d.level);
        if (d.level === 'done') { es.close(); resolve(); }
      };
    });
  }
}

function quickSync(repoId) {
  showPage('sync');
  document.querySelector('.nav-item:nth-child(2)').classList.add('active');
  document.querySelector('.nav-item:first-child').classList.remove('active');
  setTimeout(() => {
    document.getElementById('sync-repo').value = repoId;
    handleSyncTargetChange(true);
    startSync();
  }, 100);
}

// Script generation
let lastScript = '';
async function loadScript() {
  const rid = document.getElementById('script-repo').value;
  const sid = document.getElementById('script-server').value;
  if (!rid || !sid) return;
  const res = await api('POST', '/api/generate-script', { repo_id: rid, server_id: sid });
  if (res.error) return;
  lastScript = res.script;
  document.getElementById('script-content').innerHTML =
    '<button class="copy-btn" onclick="copyScript()">复制</button>' +
    esc(res.script).replace(/^(#.*)/gm, '<span style="color:var(--text3)">$1</span>')
      .replace(/\b(rsync|ssh|set|if|fi|then|echo)\b/g, '<span style="color:var(--blue)">$1</span>')
      .replace(/"([^"]*)"/g, '<span style="color:var(--green)">\"$1\"</span>');
}

function copyScript() {
  navigator.clipboard.writeText(lastScript);
  const btn = document.querySelector('#script-content .copy-btn');
  if (btn) { btn.textContent = '已复制 ✓'; setTimeout(() => btn.textContent = '复制', 1500); }
}

function downloadScript() {
  if (!lastScript) return;
  const a = document.createElement('a');
  a.href = 'data:text/plain;charset=utf-8,' + encodeURIComponent(lastScript);
  a.download = 'sync.sh';
  a.click();
}

// History
async function loadHistory() {
  cfg = await api('GET', '/api/config');
  const h = cfg.sync_history || [];
  const el = document.getElementById('history-table');
  if (!h.length) { el.innerHTML = '<div class="empty">暂无同步记录</div>'; return; }
  const rows = h.map(r => {
    const mode = r.mode === 'pull' ? 'pull' : 'push';
    const path = mode === 'pull' ? (r.path || '—') : '—';
    return `<tr>
      <td>${esc(r.time)}</td>
      <td><span class="badge badge-${mode === 'pull' ? 'blue' : 'gray'}">${mode}</span></td>
      <td>${esc(r.repo)}</td>
      <td>${esc(r.server)}</td>
      <td>${esc(path)}</td>
      <td><span class="badge badge-${r.status === 'success' ? 'green' : 'red'}">${r.status}</span></td>
      <td><span class="badge badge-${r.dry_run ? 'amber' : 'gray'}">${r.dry_run ? 'dry-run' : 'live'}</span></td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table>
    <thead><tr><th>时间</th><th>类型</th><th>仓库</th><th>服务器</th><th>路径</th><th>状态</th><th>模式</th></tr></thead>
    <tbody>
    ${rows}
    </tbody>
  </table>`;
}

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function escAttr(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function formatBytes(size) {
  if (size === null || size === undefined || Number.isNaN(Number(size))) return '未知大小';
  const value = Number(size);
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let next = value / 1024;
  let idx = 0;
  while (next >= 1024 && idx < units.length - 1) {
    next /= 1024;
    idx += 1;
  }
  return `${next.toFixed(next >= 10 ? 0 : 1)} ${units[idx]}`;
}

document.addEventListener('click', event => {
  const actionEl = event.target.closest('[data-remote-action]');
  if (!actionEl) return;
  const action = actionEl.dataset.remoteAction;
  const path = actionEl.dataset.path || '';
  if (action === 'navigate') {
    loadRemoteBrowser(path);
    return;
  }
  if (action === 'select') {
    setSelectedRemotePath(path, actionEl.dataset.entryType || '');
  }
});


// ── Edit helpers ──────────────────────────────────────────────────────────────
function toggleEditAuthMode() {
  const mode = document.getElementById('es-auth').value;
  document.getElementById('es-key-group').style.display  = mode === 'key'      ? '' : 'none';
  document.getElementById('es-pass-group').style.display = mode === 'password' ? '' : 'none';
}

function toggleEditPassVis() {
  const inp = document.getElementById('es-password');
  const btn = document.getElementById('es-pass-eye');
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = '隐藏'; }
  else { inp.type = 'password'; btn.textContent = '显示'; }
}

function openEditServer(id) {
  const s = cfg.servers.find(x => x.id === id);
  if (!s) return;
  document.getElementById('es-id').value   = s.id;
  document.getElementById('es-name').value = s.name;
  document.getElementById('es-host').value = s.host;
  document.getElementById('es-port').value = s.port;
  document.getElementById('es-user').value = s.user;
  document.getElementById('es-auth').value = s.auth_mode || 'key';
  document.getElementById('es-key').value  = s.key || '';
  document.getElementById('es-password').value = '';
  toggleEditAuthMode();
  openModal('modal-edit-server');
}

async function saveServer() {
  const id   = document.getElementById('es-id').value;
  const name = document.getElementById('es-name').value.trim();
  const host = document.getElementById('es-host').value.trim();
  if (!name || !host) return alert('名称和主机地址不能为空');
  const auth_mode = document.getElementById('es-auth').value;
  await api('PUT', `/api/servers/${id}`, {
    name, host,
    port: parseInt(document.getElementById('es-port').value) || 22,
    user: document.getElementById('es-user').value.trim() || 'root',
    auth_mode,
    key:      auth_mode === 'key'      ? document.getElementById('es-key').value.trim() : '',
    password: auth_mode === 'password' ? document.getElementById('es-password').value   : '',
  });
  closeModal('modal-edit-server');
  await loadAll();
}

function openEditRepo(id) {
  const r = cfg.repos.find(x => x.id === id);
  if (!r) return;
  document.getElementById('er-id').value       = r.id;
  document.getElementById('er-name').value     = r.name;
  document.getElementById('er-local').value    = r.local;
  document.getElementById('er-remote').value   = r.remote;
  document.getElementById('er-excludes').value = (r.excludes || []).join(', ');
  openModal('modal-edit-repo');
}

async function saveRepo() {
  const id     = document.getElementById('er-id').value;
  const name   = document.getElementById('er-name').value.trim();
  const local  = document.getElementById('er-local').value.trim();
  const remote = document.getElementById('er-remote').value.trim();
  if (!name || !local || !remote) return alert('名称、本地路径、远程路径不能为空');
  await api('PUT', `/api/repos/${id}`, {
    name, local, remote,
    excludes: document.getElementById('er-excludes').value,
  });
  closeModal('modal-edit-repo');
  await loadAll();
}

loadAll();
</script>

<!-- Edit Server Modal -->
<div class="modal-overlay" id="modal-edit-server">
  <div class="modal">
    <div class="modal-title">编辑服务器</div>
    <input type="hidden" id="es-id">
    <div class="form-row"><div class="field"><label>名称</label><input id="es-name" type="text"></div></div>
    <div class="form-row"><div class="field"><label>IP / 域名</label><input id="es-host" type="text"></div></div>
    <div class="form-row cols2">
      <div class="field"><label>端口</label><input id="es-port" type="text"></div>
      <div class="field"><label>用户名</label><input id="es-user" type="text"></div>
    </div>
    <div class="form-row">
      <div class="field">
        <label>认证方式</label>
        <select id="es-auth" onchange="toggleEditAuthMode()">
          <option value="key">SSH 密钥</option>
          <option value="password">密码</option>
        </select>
      </div>
    </div>
    <div id="es-key-group" class="form-row"><div class="field"><label>SSH 密钥路径</label><input id="es-key" type="text" placeholder="~/.ssh/id_rsa"></div></div>
    <div id="es-pass-group" class="form-row" style="display:none;"><div class="field">
      <label>新密码（留空保持不变）</label>
      <div style="position:relative;">
        <input id="es-password" placeholder="留空则不修改密码" type="password" style="padding-right:40px;">
        <button type="button" onclick="toggleEditPassVis()" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text3);cursor:pointer;font-size:12px;font-family:var(--sans);" id="es-pass-eye">显示</button>
      </div>
    </div></div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('modal-edit-server')">取消</button>
      <button class="btn btn-green" onclick="saveServer()">保存</button>
    </div>
  </div>
</div>

<!-- Edit Repo Modal -->
<div class="modal-overlay" id="modal-edit-repo">
  <div class="modal">
    <div class="modal-title">编辑仓库</div>
    <input type="hidden" id="er-id">
    <div class="form-row"><div class="field"><label>仓库名称</label><input id="er-name" type="text"></div></div>
    <div class="form-row"><div class="field"><label>本地路径</label><input id="er-local" type="text"></div></div>
    <div class="form-row"><div class="field"><label>远程路径</label><input id="er-remote" type="text"></div></div>
    <div class="form-row"><div class="field"><label>额外排除规则（逗号分隔）</label><input id="er-excludes" type="text" placeholder="*.log, .env, node_modules/"></div></div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('modal-edit-repo')">取消</button>
      <button class="btn btn-green" onclick="saveRepo()">保存</button>
    </div>
  </div>
</div>

</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    print("=" * 50)
    print("  CodeSync — Web UI for rsync sync manager")
    print("  Open: http://localhost:7788")
    print("  Config saved to: ~/.codesync/config.json")
    print("=" * 50)
    app.run(host="0.0.0.0", port=7788, debug=False, threaded=True)
