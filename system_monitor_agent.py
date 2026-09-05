#!/usr/bin/env python3
"""
system_monitor_agent.py
------------------------
Tiny TCP stats server to run on any machine you want the System Monitor
app to be able to display remotely (e.g. a Raspberry Pi on your network).

It listens on a TCP port and, on every connection, replies with a single
line of pipe-separated stats and closes the connection:

    hostname|cpu_percent|ram_percent|net_up_kbs|net_down_kbs\n

Run it directly for testing:
    python3 system_monitor_agent.py

Or install it as a systemd service so it starts on boot, the same way
media_key_driver.py runs on katniss -- see the bottom of this file for
a sample unit.

Requires: psutil  (pip install psutil --break-system-packages)
"""

import socket
import socketserver
import threading
import time

try:
    import psutil
except ImportError:
    raise SystemExit(
        "psutil is required. Install with: pip install psutil --break-system-packages"
    )

AGENT_HOST = "0.0.0.0"
AGENT_PORT = 5006

_lock = threading.Lock()
_last_counters = None
_last_time = None


def get_stats_line():
    global _last_counters, _last_time

    hostname = socket.gethostname()
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent

    now = time.monotonic()
    counters = psutil.net_io_counters()

    with _lock:
        if _last_counters is not None and _last_time is not None:
            elapsed = max(now - _last_time, 0.001)
            up_kbs = max((counters.bytes_sent - _last_counters.bytes_sent) / 1024 / elapsed, 0)
            down_kbs = max((counters.bytes_recv - _last_counters.bytes_recv) / 1024 / elapsed, 0)
        else:
            up_kbs = 0.0
            down_kbs = 0.0
        _last_counters = counters
        _last_time = now

    return f"{hostname}|{cpu:.1f}|{ram:.1f}|{up_kbs:.2f}|{down_kbs:.2f}\n"


class StatsHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(2)
            # We don't actually care what the client sends -- any
            # connection is treated as a request for the current stats.
            try:
                self.request.recv(64)
            except Exception:
                pass
            line = get_stats_line()
            self.request.sendall(line.encode("utf-8"))
        except Exception as err:
            print(f"[AGENT ERROR] {err}")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    # Prime psutil's internal CPU sample so the first real reading is meaningful.
    psutil.cpu_percent(interval=None)
    psutil.net_io_counters()

    server = ThreadedTCPServer((AGENT_HOST, AGENT_PORT), StatsHandler)
    print(f"[AGENT] system_monitor_agent listening on {AGENT_HOST}:{AGENT_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[AGENT] Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()


# ------------------------------------------------------------------
# Sample systemd unit (save as /etc/systemd/system/system-monitor-agent.service):
#
# [Unit]
# Description=System Monitor Stats Agent
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# ExecStart=/usr/bin/python3 /home/pi/system_monitor_agent.py
# Restart=on-failure
# User=pi
#
# [Install]
# WantedBy=multi-user.target
#
# Then: sudo systemctl enable --now system-monitor-agent.service
# ------------------------------------------------------------------
