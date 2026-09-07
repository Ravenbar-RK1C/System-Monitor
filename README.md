# System Monitor

A browser-based dashboard for watching CPU, RAM, network, disk I/O,
CPU temperature, and power-saving status for one or more machines —
your main box and any number of Raspberry Pis (or other Linux/Windows
machines) on the network. Designed to be viewed full-screen on a
kiosk display, in a regular browser tab, or embedded via `<iframe>`
into something like HamDashBoard.

## Files

| File                        | Role                                                                 |
|-----------------------------|-----------------------------------------------------------------------|
| `system_monitor.py`         | The dashboard itself — a Flask web app you run on one machine.       |
| `system_monitor_agent.py`   | A tiny stats server you run on each *remote* machine you want to watch. |
| `system_monitor_config.json`| Auto-created next to `system_monitor.py` on first run; stores machines + settings. |

The local machine (the one running `system_monitor.py`) doesn't need
the agent — its stats are read directly via `psutil`. Any *other*
machine you want to see needs `system_monitor_agent.py` running on it.

## Requirements

Dashboard machine:
```
pip install flask psutil --break-system-packages
```
`paramiko` is optional, and only needed for the setup page's
"auto-install agent over SSH" feature:
```
pip install paramiko --break-system-packages
```

Each remote machine (just the agent):
```
pip install psutil --break-system-packages
```

## Running it

```
python3 system_monitor.py
```

Then open in a browser:

| URL                                                     | What it shows                                  |
|----------------------------------------------------------|-------------------------------------------------|
| `http://<dashboard-host>:8767/`                          | Full dashboard — nav bar with all machines      |
| `http://<dashboard-host>:8767/?machine=<id>`              | Dashboard, pre-selecting one machine's tab      |
| `http://<dashboard-host>:8767/?machine=<id>&embed=1`      | Just the graph/stats card, no nav bar or title — for dropping into an `<iframe>` |
| `http://<dashboard-host>:8767/setup`                      | Add/remove/edit machines, upload or update the agent |

## Adding a machine

Go to `/setup` and either:

- **Local** — only one allowed, represents the dashboard's own machine.
- **Remote** — give it a label, host/IP, and the agent's port (default
  `5006`). You then need the agent running on that machine, which you
  can do one of two ways:

  1. **Manually** — copy `system_monitor_agent.py` to the target
     machine and run it (see [The agent](#the-agent) below).
  2. **Auto-install (Linux/Pi targets only)** — check "Automatically
     install & start the agent" on the add-machine form and supply
     SSH credentials (key or password). The dashboard will SFTP the
     current `system_monitor_agent.py` over, install `psutil`
     (`--user`, no sudo needed), kill any old copy, and launch it
     detached so it survives the SSH session ending. Optionally check
     "install as a systemd service" to have it auto-start on reboot
     (needs sudo — password or passwordless). SSH/sudo credentials
     are used once for that deployment and are never saved to disk.

Existing remote machines can be updated the same way from `/setup`
("Update agent on \<machine\>") — this re-copies whatever agent file
the dashboard currently has on hand and relaunches it, so it's the
same code path as an initial install.

The "Agent File Used for Deployments" box at the top of `/setup`
shows which copy of `system_monitor_agent.py` the dashboard will push
out, and lets you upload a newer version. Uploading a new version
only changes what *future* installs/updates deploy — it doesn't touch
already-running agents until you click "Update Agent" on them.

## The agent

`system_monitor_agent.py` is a small threaded TCP server. On every
connection it replies with one line and closes:

```
hostname|cpu_percent|ram_percent|net_up_kbs|net_down_kbs|power_saving|cpu_temp|disk_read_kbs|disk_write_kbs
```

- `power_saving` — `1`/`0` if `power-profiles-daemon` is available, else blank.
- `cpu_temp` — °C via `psutil.sensors_temperatures()`, else blank.
- `disk_read_kbs` / `disk_write_kbs` — throughput since the last poll, else blank.

Fields are additive and backward-compatible: older 5-field agent
responses still work fine with the current dashboard.

Run it directly for testing:
```
python3 system_monitor_agent.py
```

Or install it as a systemd service so it starts on boot (a sample
unit file is included in a comment at the bottom of the script):

```ini
[Unit]
Description=System Monitor Stats Agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/system_monitor_agent.py
Restart=on-failure
User=pi

[Install]
WantedBy=multi-user.target
```
```
sudo systemctl enable --now system-monitor-agent.service
```

(The dashboard's SSH auto-install/update feature writes and enables
this same unit for you when you check the "install as a systemd
service" box.)

## Config file

`system_monitor_config.json` sits next to `system_monitor.py` and is
created automatically on first run:

```json
{
    "machines": [
        {"id": "local", "label": "This Machine", "type": "local"},
        {"id": "shackpi", "label": "Graywolf", "type": "remote",
         "host": "192.168.1.50", "port": 5006, "description": "APRS iGate in the shack"}
    ],
    "poll_interval_ms": 1000,
    "history_length": 60
}
```

You normally won't need to hand-edit this — everything it holds is
manageable from `/setup`.

## How polling works

- The **actively viewed** machine (whichever tab is open in a browser
  right now) gets full-stats polling every `poll_interval_ms` (default
  1s), feeding the live graph.
- The **local** machine is always polled at that same fast rate,
  regardless of whether its tab is open, since reading local stats is
  cheap.
- Remote machines that **aren't** currently being viewed are checked
  only every 30 seconds, with a lightweight reachability ping — just
  enough to keep their nav-tab colored green (online) or red
  (offline) without hammering machines nobody's looking at.
- Switching to a remote machine's tab triggers one synchronous fetch
  immediately, so the graph doesn't sit empty waiting for the next
  slow-loop tick.

## Notes

- **Disk I/O** on the dashboard side is summed across physical disks
  only (SD card / HDD / SSD / NVMe) — loop devices, device-mapper
  (LVM/LUKS), zram, ram disks, optical drives, software RAID, and
  individual partitions are excluded, so the numbers reflect actual
  media wear rather than RAM-backed or double-counted I/O.
- **CPU sampling in the agent** uses a dedicated background thread
  sampling once a second into a cached value, rather than calling
  `psutil.cpu_percent()` inside each connection handler. `psutil`
  tracks CPU usage "since last call" *per calling thread*, and since
  the agent hands each connection to a new thread, calling it inline
  would make every request look like a meaningless first-ever call
  (always reading ~0%).
- Static IPs on target machines are recommended over router DHCP
  reservations, which can occasionally hand out a different address
  than expected.
