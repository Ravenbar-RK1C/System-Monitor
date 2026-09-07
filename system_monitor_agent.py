#!/usr/bin/env python3
"""
system_monitor_web.py
-----------------------
Browser-based version of the System Monitor. Same machine list, same
config file, same local/remote stats sources and agent protocol as
system_monitor.py -- but served as a web page instead of a Tkinter
kiosk window, so it actually resizes usefully and can be embedded
elsewhere (e.g. HamDashBoard).

Config lives in system_monitor_config.json next to this script (shared
format with system_monitor.py):

    {
        "machines": [
            {"id": "local", "label": "This Machine", "type": "local"}
        ],
        "poll_interval_ms": 1000,
        "history_length": 60
    }

To add a remote machine, run system_monitor_agent.py on it and add:

    {"id": "shackpi", "label": "Graywolf", "type": "remote",
     "host": "192.168.1.50", "port": 5006}

Run:
    python3 system_monitor_web.py

Then open in a browser:
    http://<this-machine>:8767/                     full dashboard, all machines
    http://<this-machine>:8767/?machine=shackpi      dashboard, preselect a machine
    http://<this-machine>:8767/?machine=shackpi&embed=1   bare graph, no nav/chrome
                                                       (use this URL in an <iframe>
                                                       to drop a single machine's
                                                       card into HamDashBoard)

Requires: flask, psutil  (pip install flask psutil --break-system-packages)
"""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from collections import deque

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

try:
    import psutil
except ImportError:
    psutil = None
    print(
        "[MONITOR WARNING] psutil not installed -- local monitoring will be "
        "unavailable. Install it with: pip install psutil --break-system-packages"
    )

try:
    import paramiko
except ImportError:
    paramiko = None
    print(
        "[MONITOR WARNING] paramiko not installed -- the setup page's \"auto-install "
        "agent\" feature will be unavailable. Install it with: "
        "pip install paramiko --break-system-packages"
    )

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_monitor_config.json")

DEFAULT_MACHINES = [
    {"id": "local", "label": socket.gethostname(), "type": "local"},
]
DEFAULT_POLL_INTERVAL_MS = 1000
DEFAULT_HISTORY_LENGTH = 60
REMOTE_TIMEOUT_SECONDS = 2
WEB_PORT = 8767


# ============================================================
# CONFIG FILE PERSISTENCE (same schema/file as system_monitor.py)
# ============================================================

def load_config():
    machines = DEFAULT_MACHINES
    poll_interval_ms = DEFAULT_POLL_INTERVAL_MS
    history_length = DEFAULT_HISTORY_LENGTH

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            machines = data.get("machines", machines)
            poll_interval_ms = data.get("poll_interval_ms", poll_interval_ms)
            history_length = data.get("history_length", history_length)
            print(f"[CONFIG] Loaded {len(machines)} machine(s) from {CONFIG_FILE}")
        except Exception as err:
            print(f"[CONFIG ERROR] Failed to load {CONFIG_FILE}: {err}")
    else:
        save_config(machines, poll_interval_ms, history_length)

    return machines, poll_interval_ms, history_length


def save_config(machines, poll_interval_ms, history_length):
    data = {
        "machines": machines,
        "poll_interval_ms": poll_interval_ms,
        "history_length": history_length,
    }
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=4)
        print(f"[CONFIG] Saved to {CONFIG_FILE}")
    except Exception as err:
        print(f"[CONFIG ERROR] Failed to save {CONFIG_FILE}: {err}")


MACHINES, POLL_INTERVAL_MS, HISTORY_LENGTH = load_config()


# ============================================================
# STATS SOURCES (unchanged logic from system_monitor.py)
# ============================================================

_local_last_counters = None
_local_last_time = None
_local_last_disk_counters = None
_local_last_disk_time = None

# Disk I/O is filtered down to physical media only (SD card / HDD / SSD / NVMe)
# so the numbers reflect actual wear rather than RAM-backed or overlay I/O.
# Excludes: loop mounts, device-mapper volumes (LVM/LUKS), zram (RAM-backed
# swap -- common on Pis specifically to spare the SD card), ram disks, optical
# drives, and software-RAID md devices. Also excludes partitions (e.g.
# mmcblk0p1, sda1) since Linux reports their I/O on the parent whole-disk
# entry too -- counting both would double the total.
_VIRTUAL_DISK_PREFIXES = ("loop", "dm-", "ram", "zram", "sr", "fd", "md")
_PARTITION_RE = re.compile(
    r'^(mmcblk\d+)p\d+$|^(nvme\d+n\d+)p\d+$|^(sd[a-z]+)\d+$|^(hd[a-z]+)\d+$|^(vd[a-z]+)\d+$'
)


def _is_physical_disk(name):
    """Best-effort check that a Linux block-device name is physical media
    (not a loop/dm/zram/ram virtual device or a partition)."""
    if name.startswith(_VIRTUAL_DISK_PREFIXES):
        return False
    if _PARTITION_RE.match(name):
        return False
    return True


def get_local_power_saving():
    """Return the active Linux power profile when power-profiles-daemon is available."""
    try:
        result = subprocess.run(
            ["powerprofilesctl", "get"],
            capture_output=True, text=True, timeout=1
        )
        if result.returncode == 0:
            profile = result.stdout.strip().lower()
            return profile == "power-saver"
    except Exception:
        pass
    return None


def get_local_cpu_temp():
    """Return the CPU temperature in Celsius, or None if no sensor is available."""
    if psutil is None or not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    if not temps:
        return None
    for key in ("cpu_thermal", "coretemp", "k10temp", "cpu-thermal", "soc_thermal"):
        if key in temps and temps[key]:
            return temps[key][0].current
    first = next(iter(temps.values()), None)
    return first[0].current if first else None


def get_local_disk_io():
    """Return (read_kbs, write_kbs) since the last call, summed across physical
    disks only -- see _is_physical_disk. Falls back to (0.0, 0.0) if per-disk
    stats aren't available on this platform."""
    global _local_last_disk_counters, _local_last_disk_time

    if psutil is None or not hasattr(psutil, "disk_io_counters"):
        return 0.0, 0.0
    try:
        per_disk = psutil.disk_io_counters(perdisk=True)
    except Exception:
        return 0.0, 0.0
    if not per_disk:
        return 0.0, 0.0

    read_bytes = sum(c.read_bytes for name, c in per_disk.items() if _is_physical_disk(name))
    write_bytes = sum(c.write_bytes for name, c in per_disk.items() if _is_physical_disk(name))

    now = time.monotonic()
    read_kbs = write_kbs = 0.0
    if _local_last_disk_counters is not None and _local_last_disk_time is not None:
        elapsed = max(now - _local_last_disk_time, 0.001)
        read_kbs = max((read_bytes - _local_last_disk_counters[0]) / 1024 / elapsed, 0)
        write_kbs = max((write_bytes - _local_last_disk_counters[1]) / 1024 / elapsed, 0)
    _local_last_disk_counters = (read_bytes, write_bytes)
    _local_last_disk_time = now
    return read_kbs, write_kbs


def get_local_stats():
    """Read this machine's own stats directly via psutil."""
    global _local_last_counters, _local_last_time

    if psutil is None:
        return None

    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent

    now = time.monotonic()
    counters = psutil.net_io_counters()
    up_kbs = down_kbs = 0.0
    if _local_last_counters is not None and _local_last_time is not None:
        elapsed = max(now - _local_last_time, 0.001)
        up_kbs = max((counters.bytes_sent - _local_last_counters.bytes_sent) / 1024 / elapsed, 0)
        down_kbs = max((counters.bytes_recv - _local_last_counters.bytes_recv) / 1024 / elapsed, 0)
    _local_last_counters = counters
    _local_last_time = now

    disk_read_kbs, disk_write_kbs = get_local_disk_io()

    power_saving = get_local_power_saving()
    cpu_temp = get_local_cpu_temp()
    return {"cpu": cpu, "ram": ram, "net_up": up_kbs, "net_down": down_kbs,
            "disk_read": disk_read_kbs, "disk_write": disk_write_kbs,
            "power_saving": power_saving, "cpu_temp": cpu_temp}


def get_remote_stats(host, port):
    """Query a machine running system_monitor_agent.py over TCP."""
    try:
        with socket.create_connection((host, port), timeout=REMOTE_TIMEOUT_SECONDS) as sock:
            sock.sendall(b"STATS\n")
            data = sock.recv(1024).decode("utf-8", errors="replace").strip()
        parts = data.split("|")
        _hostname, cpu, ram, up_kbs, down_kbs = parts[:5]
        power_saving = None
        if len(parts) >= 6 and parts[5].strip() != "":
            value = parts[5].strip().lower()
            if value in ("1", "true", "on", "yes", "power-saver"):
                power_saving = True
            elif value in ("0", "false", "off", "no", "balanced", "performance"):
                power_saving = False
        cpu_temp = None
        if len(parts) >= 7 and parts[6].strip() != "":
            try:
                cpu_temp = float(parts[6])
            except ValueError:
                cpu_temp = None
        # Disk I/O fields are optional -- older system_monitor_agent.py builds
        # won't send them, so fall back to 0.0 rather than breaking the reading.
        disk_read_kbs = 0.0
        if len(parts) >= 8 and parts[7].strip() != "":
            try:
                disk_read_kbs = float(parts[7])
            except ValueError:
                disk_read_kbs = 0.0
        disk_write_kbs = 0.0
        if len(parts) >= 9 and parts[8].strip() != "":
            try:
                disk_write_kbs = float(parts[8])
            except ValueError:
                disk_write_kbs = 0.0
        return {"cpu": float(cpu), "ram": float(ram), "net_up": float(up_kbs), "net_down": float(down_kbs),
                "disk_read": disk_read_kbs, "disk_write": disk_write_kbs,
                "power_saving": power_saving, "cpu_temp": cpu_temp}
    except Exception as err:
        print(f"[REMOTE ERROR] {host}:{port} -- {err}")
        return None


def get_stats(machine):
    if machine["type"] == "local":
        return get_local_stats()
    elif machine["type"] == "remote":
        return get_remote_stats(machine["host"], machine["port"])
    return None


# ============================================================
# REMOTE AGENT AUTO-DEPLOYMENT (Linux/Pi targets only)
# ============================================================

AGENT_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_monitor_agent.py")


def get_agent_file_info():
    if not os.path.exists(AGENT_SCRIPT_PATH):
        return None
    stat = os.stat(AGENT_SCRIPT_PATH)
    return {
        "path": AGENT_SCRIPT_PATH,
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _ssh_run(client, cmd, input_text=None, timeout=30):
    """Run a command over an open paramiko SSHClient, optionally feeding stdin
    (used for piping a sudo password + heredoc content). Returns (exit_status, stdout, stderr)."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    if input_text is not None:
        stdin.write(input_text)
        stdin.flush()
    stdin.channel.shutdown_write()
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return exit_status, out, err


def deploy_agent_via_ssh(host, ssh_port, username, password, key_path, key_passphrase,
                          install_service, sudo_password, agent_port):
    """Copy system_monitor_agent.py to a remote Linux/Pi machine over SSH, install
    psutil, and launch it. Returns (ok: bool, message: str). Never persists any
    credentials -- they're only used for this one deployment call."""
    if paramiko is None:
        return False, ("paramiko is not installed on this dashboard machine. "
                        "Install it with: pip install paramiko --break-system-packages")

    if not os.path.exists(AGENT_SCRIPT_PATH):
        return False, ("system_monitor_agent.py wasn't found next to system_monitor.py on "
                        "this dashboard machine -- place it in the same folder first.")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=ssh_port, username=username,
            password=password or None,
            key_filename=key_path or None,
            passphrase=key_passphrase or None,
            timeout=10,
        )
    except Exception as err:
        return False, f"SSH connection failed: {err}"

    try:
        try:
            sftp = client.open_sftp()
            remote_home = sftp.normalize(".")
            remote_agent_path = f"{remote_home}/system_monitor_agent.py"
            sftp.put(AGENT_SCRIPT_PATH, remote_agent_path)
            sftp.close()
        except Exception as err:
            return False, f"Failed to copy the agent script over SFTP: {err}"

        # Install psutil without needing sudo (--user). Best-effort: don't hard-fail
        # here since it may already be installed -- the reachability check at the
        # end is the real pass/fail signal.
        _ssh_run(
            client,
            "pip3 install --user --break-system-packages psutil >/dev/null 2>&1 "
            "|| pip3 install --user psutil >/dev/null 2>&1",
        )

        # Stop any previous instance so re-deploying doesn't leave duplicates running.
        _ssh_run(client, "pkill -f system_monitor_agent.py >/dev/null 2>&1; sleep 1")

        # Launch it detached from this SSH session so it keeps running after we disconnect.
        _ssh_run(client, f"nohup python3 {remote_agent_path} > ~/system_monitor_agent.log 2>&1 < /dev/null & disown")

        service_note = ""
        if install_service:
            unit_content = (
                "[Unit]\n"
                "Description=System Monitor Stats Agent\n"
                "After=network-online.target\n"
                "Wants=network-online.target\n\n"
                "[Service]\n"
                f"ExecStart=/usr/bin/python3 {remote_agent_path}\n"
                "Restart=on-failure\n"
                f"User={username}\n\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n"
            )
            if sudo_password:
                sudo_prefix = "sudo -S -p ''"
                write_input = sudo_password + "\n" + unit_content
                cred_input = sudo_password + "\n"
            else:
                sudo_prefix = "sudo -n"
                write_input = unit_content
                cred_input = None

            status, out, err = _ssh_run(
                client,
                f"{sudo_prefix} tee /etc/systemd/system/system-monitor-agent.service > /dev/null",
                input_text=write_input,
            )
            if status == 0:
                _ssh_run(client, f"{sudo_prefix} systemctl daemon-reload", input_text=cred_input)
                status2, out2, err2 = _ssh_run(
                    client, f"{sudo_prefix} systemctl enable --now system-monitor-agent.service",
                    input_text=cred_input,
                )
                if status2 == 0:
                    service_note = " Installed as a systemd service, so it'll auto-start on reboot."
                else:
                    service_note = (f" Note: it's running now, but installing it as a systemd "
                                     f"service failed ({(err2 or out2).strip()[:200]}), so it won't "
                                     f"auto-start on reboot -- you can set that up manually later.")
            else:
                service_note = (f" Note: it's running now, but installing it as a systemd service "
                                 f"failed ({(err or out).strip()[:200]}), so it won't auto-start on "
                                 f"reboot -- check that sudo access works for this user.")
    finally:
        client.close()

    # Verify it's actually reachable before declaring success -- everything above
    # can "succeed" while the agent still isn't actually listening.
    reachable = False
    for _ in range(6):
        try:
            with socket.create_connection((host, agent_port), timeout=2) as sock:
                sock.sendall(b"STATS\n")
                if sock.recv(1024):
                    reachable = True
                    break
        except Exception:
            pass
        time.sleep(1)

    if not reachable:
        return False, ("Files were copied and the agent was launched, but it isn't responding "
                        f"on port {agent_port} yet. Check ~/system_monitor_agent.log on the "
                        "target machine, and make sure nothing (like a firewall) is blocking the port.")

    return True, "Agent is up and running." + service_note


# ============================================================
# SHARED STATE + BACKGROUND POLLER
# ============================================================

def make_history():
    return {
        "cpu": deque(maxlen=HISTORY_LENGTH),
        "ram": deque(maxlen=HISTORY_LENGTH),
        "net_up": deque(maxlen=HISTORY_LENGTH),
        "net_down": deque(maxlen=HISTORY_LENGTH),
        "disk_read": deque(maxlen=HISTORY_LENGTH),
        "disk_write": deque(maxlen=HISTORY_LENGTH),
    }


_state_lock = threading.Lock()
histories = {m["id"]: make_history() for m in MACHINES}
machine_unreachable = {m["id"]: False for m in MACHINES}
machine_online = {m["id"]: None for m in MACHINES}   # None = not checked yet
power_saving_status = {m["id"]: None for m in MACHINES}
cpu_temp_status = {m["id"]: None for m in MACHINES}
last_viewed_at = {m["id"]: 0.0 for m in MACHINES}

VIEW_ACTIVE_WINDOW_SECONDS = 8   # a machine polled more recently than this counts as "being viewed"
LIVENESS_INTERVAL_SECONDS = 30   # offline-detection cadence for machines nobody's currently viewing


def is_recently_viewed(machine_id):
    return (time.time() - last_viewed_at[machine_id]) <= VIEW_ACTIVE_WINDOW_SECONDS


def record_stats(machine_id, stats):
    """Apply a fresh stats reading (or a failure) to the shared state. Caller holds no lock."""
    with _state_lock:
        if stats is None:
            machine_unreachable[machine_id] = True
            machine_online[machine_id] = False
        else:
            machine_unreachable[machine_id] = False
            machine_online[machine_id] = True
            hist = histories[machine_id]
            hist["cpu"].append(stats["cpu"])
            hist["ram"].append(stats["ram"])
            hist["net_up"].append(stats["net_up"])
            hist["net_down"].append(stats["net_down"])
            hist["disk_read"].append(stats.get("disk_read", 0.0))
            hist["disk_write"].append(stats.get("disk_write", 0.0))
            power_saving_status[machine_id] = stats.get("power_saving")
            cpu_temp_status[machine_id] = stats.get("cpu_temp")


def slugify(text):
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip()).strip("_").lower()
    return slug or "machine"


def init_machine_state(machine_id):
    histories[machine_id] = make_history()
    machine_unreachable[machine_id] = False
    machine_online[machine_id] = None
    power_saving_status[machine_id] = None
    cpu_temp_status[machine_id] = None
    last_viewed_at[machine_id] = 0.0


def clear_machine_state(machine_id):
    histories.pop(machine_id, None)
    machine_unreachable.pop(machine_id, None)
    machine_online.pop(machine_id, None)
    power_saving_status.pop(machine_id, None)
    cpu_temp_status.pop(machine_id, None)
    last_viewed_at.pop(machine_id, None)


def poll_loop():
    """Fast loop: full stats (feeds the graph history) for local machines always,
    and for any remote machine that's actively being viewed right now."""
    if psutil is not None:
        psutil.cpu_percent(interval=None)
        psutil.net_io_counters()

    while True:
        for machine in list(MACHINES):  # snapshot -- machines can be added/removed via /setup
            if machine["type"] != "local" and not is_recently_viewed(machine["id"]):
                continue  # not being watched -- liveness_loop covers offline detection for it
            record_stats(machine["id"], get_stats(machine))
        time.sleep(max(POLL_INTERVAL_MS, 250) / 1000)


def liveness_loop():
    """Slow loop: a lightweight reachability check for remote machines nobody's
    actively viewing, purely to keep their nav-tab online/offline status current."""
    while True:
        for machine in list(MACHINES):  # snapshot -- machines can be added/removed via /setup
            if machine["type"] == "local" or is_recently_viewed(machine["id"]):
                continue  # already kept fresh by the fast loop
            stats = get_remote_stats(machine["host"], machine["port"])
            with _state_lock:
                machine_online[machine["id"]] = stats is not None
                machine_unreachable[machine["id"]] = stats is None
        time.sleep(LIVENESS_INTERVAL_SECONDS)


# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)


@app.route("/api/machines")
def api_machines():
    with _state_lock:
        return jsonify([
            {"id": m["id"], "label": m["label"], "online": machine_online[m["id"]],
             "description": m.get("description", "")}
            for m in MACHINES
        ])


@app.route("/api/stats/<machine_id>")
def api_stats(machine_id):
    if machine_id not in histories:
        return jsonify({"error": "unknown machine"}), 404

    machine = next(m for m in MACHINES if m["id"] == machine_id)
    # If this machine was cold (nobody watching it), fetch once synchronously so
    # the graph doesn't sit empty for up to a full poll tick after switching tabs.
    if not is_recently_viewed(machine_id):
        record_stats(machine_id, get_stats(machine))
    last_viewed_at[machine_id] = time.time()

    with _state_lock:
        hist = histories[machine_id]
        return jsonify({
            "unreachable": machine_unreachable[machine_id],
            "online": machine_online[machine_id],
            "power_saving": power_saving_status[machine_id],
            "cpu_temp": cpu_temp_status[machine_id],
            "history": {
                "cpu": list(hist["cpu"]),
                "ram": list(hist["ram"]),
                "net_up": list(hist["net_up"]),
                "net_down": list(hist["net_down"]),
                "disk_read": list(hist["disk_read"]),
                "disk_write": list(hist["disk_write"]),
            },
        })


@app.route("/setup/upload-agent", methods=["POST"])
def setup_upload_agent():
    file = request.files.get("agent_file")
    if not file or not file.filename:
        return redirect(url_for("setup_page", error="No file selected."))

    raw = file.read()
    try:
        content_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return redirect(url_for("setup_page", error="That file doesn't look like a text/Python file."))

    try:
        compile(content_text, "system_monitor_agent.py", "exec")
    except SyntaxError as err:
        return redirect(url_for("setup_page", error=f"That file has a Python syntax error: {err}"))

    try:
        if os.path.exists(AGENT_SCRIPT_PATH):
            shutil.copyfile(AGENT_SCRIPT_PATH, AGENT_SCRIPT_PATH + ".bak")
        with open(AGENT_SCRIPT_PATH, "w", encoding="utf-8") as f:
            f.write(content_text)
    except Exception as err:
        return redirect(url_for("setup_page", error=f"Failed to save the agent file: {err}"))

    return redirect(url_for(
        "setup_page",
        success=(f"Agent file updated ({len(content_text)} bytes). Existing machines keep running "
                 "their old copy until you click \"Update Agent\" on each one."),
    ))


@app.route("/setup")
def setup_page():
    with _state_lock:
        machines_snapshot = list(MACHINES)
        online_snapshot = dict(machine_online)
    return render_template_string(
        SETUP_TEMPLATE,
        machines=machines_snapshot,
        online=online_snapshot,
        agent_info=get_agent_file_info(),
        agent_script_path=AGENT_SCRIPT_PATH,
        error=request.args.get("error"),
        success=request.args.get("success"),
    )


@app.route("/setup/add", methods=["POST"])
def setup_add():
    label = request.form.get("label", "").strip()
    machine_type = request.form.get("type", "").strip()
    raw_id = request.form.get("id", "").strip()
    host = request.form.get("host", "").strip()
    port_raw = request.form.get("port", "").strip()
    auto_install = request.form.get("auto_install") == "on"

    if not label:
        return redirect(url_for("setup_page", error="Label is required."))
    if machine_type not in ("local", "remote"):
        return redirect(url_for("setup_page", error="Invalid machine type."))

    machine_id = slugify(raw_id or label)

    with _state_lock:
        existing_ids = {m["id"] for m in MACHINES}
    if machine_id in existing_ids:
        return redirect(url_for("setup_page", error=f"A machine with id '{machine_id}' already exists."))

    description = request.form.get("description", "").strip()

    if machine_type == "local":
        with _state_lock:
            if any(m["type"] == "local" for m in MACHINES):
                return redirect(url_for("setup_page", error="Only one local machine entry is supported."))
        new_machine = {"id": machine_id, "label": label, "type": "local", "description": description}
    else:
        if not host:
            return redirect(url_for("setup_page", error="Host is required for a remote machine."))
        try:
            port = int(port_raw) if port_raw else 5006
        except ValueError:
            return redirect(url_for("setup_page", error="Port must be a number."))
        new_machine = {"id": machine_id, "label": label, "type": "remote", "host": host, "port": port,
                       "description": description}

        if auto_install:
            ssh_port_raw = request.form.get("ssh_port", "").strip()
            try:
                ssh_port = int(ssh_port_raw) if ssh_port_raw else 22
            except ValueError:
                return redirect(url_for("setup_page", error="SSH port must be a number."))
            ssh_username = request.form.get("ssh_username", "").strip()
            auth_method = request.form.get("auth_method", "").strip()
            ssh_password = request.form.get("ssh_password", "")
            key_path = request.form.get("key_path", "").strip()
            key_passphrase = request.form.get("key_passphrase", "")
            install_service = request.form.get("install_service") == "on"
            sudo_password = request.form.get("sudo_password", "")

            if not ssh_username:
                return redirect(url_for("setup_page", error="SSH username is required for auto-install."))
            if auth_method == "key" and not key_path:
                return redirect(url_for("setup_page", error="Private key path is required for key-based auth."))
            if auth_method == "password" and not ssh_password:
                return redirect(url_for("setup_page", error="SSH password is required for password auth."))

            ok, message = deploy_agent_via_ssh(
                host=host, ssh_port=ssh_port, username=ssh_username,
                password=ssh_password if auth_method == "password" else None,
                key_path=key_path if auth_method == "key" else None,
                key_passphrase=key_passphrase if auth_method == "key" else None,
                install_service=install_service, sudo_password=sudo_password,
                agent_port=port,
            )
            if not ok:
                return redirect(url_for("setup_page", error=f"Auto-install failed: {message}"))

    with _state_lock:
        # Initialize state before the machine becomes visible to the poll loops,
        # so nothing can look it up in MACHINES before its dict entries exist.
        init_machine_state(machine_id)
        MACHINES.append(new_machine)
        save_config(MACHINES, POLL_INTERVAL_MS, HISTORY_LENGTH)

    success_msg = f"Added '{label}'."
    if machine_type == "remote" and auto_install:
        success_msg += f" {message}"
    return redirect(url_for("setup_page", success=success_msg))


@app.route("/setup/remove/<machine_id>", methods=["POST"])
def setup_remove(machine_id):
    with _state_lock:
        match = next((m for m in MACHINES if m["id"] == machine_id), None)
        if match is None:
            return redirect(url_for("setup_page", error="Machine not found."))
        # Remove from MACHINES before clearing state dicts, so a poll loop that
        # already grabbed a snapshot containing this id can't hit a KeyError.
        MACHINES[:] = [m for m in MACHINES if m["id"] != machine_id]
        clear_machine_state(machine_id)
        save_config(MACHINES, POLL_INTERVAL_MS, HISTORY_LENGTH)

    return redirect(url_for("setup_page", success=f"Removed '{match['label']}'."))


@app.route("/setup/edit/<machine_id>", methods=["POST"])
def setup_edit(machine_id):
    description = request.form.get("description", "").strip()
    with _state_lock:
        match = next((m for m in MACHINES if m["id"] == machine_id), None)
        if match is None:
            return redirect(url_for("setup_page", error="Machine not found."))
        match["description"] = description
        save_config(MACHINES, POLL_INTERVAL_MS, HISTORY_LENGTH)

    return redirect(url_for("setup_page", success=f"Updated description for '{match['label']}'."))


@app.route("/setup/update-agent/<machine_id>", methods=["POST"])
def setup_update_agent(machine_id):
    with _state_lock:
        match = next((m for m in MACHINES if m["id"] == machine_id), None)
    if match is None or match["type"] != "remote":
        return redirect(url_for("setup_page", error="Machine not found or not a remote machine."))

    ssh_port_raw = request.form.get("ssh_port", "").strip()
    try:
        ssh_port = int(ssh_port_raw) if ssh_port_raw else 22
    except ValueError:
        return redirect(url_for("setup_page", error="SSH port must be a number."))
    ssh_username = request.form.get("ssh_username", "").strip()
    auth_method = request.form.get("auth_method", "").strip()
    ssh_password = request.form.get("ssh_password", "")
    key_path = request.form.get("key_path", "").strip()
    key_passphrase = request.form.get("key_passphrase", "")
    install_service = request.form.get("install_service") == "on"
    sudo_password = request.form.get("sudo_password", "")

    if not ssh_username:
        return redirect(url_for("setup_page", error="SSH username is required."))
    if auth_method == "key" and not key_path:
        return redirect(url_for("setup_page", error="Private key path is required for key-based auth."))
    if auth_method == "password" and not ssh_password:
        return redirect(url_for("setup_page", error="SSH password is required for password auth."))

    # This re-copies the current local agent script and relaunches it -- an
    # "update" is functionally identical to the initial install.
    ok, message = deploy_agent_via_ssh(
        host=match["host"], ssh_port=ssh_port, username=ssh_username,
        password=ssh_password if auth_method == "password" else None,
        key_path=key_path if auth_method == "key" else None,
        key_passphrase=key_passphrase if auth_method == "key" else None,
        install_service=install_service, sudo_password=sudo_password,
        agent_port=match["port"],
    )
    if not ok:
        return redirect(url_for("setup_page", error=f"Update failed for '{match['label']}': {message}"))
    return redirect(url_for("setup_page", success=f"Updated agent on '{match['label']}'. {message}"))


@app.route("/")
def index():
    machine_id = request.args.get("machine") or (MACHINES[0]["id"] if MACHINES else None)
    embed = request.args.get("embed", "").lower() in ("1", "true", "yes")
    return render_template_string(
        PAGE_TEMPLATE,
        machines=MACHINES,
        initial_machine=machine_id,
        embed=embed,
        poll_ms=max(POLL_INTERVAL_MS, 250),
        history_len=HISTORY_LENGTH,
    )


PAGE_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>System Monitor</title>
<style>
  html, body { height: 100%; margin: 0; background: #1e1e1e; color: #ffffff;
               font-family: Helvetica, Arial, sans-serif; }
  #app { display: flex; flex-direction: column; height: 100%; }
  #tabbar { display: {{ 'none' if embed else 'flex' }}; background: #111111;
            flex-wrap: wrap; }
  #tabbar button { flex: 1 1 auto; min-width: 90px; padding: 10px 10px 8px; border: 3px solid transparent;
                   background: #7f8c8d; color: white; font-weight: bold;
                   cursor: pointer; margin: 2px; border-radius: 4px; box-sizing: border-box;
                   line-height: 1.25; }
  #tabbar button .tabLabel { display: block; font-size: 18px; font-weight: bold; }
  #tabbar button .tabDesc { display: block; font-size: 11px; font-weight: normal; opacity: 0.8;
                             margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #tabbar button.online { background: #27ae60; }
  #tabbar button.offline { background: #c0392b; }
  #tabbar button.active { border-color: #ffffff; }
  #title { display: {{ 'none' if embed else 'block' }}; text-align: center;
           font-size: 20px; font-weight: bold; padding: 10px 0 4px; }
  #statsRow { display: flex; background: #232b2f; border-radius: 6px; margin: 10px;
              padding: 6px 0; flex-wrap: wrap; }
  #statsRow .stat { flex: 1; min-width: 70px; text-align: center; margin: 3px;
                     border: 2px solid #444; border-radius: 6px; padding: 4px 2px; }
  #statsRow .stat.cpu { border-color: #2ecc71; }
  #statsRow .stat.ram { border-color: #3498db; }
  #statsRow .stat.up { border-color: #9b59b6; }
  #statsRow .stat.down { border-color: #e67e22; }
  #statsRow .stat.diskread { border-color: #1abc9c; }
  #statsRow .stat.diskwrite { border-color: #f1c40f; }
  #statsRow .stat .label { color: #7f8c8d; font-size: 11px; font-weight: bold; }
  #statsRow .stat .value { font-size: 22px; font-weight: bold; }
  #statsRow .stat .toggle { margin-top: 3px; }
  #statsRow .stat .toggle input { cursor: pointer; }
  #statsRow .stat.dim { opacity: 0.4; }
  #powerLabel { position: absolute; right: 20px; top: 14px; font-size: 11px;
                color: #7f8c8d; font-weight: bold; }
  #setupBtn { display: {{ 'none' if embed else 'inline-block' }}; position: fixed; top: 8px; right: 12px;
              font-size: 20px; text-decoration: none; color: #7f8c8d; z-index: 10; }
  #setupBtn:hover { color: #ffffff; }
  #graphWrap { position: relative; flex: 1; margin: 0 10px 10px; background: #232b2f;
               border-radius: 6px; min-height: 60px; padding: 16px 8px; box-sizing: border-box; }
  canvas { width: 100%; height: 100%; display: block; }
  #unreachableMsg { position: absolute; inset: 0; display: none; align-items: center;
                    justify-content: center; color: #e74c3c; font-size: 18px;
                    font-weight: bold; }
</style>
</head>
<body>
<div id="app">
  <a id="setupBtn" href="/setup" title="Setup">&#9881;</a>
  <div id="title">SYSTEM MONITOR</div>
  <div id="tabbar"></div>
  <div id="statsRow" style="position: relative;">
    <div class="stat cpu" data-key="cpu"><div class="label">CPU</div><div class="value" id="valCpu">--</div>
      <div class="toggle"><input type="checkbox" class="statToggle" data-key="cpu" checked></div></div>
    <div class="stat ram" data-key="ram"><div class="label">RAM</div><div class="value" id="valRam">--</div>
      <div class="toggle"><input type="checkbox" class="statToggle" data-key="ram" checked></div></div>
    <div class="stat up" data-key="up"><div class="label">UPLOAD</div><div class="value" id="valUp">--</div>
      <div class="toggle"><input type="checkbox" class="statToggle" data-key="up" checked></div></div>
    <div class="stat down" data-key="down"><div class="label">DOWNLOAD</div><div class="value" id="valDown">--</div>
      <div class="toggle"><input type="checkbox" class="statToggle" data-key="down" checked></div></div>
    <div class="stat diskread" data-key="diskRead"><div class="label">DISK READ</div><div class="value" id="valDiskRead">--</div>
      <div class="toggle"><input type="checkbox" class="statToggle" data-key="diskRead" checked></div></div>
    <div class="stat diskwrite" data-key="diskWrite"><div class="label">DISK WRITE</div><div class="value" id="valDiskWrite">--</div>
      <div class="toggle"><input type="checkbox" class="statToggle" data-key="diskWrite" checked></div></div>
    <div class="stat"><div class="label">CPU TEMP</div><div class="value" id="valTemp">--</div></div>
    <div id="powerLabel">POWER SAVING: --</div>
  </div>
  <div id="graphWrap">
    <canvas id="graph"></canvas>
    <div id="unreachableMsg">MACHINE UNREACHABLE</div>
  </div>
</div>

<script>
const POLL_MS = {{ poll_ms }};
const HISTORY_LEN = {{ history_len }};
const EMBED = {{ embed | tojson }};
let currentMachine = {{ initial_machine | tojson }};
let machines = [];

const canvas = document.getElementById('graph');
const ctx = canvas.getContext('2d');

function resizeCanvas() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener('resize', () => { resizeCanvas(); if (lastData) drawGraph(lastData); });

let lastData = null;

const visibility = { cpu: true, ram: true, up: true, down: true, diskRead: true, diskWrite: true };

document.querySelectorAll('.statToggle').forEach(cb => {
  cb.addEventListener('change', () => {
    visibility[cb.dataset.key] = cb.checked;
    cb.closest('.stat').classList.toggle('dim', !cb.checked);
    if (lastData) drawGraph(lastData);
  });
});

function scaledPoints(values, plotW, plotH, padLeft, padTop, scaleMax) {
  const pts = [];
  for (let i = 0; i < values.length; i++) {
    const x = padLeft + (i / (HISTORY_LEN - 1)) * plotW;
    const y = padTop + plotH - (Math.min(values[i], scaleMax) / scaleMax) * plotH;
    pts.push([x, y]);
  }
  const shift = (plotW + padLeft) - pts[pts.length - 1][0];
  return pts.map(([x, y]) => [x + shift, y]);
}

function drawLine(points, color) {
  if (points.length < 2) return;
  ctx.beginPath();
  ctx.moveTo(points[0][0], points[0][1]);
  for (let i = 1; i < points.length; i++) ctx.lineTo(points[i][0], points[i][1]);
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();
}

function drawGraph(data) {
  const rect = canvas.parentElement.getBoundingClientRect();
  const width = rect.width, height = rect.height;
  ctx.clearRect(0, 0, width, height);

  document.getElementById('unreachableMsg').style.display = data.unreachable ? 'flex' : 'none';
  if (data.unreachable) return;
  if (width <= 1 || height <= 1) return;

  const padLeft = 46, padRight = 54, padTop = 14, padBottom = 10;
  const plotW = Math.max(width - padLeft - padRight, 1);
  const plotH = Math.max(height - padTop - padBottom, 1);

  const cpu = data.history.cpu.length ? data.history.cpu : [0];
  const ram = data.history.ram.length ? data.history.ram : [0];
  const up = data.history.net_up.length ? data.history.net_up : [0];
  const down = data.history.net_down.length ? data.history.net_down : [0];
  const diskRead = (data.history.disk_read && data.history.disk_read.length) ? data.history.disk_read : [0];
  const diskWrite = (data.history.disk_write && data.history.disk_write.length) ? data.history.disk_write : [0];

  const pctScale = 100;
  const netScale = Math.max(
    Math.max(...up), Math.max(...down),
    Math.max(...diskRead), Math.max(...diskWrite),
    1
  ) * 1.2;

  ctx.font = '10px Helvetica';
  [0, 0.5, 1.0].forEach(frac => {
    const y = padTop + plotH - frac * plotH;
    ctx.strokeStyle = '#3a4a52';
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(padLeft + plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#7f8c8d';
    ctx.textAlign = 'right';
    ctx.fillText((pctScale * frac).toFixed(0) + '%', padLeft - 8, y + 3);
    ctx.textAlign = 'left';
    ctx.fillText((netScale * frac).toFixed(0), padLeft + plotW + 8, y + 3);
  });
  ctx.fillText('KB/s', padLeft + plotW + 8, padTop - 4);

  if (visibility.cpu) drawLine(scaledPoints(cpu, plotW, plotH, padLeft, padTop, pctScale), '#2ecc71');
  if (visibility.ram) drawLine(scaledPoints(ram, plotW, plotH, padLeft, padTop, pctScale), '#3498db');
  if (visibility.up) drawLine(scaledPoints(up, plotW, plotH, padLeft, padTop, netScale), '#9b59b6');
  if (visibility.down) drawLine(scaledPoints(down, plotW, plotH, padLeft, padTop, netScale), '#e67e22');
  if (visibility.diskRead) drawLine(scaledPoints(diskRead, plotW, plotH, padLeft, padTop, netScale), '#1abc9c');
  if (visibility.diskWrite) drawLine(scaledPoints(diskWrite, plotW, plotH, padLeft, padTop, netScale), '#f1c40f');

  document.getElementById('valCpu').textContent = cpu[cpu.length - 1].toFixed(0) + '%';
  document.getElementById('valRam').textContent = ram[ram.length - 1].toFixed(0) + '%';
  document.getElementById('valUp').textContent = up[up.length - 1].toFixed(1) + ' KB/s';
  document.getElementById('valDown').textContent = down[down.length - 1].toFixed(1) + ' KB/s';
  document.getElementById('valDiskRead').textContent = diskRead[diskRead.length - 1].toFixed(1) + ' KB/s';
  document.getElementById('valDiskWrite').textContent = diskWrite[diskWrite.length - 1].toFixed(1) + ' KB/s';
  document.getElementById('valTemp').textContent =
    (typeof data.cpu_temp === 'number') ? data.cpu_temp.toFixed(1) + '\u00B0C' : '--';

  const ps = data.power_saving;
  document.getElementById('powerLabel').textContent =
    'POWER SAVING: ' + (ps === true ? 'ON' : ps === false ? 'OFF' : '--');
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

function buildTabs() {
  const bar = document.getElementById('tabbar');
  bar.innerHTML = '';
  machines.forEach(m => {
    const btn = document.createElement('button');
    let html = `<span class="tabLabel">${escapeHtml(m.label.toUpperCase())}</span>`;
    if (m.description) {
      html += `<span class="tabDesc">${escapeHtml(m.description)}</span>`;
    }
    btn.innerHTML = html;
    const statusClass = m.online === true ? 'online' : m.online === false ? 'offline' : '';
    btn.className = statusClass + (m.id === currentMachine ? ' active' : '');
    btn.onclick = () => {
      currentMachine = m.id;
      const url = new URL(window.location);
      url.searchParams.set('machine', m.id);
      window.history.replaceState({}, '', url);
      buildTabs();
      poll();
    };
    bar.appendChild(btn);
  });
}

async function loadMachines() {
  try {
    const res = await fetch('/api/machines');
    machines = await res.json();
  } catch (err) {
    console.error('loadMachines failed', err);
    return;
  }
  if (!currentMachine && machines.length) currentMachine = machines[0].id;
  buildTabs();
}

async function poll() {
  if (!currentMachine) return;
  try {
    const res = await fetch('/api/stats/' + encodeURIComponent(currentMachine));
    if (!res.ok) return;
    const data = await res.json();
    lastData = data;
    drawGraph(data);
  } catch (err) {
    console.error('poll failed', err);
  }
}

(async function init() {
  resizeCanvas();
  await loadMachines();
  await poll();
  setInterval(poll, POLL_MS);
  // Tab online/offline coloring refreshes on its own, slower cadence -- it
  // doesn't need to be as frequent as the active machine's graph data.
  setInterval(loadMachines, 5000);
})();
</script>
</body>
</html>
"""


SETUP_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>System Monitor - Setup</title>
<style>
  body { margin: 0; background: #1e1e1e; color: #ffffff; font-family: Helvetica, Arial, sans-serif;
         padding: 16px; }
  h1 { font-size: 20px; }
  a.back { color: #7f8c8d; text-decoration: none; font-size: 13px; }
  a.back:hover { color: #ffffff; }
  .banner { padding: 10px 14px; border-radius: 6px; margin: 12px 0; font-size: 14px; }
  .banner.error { background: #c0392b33; border: 1px solid #c0392b; }
  .banner.success { background: #27ae6033; border: 1px solid #27ae60; }
  table { width: 100%; border-collapse: collapse; margin: 12px 0 24px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #333; font-size: 14px; }
  th { color: #7f8c8d; font-size: 12px; text-transform: uppercase; }
  .pill { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; }
  .pill.online { background: #27ae60; }
  .pill.offline { background: #c0392b; }
  .pill.unknown { background: #7f8c8d; }
  button, input[type=submit] { cursor: pointer; }
  .removeBtn { background: #c0392b; color: white; border: none; border-radius: 4px;
               padding: 6px 12px; font-weight: bold; }
  .removeBtn:hover { background: #e74c3c; }
  .descForm { display: flex; gap: 6px; align-items: center; }
  .descInput { background: #1e1e1e; color: white; border: 1px solid #444; border-radius: 4px;
               padding: 6px 8px; font-size: 13px; width: 200px; }
  .saveBtn { background: #34495e; color: white; border: none; border-radius: 4px;
             padding: 6px 10px; font-weight: bold; }
  .saveBtn:hover { background: #46627f; }
  form.addForm { background: #232b2f; border-radius: 8px; padding: 16px; max-width: 420px; }
  form.addForm label, .sshForm label { display: block; margin-top: 10px; font-size: 12px; color: #7f8c8d;
                        text-transform: uppercase; font-weight: bold; }
  form.addForm input, form.addForm select, .sshForm input, .sshForm select {
                        width: 100%; box-sizing: border-box; padding: 8px;
                        margin-top: 4px; border-radius: 4px; border: 1px solid #444;
                        background: #1e1e1e; color: white; font-size: 14px; }
  form.addForm .submitRow, .sshForm .submitRow { margin-top: 16px; }
  form.addForm .submitRow input, .sshForm .submitRow input { background: #2ecc71; border: none;
                        font-weight: bold; padding: 10px; width: auto; }
  .hint { color: #7f8c8d; font-size: 11px; margin-top: 4px; }
  .agentFileBox { background: #232b2f; border-radius: 8px; padding: 14px 16px; margin: 12px 0 20px; max-width: 600px; }
  .agentFileBox h2 { font-size: 14px; margin: 0 0 6px; }
  .uploadForm { display: flex; gap: 8px; align-items: center; margin: 10px 0 6px; }
  .uploadForm input[type=file] { color: #ddd; font-size: 13px; }
  .updateDetails { background: #232b2f; border-radius: 8px; padding: 4px 12px; max-width: 420px; }
  .updateDetails summary { cursor: pointer; padding: 8px 0; font-size: 13px; color: #7f8c8d; }
  .updateDetails summary:hover { color: #ffffff; }
  .updateDetails[open] summary { color: #ffffff; }
  .sshForm { padding-bottom: 12px; }
</style>
</head>
<body>
  <a class="back" href="/">&larr; Back to dashboard</a>
  <h1>SYSTEM MONITOR SETUP</h1>

  {% if error %}<div class="banner error">{{ error }}</div>{% endif %}
  {% if success %}<div class="banner success">{{ success }}</div>{% endif %}

  <div class="agentFileBox">
    <h2>Agent File Used for Deployments</h2>
    {% if agent_info %}
    <div class="hint">{{ agent_info.path }} &mdash; {{ agent_info.size }} bytes, last updated {{ agent_info.modified }}</div>
    {% else %}
    <div class="hint" style="color: #e74c3c;">No agent file found at {{ agent_script_path }} &mdash; auto-install and update will fail until one is uploaded.</div>
    {% endif %}
    <form method="post" action="{{ url_for('setup_upload_agent') }}" enctype="multipart/form-data" class="uploadForm">
      <input type="file" name="agent_file" accept=".py" required>
      <button type="submit" class="saveBtn">Upload</button>
    </form>
    <div class="hint">This is the file "Auto-install" and "Update Agent" copy to remote machines. Uploading a new version doesn't touch already-deployed machines until you click Update Agent on each one.</div>
  </div>

  <table>
    <tr><th>Label</th><th>Type</th><th>Host</th><th>Status</th><th>Description</th><th></th></tr>
    {% for m in machines %}
    <tr>
      <td>{{ m.label }}</td>
      <td>{{ m.type }}</td>
      <td>{{ (m.host ~ ':' ~ m.port) if m.type == 'remote' else '\u2014' }}</td>
      <td>
        {% set st = online.get(m.id) %}
        {% if st is sameas true %}<span class="pill online">ONLINE</span>
        {% elif st is sameas false %}<span class="pill offline">OFFLINE</span>
        {% else %}<span class="pill unknown">UNKNOWN</span>{% endif %}
      </td>
      <td>
        <form method="post" action="{{ url_for('setup_edit', machine_id=m.id) }}" class="descForm">
          <input type="text" name="description" value="{{ m.description or '' }}"
                 placeholder="What is this machine?" class="descInput">
          <button type="submit" class="saveBtn">Save</button>
        </form>
      </td>
      <td>
        <form method="post" action="{{ url_for('setup_remove', machine_id=m.id) }}"
              onsubmit="return confirm('Remove {{ m.label }}?');">
          <button type="submit" class="removeBtn">Remove</button>
        </form>
      </td>
    </tr>
    {% if m.type == 'remote' %}
    <tr>
      <td colspan="6" style="padding-top: 0; padding-bottom: 12px;">
        <details class="updateDetails">
          <summary>Update agent on {{ m.label }}</summary>
          <form method="post" action="{{ url_for('setup_update_agent', machine_id=m.id) }}" class="sshForm">
            <div class="hint">Re-copies the current system_monitor_agent.py to this machine and restarts it. Credentials are used once and never saved.</div>

            <label>SSH Port</label>
            <input type="text" name="ssh_port" placeholder="22" value="22">

            <label>SSH Username</label>
            <input type="text" name="ssh_username" placeholder="pi">

            <label>Auth Method</label>
            <select name="auth_method" class="authMethodSelect" onchange="toggleAuthFieldsIn(this)">
              <option value="key">SSH Key</option>
              <option value="password">Password</option>
            </select>

            <div class="keyFields">
              <label>Private Key Path (on this dashboard machine)</label>
              <input type="text" name="key_path" placeholder="/home/you/.ssh/id_rsa">
              <label>Key Passphrase (if any)</label>
              <input type="password" name="key_passphrase">
            </div>

            <div class="passwordFields" style="display: none;">
              <label>SSH Password</label>
              <input type="password" name="ssh_password">
            </div>

            <label style="display: inline-flex; align-items: center; gap: 6px; text-transform: none; font-size: 13px; color: #ddd; margin-top: 12px;">
              <input type="checkbox" name="install_service" style="width: auto;">
              Also (re)install as a systemd service (auto-start on reboot -- needs sudo)
            </label>
            <label>Sudo Password (only if the box above is checked; leave blank if passwordless sudo)</label>
            <input type="password" name="sudo_password">

            <div class="submitRow">
              <input type="submit" value="Update Agent">
            </div>
          </form>
        </details>
      </td>
    </tr>
    {% endif %}
    {% endfor %}
  </table>

  <form class="addForm" method="post" action="{{ url_for('setup_add') }}">
    <label>Label</label>
    <input type="text" name="label" placeholder="e.g. Graywolf" required>

    <label>Description (optional)</label>
    <input type="text" name="description" placeholder="e.g. APRS iGate in the shack">

    <label>Machine ID (optional)</label>
    <input type="text" name="id" placeholder="auto-generated from label">
    <div class="hint">Lowercase letters/numbers only; used in URLs. Leave blank to auto-generate.</div>

    <label>Type</label>
    <select name="type" id="typeSelect" onchange="toggleRemoteFields()">
      <option value="remote">Remote (runs system_monitor_agent.py)</option>
      <option value="local">Local (this machine)</option>
    </select>

    <div id="remoteFields">
      <label>Host / IP</label>
      <input type="text" name="host" placeholder="192.168.1.50">

      <label>Port (agent listens here)</label>
      <input type="text" name="port" placeholder="5006" value="5006">

      <div style="margin-top: 14px; border-top: 1px solid #444; padding-top: 10px;">
        <label style="display: inline-flex; align-items: center; gap: 6px; text-transform: none; font-size: 13px; color: #ddd;">
          <input type="checkbox" name="auto_install" id="autoInstall" onchange="toggleSSHFields()" style="width: auto;">
          Automatically install &amp; start the agent on this machine (Linux/Pi only, over SSH)
        </label>

        <div id="sshFields" class="sshForm" style="display: none; margin-top: 8px;">
          <div class="hint">Credentials are used once for this deployment and are never saved.</div>

          <label>SSH Port</label>
          <input type="text" name="ssh_port" placeholder="22" value="22">

          <label>SSH Username</label>
          <input type="text" name="ssh_username" placeholder="pi">

          <label>Auth Method</label>
          <select name="auth_method" class="authMethodSelect" onchange="toggleAuthFieldsIn(this)">
            <option value="key">SSH Key</option>
            <option value="password">Password</option>
          </select>

          <div class="keyFields">
            <label>Private Key Path (on this dashboard machine)</label>
            <input type="text" name="key_path" placeholder="/home/you/.ssh/id_rsa">
            <label>Key Passphrase (if any)</label>
            <input type="password" name="key_passphrase">
          </div>

          <div class="passwordFields" style="display: none;">
            <label>SSH Password</label>
            <input type="password" name="ssh_password">
          </div>

          <label style="display: inline-flex; align-items: center; gap: 6px; text-transform: none; font-size: 13px; color: #ddd; margin-top: 12px;">
            <input type="checkbox" name="install_service" style="width: auto;">
            Also install as a systemd service (auto-start on reboot -- needs sudo)
          </label>

          <label>Sudo Password (only if the box above is checked; leave blank if passwordless sudo)</label>
          <input type="password" name="sudo_password">
        </div>
      </div>
    </div>

    <div class="submitRow">
      <input type="submit" value="Add Machine">
    </div>
  </form>

  <script>
    function toggleRemoteFields() {
      const isRemote = document.getElementById('typeSelect').value === 'remote';
      document.getElementById('remoteFields').style.display = isRemote ? 'block' : 'none';
    }
    function toggleSSHFields() {
      document.getElementById('sshFields').style.display =
        document.getElementById('autoInstall').checked ? 'block' : 'none';
    }
    function toggleAuthFieldsIn(selectEl) {
      const scope = selectEl.closest('.sshForm');
      const isKey = selectEl.value === 'key';
      scope.querySelectorAll('.keyFields').forEach(el => el.style.display = isKey ? 'block' : 'none');
      scope.querySelectorAll('.passwordFields').forEach(el => el.style.display = isKey ? 'none' : 'block');
    }
    toggleRemoteFields();
    document.querySelectorAll('.authMethodSelect').forEach(toggleAuthFieldsIn);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=liveness_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)
