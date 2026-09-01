# ambery-ip

A small, dependency-free Python client for old **Ambery IP-P2** remote power
switches — and likely its siblings, the **IP-P4**, **IP-P4S**, and **IP-P6**
(more/fewer outlets, same firmware family) — which expose no REST or SNMP
API. The only interface these units offer is a password-protected web page
meant for a human, so this script reverse-engineers that page's own
JavaScript to log in and drive the switches programmatically.

It was built specifically to expose the switches to **Home Assistant** via
the `command_line` integration, but it's a plain CLI/Python module, so it
runs anywhere Python 3 is available (NAS, Linux, MacOS, etc).

> **Compatibility note**: confirmed working against an IP-P2. The IP-P4,
> IP-P4S, and IP-P6 use the same web UI and control protocol as far as I've
> seen, but I haven't had one on hand to test — the script supports a
> configurable port count (`--ports` / `AMBERY_PORTS`) for this reason. If
> you try it on one of those models, I'd love to hear whether it worked.

## Why this exists

The IP-P2's web UI is the only way to check or change outlet state: log in
with a username/password, land on a page that shows the two outlets, click
to toggle them. No documented API, no SNMP on this particular unit. This
script logs in, polls status, and toggles outlets the same way the web page
itself does — by replaying the exact HTTP calls its own JavaScript makes.

## How it works

The device's own JavaScript (`power_monitor_web.js` / `login.csp`) spells
out its entire "API":

| Action | Request |
|---|---|
| Login | `POST /login_auth.csp` with `auth_user=...&auth_passwd=...`. Response body: `0` success (session cookie set), `1` bad credentials, `2` idle timeout, `3` too many concurrent sessions. |
| Poll status | `GET /power_monitor_frame.csp?srm_no=1&time=<ms>`. Response is a JS array literal; index `7` holds per-port status codes, index `8` holds per-port amperage. |
| Toggle a port | `GET /power_monitor_frame.csp?srm_no=1&power_id=<n>&status=<s>&time=<ms>` — `status=1` on, `status=2` off, `status=3` reboot. |
| All ports at once | Same endpoint with `all_status=<s>` instead of `power_id`/`status`. |

Per-port status codes (from the firmware's own comments):

```
0 POWER_OFF           1 POWER_ON
2 POWER_OFF_ACT       3 POWER_ON_ACT        (transitional)
4 POWER_DISABLE
5 SHUTDOWN_ERR        (counts as ON)
6 POWER_RESET_ACT     (counts as OFF_ACT)
7 POWER_RESET_ERR     (counts as ON)
8 POWER_ACTIONING
9 POWER_OVERLOADING
```

The status response isn't strict JSON, but since it only ever contains
numbers, strings, and nested arrays, it parses safely with Python's
`ast.literal_eval` — no `eval()`, no third-party JSON-repair hacks.

## Requirements

- Python 3, standard library only (`urllib`, `http.cookiejar`) — no `pip
  install` needed, so it runs unmodified inside containers, on a NAS, on
  Home Assistant OS, or on a regular machine.

## Installation

Clone or copy `ambery-ip.py` anywhere Python 3 can reach it and the switch:

```bash
git clone https://github.com/baylanger/ambery-ip.git
cd ambery-ip
```

## Usage

Credentials are read **only** from environment variables — `AMBERY_USER`
and `AMBERY_PASSWORD` — deliberately not accepted as CLI flags, so a
password never shows up in `ps` output on a shared or multi-user host.
Host and port count can come from environment variables too, or be
overridden per invocation (handy if you manage several units with one set
of credentials):

```bash
export AMBERY_HOST=192.168.1.5
export AMBERY_USER=admin
export AMBERY_PASSWORD=your-password

python3 ambery-ip.py status                   # full status as JSON
python3 ambery-ip.py status --port 1          # prints ON / OFF

python3 ambery-ip.py status --pretty          # indented, multi-line JSON
python3 ambery-ip.py status --port 1 --pretty # prints status as JSON

python3 ambery-ip.py status --human           # plain text, e.g.:
python3 ambery-ip.py status --port 1 --human  # Port 1: ON (0.00)

python3 ambery-ip.py on 1                      # turn port 1 on
python3 ambery-ip.py off 2                     # turn port 2 off
python3 ambery-ip.py reboot 1                  # power-cycle port 1
python3 ambery-ip.py all-on                    # turn every port on
python3 ambery-ip.py all-off                   # turn every port off

# A second unit, same credentials, different Hostname or IP and outlet count:
python3 ambery-ip.py --host 192.168.1.20 --ports 4 status
```

| Flag / env var | Purpose |
|---|---|
| `AMBERY_USER`, `AMBERY_PASSWORD` | Login credentials. Environment-only, no CLI equivalent. |
| `--host` / `AMBERY_HOST` | Device IP or hostname. `--host` wins if both are set. |
| `--ports` / `AMBERY_PORTS` | Outlet count (default `2`). Use `4` or `6` for IP-P4/IP-P4S/IP-P6. |
| `--https` | Talk to the unit over HTTPS instead of HTTP. |
| `--no-verify-ssl` | Skip TLS certificate verification (self-signed certs). |

### As a Python library

```python
from ambery_ip import AmberyRemotePower

dev = AmberyRemotePower("192.168.1.5", "admin", "your-password", num_ports=2)
print(dev.get_status())
dev.set_port(1, True)    # turn port 1 on
dev.set_port(2, False)   # turn port 2 off
dev.reboot_port(1)
```

## Home Assistant integration

The intended use case: expose each outlet as a `switch` entity via the
[`command_line`](https://www.home-assistant.io/integrations/command_line/)
integration. See [`command_line.yaml`](command_line.yaml) in this repo for
a working example.

Recommended layout, keeping credentials out of anything you commit:

```
scripts/
├── ambery-ip.py             # Python script
├── ambery-ip-run.sh         # wrapper
└── ambery-ip-defaults.sh    # holds real host/user/password
```

`ambery-ip-defaults.sh`:
```bash
export AMBERY_HOST="192.168.1.5"
export AMBERY_USER="admin"
export AMBERY_PASSWORD="your-password"
```

`ambery-ip-run.sh`:
```bash
#!/bin/sh
DIR="$(dirname "$0")"
. "$DIR/ambery-ip-defaults.sh"
exec python3 "$DIR/ambery-ip.py" "$@"
```

`command_line.yaml` (included from `configuration.yaml` via
`command_line: !include command_line.yaml`):
```yaml
- switch:
    name: Ambery Port 1 - Google Coral
    unique_id: ambery_port1
    scan_interval: 60
    command_timeout: 10
    command_state: "sh /config/scripts/ambery-ip-run.sh status --port 1"
    command_on: "sh /config/scripts/ambery-ip-run.sh on 1"
    command_off: "sh /config/scripts/ambery-ip-run.sh off 1"
    value_template: "{{ value == 'ON' }}"
    icon: >
      {% if value == 'ON' %} mdi:power-plug
      {% else %} mdi:power-plug-off
      {% endif %}
- switch:
    name: Ambery Port 2
    unique_id: ambery_port2
    scan_interval: 60
    command_timeout: 10
    command_state: "sh /config/scripts/ambery-ip-run.sh status --port 2"
    command_on: "sh /config/scripts/ambery-ip-run.sh on 2"
    command_off: "sh /config/scripts/ambery-ip-run.sh off 2"
    value_template: "{{ value == 'ON' }}"
    icon: >
      {% if value == 'ON' %} mdi:power-plug
      {% else %} mdi:power-plug-off
      {% endif %}
```

Each poll/toggle shells out and logs in fresh (this old firmware has no
token-based auth to reuse), so there's a login round-trip's worth of
latency on every call — fine for a couple of outlets on a LAN with a
30-second `scan_interval`, but worth knowing about if you're tempted to
poll aggressively.

## Security notes

- No password ever passed as a CLI argument, so it won't leak through `ps`,
  shell history expansion, or process listings on shared hosts.
- The device itself only supports plaintext HTTP auth over the LAN (no
  token/session beyond a cookie) — treat it as a LAN-only device behind
  your own network boundary, the same way you would any other legacy IoT
  gadget with no real security model. If you need to access your Ambery
  over Internet, consider adding nginx/caddy w/ SSL fronting your switch.
- Change the device's default password if you haven't already; these units
  commonly ship with `admin` / `admin`.

## Limitations

- Screen-scraping means this is inherently coupled to this exact firmware
  version's HTML/JS. If your unit's firmware differs, the response format
  in `_parse_status_response` may need adjusting.
- No SNMP support in this script even if your unit has it — this project
  exists specifically for units that don't. If you have SNMP available,
  Home Assistant's native SNMP integration is a more standard fit.
- Only tested against an IP-P2 with 2 outlets; IP-P4/IP-P4S/IP-P6 support
  is speculative pending confirmation.

## Contributing

If you run this against an IP-P4, IP-P4S, or IP-P6 and it works (or
doesn't), an issue or PR confirming behavior — and any differences in the
status response format — would be genuinely useful for the next person
finding this repo.

## License

MIT
