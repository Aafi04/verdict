"""Read-only status checker for the Verdict production stack.

Checks 7 layers in order and stops at the first FAIL.  Prints one line per
check (PASS / FAIL / SKIP) then a compact summary table.  Exit 0 = all OK,
exit 1 = at least one FAIL.

Usage:
    python scripts/status.py [--tunnel-url <url>]

The --tunnel-url flag overrides the auto-detected Cloudflare Quick Tunnel URL
(useful when the journal is unavailable but you already know the current URL).
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SSH_USER_HOST = "aafi04@aafi-server.local"
GITHUB_PAGES_URL = "https://aafi04.github.io/verdict-quiz/"
HEALTH_CHECK_CMD = "sudo -n /usr/local/bin/verdict-health-check"
SSH_TIMEOUT = 8  # seconds for subprocess timeout
HTTP_TIMEOUT = 10  # seconds for urllib

REQUIRED_SERVICES = [
    "docker",
    "verdict-quiz.service",
    "verdict-tunnel.service",
    "verdict-health.timer",
]

LAYER_NAMES = [
    "SSH reachable",
    "Linux services + docker + battery + disk",
    "Local healthz (quiz on 127.0.0.1:3000)",
    "Tunnel URL extraction",
    "Public healthz via tunnel",
    "Redirect stub (GitHub Pages)",
    "Integrity check",
]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
class Result(NamedTuple):
    layer: int
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str


results: list[Result] = []


def emit(layer: int, status: str, detail: str = "") -> None:
    name = LAYER_NAMES[layer - 1]
    tag = f"[{status:4s}]"
    line = f"  {tag}  Layer {layer}: {name}"
    if detail:
        line += f"  —  {detail}"
    print(line)
    results.append(Result(layer, name, status, detail))


def fail_block(layer: int, what: str, agent_prompt: str, manual: str = "") -> None:
    """Print structured failure block after the table."""
    name = LAYER_NAMES[layer - 1]
    print()
    print("=" * 70)
    print(f"FAILED AT:              Layer {layer} — {name}")
    print(f"WHAT IS WRONG:          {what}")
    print(f"PROMPT FOR THE CODING AGENT:\n  {agent_prompt}")
    if manual:
        print(f"MANUAL FIX (physical):  {manual}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Layer 1: SSH reachable
# ---------------------------------------------------------------------------
def _detect_vpn() -> str:
    """Return a short VPN/offline hint by inspecting local network adapters."""
    try:
        out = subprocess.run(
            ["ipconfig"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return ""
    # Look for virtual/VPN adapter keywords
    for keyword in ("vpn", "tap-", "tun", "nordvpn", "expressvpn", "openvpn", "wireguard"):
        if keyword in out:
            return "VPN adapter detected — disable VPN and retry"
    return ""


def _dns_resolves(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        return True
    except socket.gaierror:
        return False


def layer1_ssh() -> bool:
    hostname = SSH_USER_HOST.split("@")[1]
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             SSH_USER_HOST, "true"],
            check=False,
            capture_output=True,
            timeout=SSH_TIMEOUT,
        )
        if r.returncode == 0:
            emit(1, "PASS")
            return True
    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        emit(1, "FAIL", "ssh binary not found on PATH")
        return False

    # Diagnose
    vpn_hint = _detect_vpn()
    if vpn_hint:
        reason = vpn_hint
    elif not _dns_resolves(hostname):
        reason = f"hostname '{hostname}' does not resolve — check mDNS / Bonjour"
    else:
        reason = "SSH timeout or connection refused — host may be offline"

    emit(1, "FAIL", reason)
    fail_block(
        1,
        f"SSH to {SSH_USER_HOST} failed: {reason}",
        (
            "Diagnose Layer 1 SSH failure (read-only first, ask before restarting "
            "anything; never restart verdict-tunnel.service unless it is actually inactive). "
            f"Reason: {reason}"
        ),
        "If host is physically off, power it on; if on VPN, disable it.",
    )
    return False


# ---------------------------------------------------------------------------
# Layer 2: Linux services + docker + battery + disk
# ---------------------------------------------------------------------------
_L2_SCRIPT = r"""
set -e
echo '===SERVICES==='
systemctl is-active docker verdict-quiz.service verdict-tunnel.service verdict-health.timer
echo '===DOCKER==='
ids=$(docker ps -q 2>/dev/null)
if [ -n "$ids" ]; then
  docker inspect --format '{{.Name}} {{.State.Health.Status}}' $ids 2>/dev/null || true
else
  echo 'NO_CONTAINERS'
fi
echo '===BATTERY==='
upower -i /org/freedesktop/UPower/devices/battery_BAT0 2>/dev/null \
  | grep -E 'state|percentage|online' || echo 'BATTERY_UNAVAILABLE'
echo '===DISK==='
df -h /home | tail -n 1
echo '===MEM==='
free -m | grep Mem
"""


def layer2_linux_services() -> bool:
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             SSH_USER_HOST, _L2_SCRIPT],
            check=False,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT + 10,
        )
    except subprocess.TimeoutExpired:
        emit(2, "FAIL", "SSH layer-2 script timed out")
        return False

    out = r.stdout
    sections: dict[str, list[str]] = {}
    current = ""
    for line in out.splitlines():
        if line.startswith("===") and line.endswith("==="):
            current = line.strip("=").strip()
            sections[current] = []
        elif current and line.strip():
            sections[current].append(line.strip())

    issues: list[str] = []

    # Services
    svc_lines = sections.get("SERVICES", [])
    for svc_name, state in zip(REQUIRED_SERVICES, svc_lines):
        if state != "active":
            issues.append(f"{svc_name}={state}")

    # Docker containers — flag "unhealthy"
    for dline in sections.get("DOCKER", []):
        if "unhealthy" in dline:
            issues.append(f"docker container unhealthy: {dline}")

    if issues:
        detail = "; ".join(issues)
        emit(2, "FAIL", detail)
        fail_block(
            2,
            detail,
            (
                "Diagnose Layer 2 service/docker failure read-only first and ask before "
                "restarting anything; never restart verdict-tunnel.service unless it is "
                f"actually inactive. Failing: {detail}"
            ),
        )
        return False

    # Battery + disk — informational (not a FAIL, just reported)
    batt_lines = sections.get("BATTERY", [])
    disk_lines = sections.get("DISK", [])
    mem_lines = sections.get("MEM", [])
    info_parts: list[str] = []
    for bl in batt_lines:
        if "BATTERY_UNAVAILABLE" not in bl:
            info_parts.append(bl.strip())
    if disk_lines:
        info_parts.append(f"disk={disk_lines[0]}")
    if mem_lines:
        info_parts.append(f"mem={mem_lines[0]}")

    emit(2, "PASS", " | ".join(info_parts) if info_parts else "")
    return True


# ---------------------------------------------------------------------------
# Layer 3: local healthz on Linux via SSH
# ---------------------------------------------------------------------------
def layer3_local_healthz() -> bool:
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             SSH_USER_HOST,
             "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/healthz"],
            check=False,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT,
        )
        code = r.stdout.strip().strip("'")
    except subprocess.TimeoutExpired:
        emit(3, "FAIL", "SSH timed out")
        return False

    if code == "200":
        emit(3, "PASS", "HTTP 200")
        return True

    emit(3, "FAIL", f"HTTP {code or 'no response'}")
    fail_block(
        3,
        f"Quiz /healthz returned {code or 'no response'} on 127.0.0.1:3000",
        (
            "Diagnose Layer 3 local healthz failure on aafi-server.local read-only first "
            "and ask before restarting anything; never restart verdict-tunnel.service unless "
            "it is actually inactive. Only verdict-quiz.service may be restarted."
        ),
    )
    return False


# ---------------------------------------------------------------------------
# Layer 4: extract tunnel URL from journal
# ---------------------------------------------------------------------------
def layer4_tunnel_url(override: str | None) -> str | None:
    if override:
        emit(4, "PASS", f"URL supplied via --tunnel-url: {override}")
        return override

    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             SSH_USER_HOST,
             ("journalctl -u verdict-tunnel.service -b --no-pager -n 80 "
              "| grep -oE 'https://[a-z0-9-]+\\.trycloudflare\\.com' | tail -n 1")],
            check=False,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT,
        )
        url = r.stdout.strip()
    except subprocess.TimeoutExpired:
        emit(4, "FAIL", "SSH timed out reading journal")
        return None

    if url.startswith("https://") and "trycloudflare.com" in url:
        emit(4, "PASS", url)
        return url

    emit(4, "SKIP", "No trycloudflare.com URL found in current boot journal")
    return None


# ---------------------------------------------------------------------------
# Layer 5: public healthz via tunnel
# ---------------------------------------------------------------------------
def layer5_public_healthz(tunnel_url: str | None) -> bool:
    if tunnel_url is None:
        emit(5, "SKIP", "No tunnel URL (layer 4 skipped)")
        return True  # not a hard failure — keep going to stub check

    url = f"{tunnel_url.rstrip('/')}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError) as exc:
        emit(5, "FAIL", str(exc))
        fail_block(
            5,
            f"Public healthz unreachable: {exc}",
            (
                "Diagnose Layer 5 public tunnel healthz failure read-only first; "
                "never restart verdict-tunnel.service unless it is actually inactive."
            ),
        )
        return False

    if code == 200:
        emit(5, "PASS", f"HTTP 200 from {tunnel_url}")
        return True

    emit(5, "FAIL", f"HTTP {code}")
    fail_block(
        5,
        f"Tunnel healthz returned HTTP {code}",
        (
            "Diagnose Layer 5 public tunnel healthz failure. HTTP "
            f"{code} from {url}. Read-only first; never restart verdict-tunnel.service "
            "unless it is actually inactive."
        ),
    )
    return False


# ---------------------------------------------------------------------------
# Layer 6: redirect stub (GitHub Pages)
# ---------------------------------------------------------------------------
def layer6_redirect_stub(tunnel_url: str | None) -> bool:
    try:
        with urllib.request.urlopen(GITHUB_PAGES_URL, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
        body = ""
    except (urllib.error.URLError, OSError) as exc:
        emit(6, "FAIL", f"GitHub Pages unreachable: {exc}")
        fail_block(
            6,
            f"GitHub Pages URL unreachable: {exc}",
            (
                f"Diagnose Layer 6: {GITHUB_PAGES_URL} is unreachable. "
                "Check the Aafi04/verdict-quiz repo GitHub Pages settings."
            ),
        )
        return False

    if code != 200:
        emit(6, "FAIL", f"HTTP {code}")
        fail_block(
            6,
            f"GitHub Pages returned HTTP {code}",
            f"Diagnose Layer 6: {GITHUB_PAGES_URL} returned {code}.",
        )
        return False

    if tunnel_url and tunnel_url not in body:
        emit(6, "FAIL", "REDIRECT STALE — body does not contain tunnel hostname")
        fail_block(
            6,
            "REDIRECT STALE: redirect-stub/index.html still points to old URL",
            (
                f"Update redirect-stub/index.html to point to {tunnel_url}, "
                "commit, push to Aafi04/verdict-quiz main, and wait for Pages rebuild. "
                "Read-only diagnosis first; never restart verdict-tunnel.service."
            ),
            f"Manually update index.html with the new URL: {tunnel_url}",
        )
        return False

    suffix = " and contains tunnel hostname" if tunnel_url else ""
    emit(6, "PASS", "GitHub Pages reachable" + suffix)
    return True


# ---------------------------------------------------------------------------
# Layer 7: integrity check via SSH sudo
# ---------------------------------------------------------------------------
def layer7_integrity() -> bool:
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             SSH_USER_HOST, HEALTH_CHECK_CMD],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = r.stdout.strip()
    except subprocess.TimeoutExpired:
        emit(7, "FAIL", "SSH timed out waiting for health-check")
        return False

    # Parse JSON — health check outputs JSON (may have preamble lines)
    data: dict = {}
    for i, line in enumerate(raw.splitlines()):
        if line.startswith("{"):
            try:
                data = json.loads("\n".join(raw.splitlines()[i:]))
                break
            except json.JSONDecodeError:
                continue

    if not data:
        emit(7, "FAIL", f"Could not parse health-check JSON: {raw[:120]!r}")
        return False

    status = data.get("integrity_status", "UNKNOWN")

    # Safe subset to display — never print conversion rates, lifts, intervals
    fs = data.get("flagsmith_exposures", {})
    sq = data.get("sqlite_exposures", {})
    cd = data.get("count_diff", {})

    safe_summary = (
        f"integrity={status} | "
        f"flagsmith_total={fs.get('total', '?')} | "
        f"sqlite_total={sq.get('total', '?')} | "
        f"count_diff_total={cd.get('total', '?')} | "
        f"tunnel={data.get('tunnel_hostname', '?')}"
    )

    if status == "OK":
        emit(7, "PASS", safe_summary)
        return True

    if status == "STALE_SNAPSHOT":
        as_of = data.get("as_of", "?")
        emit(7, "SKIP", f"STALE_SNAPSHOT (as_of={as_of}) — wait 5 min and re-run")
        return True  # not a hard failure

    emit(7, "FAIL", safe_summary)
    fail_block(
        7,
        f"Integrity check failed: {status}",
        (
            f"Diagnose Layer 7 integrity failure on aafi-server.local. "
            f"integrity_status={status}, count_diff={cd}. "
            "Read-only diagnosis first; ask before restarting anything; "
            "never restart verdict-tunnel.service unless it is actually inactive."
        ),
    )
    return False


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
def print_summary() -> None:
    print()
    print("─" * 70)
    print(f"  {'Layer':<7} {'Status':<6}  Name")
    print("─" * 70)
    for r in results:
        print(f"  {r.layer:<7} {r.status:<6}  {r.name}")
    print("─" * 70)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tunnel-url",
        default=None,
        help="Override auto-detected Cloudflare tunnel URL",
    )
    args = parser.parse_args()

    print()
    print("Verdict Stack Status Check")
    print("─" * 70)

    if not layer1_ssh():
        print_summary()
        return 1

    if not layer2_linux_services():
        print_summary()
        return 1

    if not layer3_local_healthz():
        print_summary()
        return 1

    tunnel_url = layer4_tunnel_url(args.tunnel_url)
    # Layer 4 SKIP (no URL in journal) is not a hard stop — continue to stub/integrity

    if not layer5_public_healthz(tunnel_url):
        print_summary()
        return 1

    if not layer6_redirect_stub(tunnel_url):
        print_summary()
        return 1

    if not layer7_integrity():
        print_summary()
        return 1

    print_summary()
    any_fail = any(r.status == "FAIL" for r in results)
    if any_fail:
        return 1

    print()
    print("  ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
