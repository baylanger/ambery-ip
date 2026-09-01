#!/usr/bin/env python3
"""
Ambery IP-Pn remote power switch client (tested on IP-P2; the same web
control protocol is likely shared by the IP-P4 and IP-P6 variants, since
they appear to run the same firmware family with a different port count --
unconfirmed, but the script supports a configurable port count for this).

Reverse-engineered from the unit's own JavaScript (power_monitor_web.js /
login.csp), since this old firmware has no REST/SNMP API:

  Login:   POST /login_auth.csp   body: auth_user=...&auth_passwd=...
           Response body (plain text):
             "0" = success (session cookie set by the server)
             "1" = bad credentials
             "2" = idle timeout, please re-login
             "3" = too many concurrent sessions

  Status:  GET /power_monitor_frame.csp?srm_no=1&time=<ms>
           Response is a JS array literal, e.g.:
             ["", "", "", "1", "1,0,...", "", ["1","1"], ["0","1"], ["0.00Amp","0.10Amp"]]
           Meaningful indices (per the unit's own load_SRM_status()):
             data[7] -> per-port status codes (as strings)
             data[8] -> per-port amperage strings

           Status codes (from the firmware's C-style comments):
             0 POWER_OFF        1 POWER_ON
             2 POWER_OFF_ACT    3 POWER_ON_ACT      (transitional)
             4 POWER_DISABLE
             5 SHUTDOWN_ERR (*counts as ON)
             6 POWER_RESET_ACT (*counts as OFF_ACT)
             7 POWER_RESET_ERR (*counts as ON)
             8 POWER_ACTIONING
             9 POWER_OVERLOADING

  Toggle:  GET /power_monitor_frame.csp?srm_no=1&power_id=<port>&status=<s>&time=<ms>
             status=1 -> turn ON
             status=2 -> turn OFF
             status=3 -> reboot (power-cycle)

  All ports at once:
           GET /power_monitor_frame.csp?srm_no=1&all_status=<s>&time=<ms>
             all_status=1 -> all ON, all_status=2 -> all OFF

The number of physical outlets varies by model (2 on the IP-P2, per its own
`number_of_ports` JS variable; presumably 4 / 6 on the P4 / P6), even though
the config table internally always reserves 8 slots. This client takes the
port count as a parameter (default 2) rather than hardcoding it.

Usage as a library:
    from ambery_ip import AmberyRemotePower
    dev = AmberyRemotePower("192.168.1.50", "admin", "password", num_ports=2)
    print(dev.get_status())
    dev.set_port(1, True)   # turn port 1 on
    dev.set_port(2, False)  # turn port 2 off

Usage as a CLI (for Home Assistant command_line integration). Username and
password are read ONLY from environment variables -- AMBERY_USER,
AMBERY_PASSWORD -- there are no --user/--password flags, so the password
never appears in `ps` output. Host and port count can come from the same
environment variables (AMBERY_HOST, AMBERY_PORTS) or be overridden per call
with --host/--ports (handy if you have several units sharing one set of
credentials but different IPs):
    export AMBERY_HOST=192.168.1.5
    export AMBERY_USER=admin
    export AMBERY_PASSWORD=your-password
    python3 ambery-ip.py status
    python3 ambery-ip.py status --port 1
    python3 ambery-ip.py on 1
    python3 ambery-ip.py off 2
    python3 ambery-ip.py reboot 1
    python3 ambery-ip.py --host 192.168.1.11 --ports 4 status
"""

import argparse
import ast
import http.cookiejar
import os
import ssl
import sys
import time
import json
import urllib.error
import urllib.parse
import urllib.request

# Status codes that mean "the outlet is effectively energized"
ON_STATES = {1, 3, 5, 7}   # ON, ON_ACT, SHUTDOWN_ERR(*ON), RESET_ERR(*ON)
# Status codes that mean "in the middle of turning on/off/rebooting"
TRANSITIONAL_STATES = {2, 3, 6, 8}

DEFAULT_NUM_PORTS = 2  # IP-P2 is a 2-outlet unit; override for P4/P6


class AmberyError(Exception):
    """Base error for AmberyRemotePower operations."""


class AmberyAuthError(AmberyError):
    """Login failed (bad credentials, timeout, or too many sessions)."""


class AmberyRemotePower:
    def __init__(self, host, username, password, use_https=False,
                 verify_ssl=True, timeout=10, srm_no=1,
                 num_ports=DEFAULT_NUM_PORTS):
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.srm_no = srm_no
        self.num_ports = num_ports
        self._logged_in = False

        self.cookiejar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.cookiejar)]
        if use_https and not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    # -- internal helpers ----------------------------------------------

    @staticmethod
    def _ms_timestamp():
        return int(time.time() * 1000)

    def _parse_status_response(self, text):
        """The device returns a JS array literal, not strict JSON. It only
        ever contains numbers, strings, and nested arrays, so it's also
        valid Python literal syntax -- ast.literal_eval handles it safely
        without executing arbitrary code."""
        text = text.strip()
        if text == "TimeOut":
            raise AmberyAuthError("Session expired (server said 'TimeOut')")
        start = text.find("[")
        if start == -1:
            raise AmberyError(f"Unexpected status response: {text!r}")
        array_text = text[start:]
        try:
            return ast.literal_eval(array_text)
        except (ValueError, SyntaxError) as exc:
            raise AmberyError(
                f"Could not parse status response: {array_text!r}"
            ) from exc

    # -- low-level HTTP (stdlib only, no third-party deps) ---------------

    def _http_get(self, path, params):
        qs = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{qs}"
        req = urllib.request.Request(url, method="GET")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise AmberyError(f"HTTP request to {url} failed: {exc}") from exc

    def _http_post(self, path, params, form_data):
        qs = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{qs}"
        body = urllib.parse.urlencode(form_data).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise AmberyError(f"HTTP request to {url} failed: {exc}") from exc

    # -- auth -------------------------------------------------------------

    def login(self):
        text = self._http_post(
            "/login_auth.csp",
            {"time": self._ms_timestamp()},
            {"auth_user": self.username, "auth_passwd": self.password},
        )
        code = text.strip()
        if code == "0":
            self._logged_in = True
            return True
        errors = {
            "1": "Invalid username or password",
            "2": "Idle session timeout, please retry",
            "3": "Too many concurrent connections to this device",
        }
        self._logged_in = False
        raise AmberyAuthError(errors.get(code, f"Unknown login response: {code!r}"))

    def _ensure_login(self):
        if not self._logged_in:
            self.login()

    def _get(self, params):
        """GET power_monitor_frame.csp with auto re-login on session timeout."""
        self._ensure_login()
        text = self._http_get("/power_monitor_frame.csp", params)
        if text.strip() == "TimeOut":
            self.login()
            text = self._http_get("/power_monitor_frame.csp", params)
        return text

    # -- public API ---------------------------------------------------

    def get_status(self):
        """Return {port_num: {"raw_status": int, "is_on": bool,
        "transitional": bool, "amp": str|None}} for each port
        (1..self.num_ports)."""
        params = {"srm_no": self.srm_no, "time": self._ms_timestamp()}
        text = self._get(params)
        data = self._parse_status_response(text)

        p_status = data[7] if len(data) > 7 else []
        amps = data[8] if len(data) > 8 else []

        ports = {}
        for i in range(self.num_ports):
            port_num = i + 1
            raw = p_status[i] if i < len(p_status) else ""
            raw = str(raw).strip()
            state = int(raw) if raw != "" else None
            ports[port_num] = {
                "raw_status": state,
                "is_on": state in ON_STATES if state is not None else None,
                "transitional": state in TRANSITIONAL_STATES if state is not None else None,
                "amp": amps[i] if i < len(amps) else None,
            }
        return ports

    def set_port(self, port_id, turn_on):
        """turn_on=True -> ON, turn_on=False -> OFF."""
        if port_id not in range(1, self.num_ports + 1):
            raise ValueError(f"port_id must be 1-{self.num_ports}")
        status = 1 if turn_on else 2
        params = {
            "srm_no": self.srm_no,
            "power_id": port_id,
            "status": status,
            "time": self._ms_timestamp(),
        }
        self._get(params)

    def reboot_port(self, port_id):
        if port_id not in range(1, self.num_ports + 1):
            raise ValueError(f"port_id must be 1-{self.num_ports}")
        params = {
            "srm_no": self.srm_no,
            "power_id": port_id,
            "status": 3,
            "time": self._ms_timestamp(),
        }
        self._get(params)

    def set_all(self, turn_on):
        status = 1 if turn_on else 2
        params = {
            "srm_no": self.srm_no,
            "all_status": status,
            "time": self._ms_timestamp(),
        }
        self._get(params)


# -- CLI -----------------------------------------------------------------

def format_status_human(status):
    """Plain-text summary, one line per port, e.g.:
    Port 1: ON  (0.10A)
    Port 2: OFF (0.00A)
    """
    lines = []
    for port_num in sorted(status):
        info = status[port_num]
        if info["is_on"] is None:
            state = "UNKNOWN"
        elif info["transitional"]:
            state = "ON (busy)" if info["is_on"] else "OFF (busy)"
        else:
            state = "ON" if info["is_on"] else "OFF"
        amp = f" ({info['amp']})" if info.get("amp") else ""
        lines.append(f"Port {port_num}: {state}{amp}")
    return "\n".join(lines)


def build_device_from_args(args):
    host = args.host or os.environ.get("AMBERY_HOST")
    user = os.environ.get("AMBERY_USER")
    password = os.environ.get("AMBERY_PASSWORD")
    if not host or not user or password is None:
        print("Missing config: need a host (--host or AMBERY_HOST env var) "
              "plus AMBERY_USER and AMBERY_PASSWORD environment variables. "
              "There are deliberately no --user/--password flags, so the "
              "password never shows up in `ps`.",
              file=sys.stderr)
        sys.exit(2)

    num_ports = args.ports
    if num_ports is None:
        env_ports = os.environ.get("AMBERY_PORTS")
        num_ports = int(env_ports) if env_ports else DEFAULT_NUM_PORTS

    return AmberyRemotePower(host, user, password, use_https=args.https,
                              verify_ssl=not args.no_verify_ssl,
                              num_ports=num_ports)


def main():
    parser = argparse.ArgumentParser(
        description="Ambery IP-Pn remote power switch CLI (IP-P2/P4/P6 "
                     "family). AMBERY_USER and AMBERY_PASSWORD must come "
                     "from the environment -- there are no --user/--password "
                     "flags, so credentials never appear in `ps` output. "
                     "Host defaults to AMBERY_HOST but can be overridden "
                     "with --host (not sensitive, fine as a CLI arg) -- "
                     "useful if you have several units sharing one set of "
                     "credentials but different IPs.")
    parser.add_argument("--host", help="Device IP/hostname; overrides "
                                        "AMBERY_HOST env var if given")
    parser.add_argument("--ports", type=int, help="Number of outlets on "
                                                    "this unit (2 for "
                                                    "IP-P2, presumably 4/6 "
                                                    "for IP-P4/IP-P6); "
                                                    "overrides AMBERY_PORTS "
                                                    "env var, defaults to 2")
    parser.add_argument("--https", action="store_true",
                         help="Use https instead of http")
    parser.add_argument("--no-verify-ssl", action="store_true",
                         help="Skip TLS certificate verification")

    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Print status as JSON "
                                               "(single line by default)")
    p_status.add_argument("--port", type=int,
                           help="Only print this port's is_on (prints "
                                "'ON'/'OFF' and exits 1 on error, for "
                                "HA command_line sensor use)")
    p_status.add_argument("--pretty", action="store_true",
                           help="Print indented, multi-line JSON instead "
                                "of one line")
    p_status.add_argument("--human", action="store_true",
                           help="Print a plain-text summary instead of "
                                "JSON")

    p_on = sub.add_parser("on", help="Turn a port on")
    p_on.add_argument("port", type=int)

    p_off = sub.add_parser("off", help="Turn a port off")
    p_off.add_argument("port", type=int)

    p_reboot = sub.add_parser("reboot", help="Power-cycle a port")
    p_reboot.add_argument("port", type=int)

    sub.add_parser("all-on", help="Turn all ports on")
    sub.add_parser("all-off", help="Turn all ports off")

    args = parser.parse_args()
    dev = build_device_from_args(args)

    try:
        if args.command == "status":
            status = dev.get_status()
            if args.port:
                port = status[args.port]
                if port["is_on"] is None:
                    print("UNKNOWN")
                    sys.exit(1)
                print("ON" if port["is_on"] else "OFF")
            elif args.human:
                print(format_status_human(status))
            elif args.pretty:
                print(json.dumps(status, indent=2, sort_keys=True))
            else:
                print(json.dumps(status))
        elif args.command == "on":
            dev.set_port(args.port, True)
            print("OK")
        elif args.command == "off":
            dev.set_port(args.port, False)
            print("OK")
        elif args.command == "reboot":
            dev.reboot_port(args.port)
            print("OK")
        elif args.command == "all-on":
            dev.set_all(True)
            print("OK")
        elif args.command == "all-off":
            dev.set_all(False)
            print("OK")
    except (AmberyError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
