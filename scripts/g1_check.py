"""Gate G1 verification script for self-hosted Flagsmith.

1. Sends one experiment exposure (`result_view`) and one metric event
   (`shipped_ambiguous`) via the Flagsmith Python SDK.
2. Connects to the local Flagsmith MCP server (`tools/list`), lists tools, and
   attempts to read experiment exposures and results via MCP.
3. Queries Flagsmith Management REST API for the same experiment, exposures,
   and results, printing raw responses.

Configuration is read from environment variables (or `.env` in repo root).
Secrets are never logged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from flagsmith import EventProcessorConfig, Flagsmith


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (does not overwrite existing env vars)."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _parse_mcp_response(resp: httpx.Response) -> dict[str, Any]:
    content_type = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in content_type:
        data_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            return {"raw_sse": text}
        return json.loads(data_lines[-1])
    return resp.json()


class SimpleMcpClient:
    """Minimal Streamable-HTTP MCP client for Gate G1 verification."""

    def __init__(self, url: str, api_key: str) -> None:
        self.url = url
        self.api_key = api_key
        self.session_id: str | None = None
        self._req_id = 0
        self._client = httpx.Client(timeout=20.0)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._req_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        resp = self._client.post(self.url, headers=self._headers(), json=payload)
        if sid := resp.headers.get("mcp-session-id"):
            self.session_id = sid
        resp.raise_for_status()
        return _parse_mcp_response(resp)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        resp = self._client.post(self.url, headers=self._headers(), json=payload)
        if sid := resp.headers.get("mcp-session-id"):
            self.session_id = sid

    def initialize(self) -> dict[str, Any]:
        res = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "verdict-g1-check", "version": "0.1.0"},
            },
        )
        self.notify("notifications/initialized")
        return res

    def close(self) -> None:
        self._client.close()


def send_sdk_exposure_and_event(
    flagsmith_url: str,
    env_key: str,
    feature_name: str,
    event_name: str,
    identifier: str,
) -> None:
    base = flagsmith_url.rstrip("/")
    client = Flagsmith(
        environment_key=env_key,
        api_url=f"{base}/api/v1/",
        enable_events=True,
        event_processor_config=EventProcessorConfig(
            events_api_url=f"{base}/api/",
            max_buffer_items=100,
            flush_interval_seconds=1.0,
        ),
    )
    try:
        flag = client.get_experiment_flag(feature_name, identifier=identifier)
        print(
            "[G1a] SDK get_experiment_flag:",
            json.dumps(
                {
                    "identifier": identifier,
                    "feature_name": feature_name,
                    "enabled": getattr(flag, "enabled", None),
                    "value": getattr(flag, "value", None),
                    "variant": getattr(flag, "variant", None),
                    "reason": getattr(flag, "reason", None),
                    "is_default": getattr(flag, "is_default", None),
                    "experiment": str(getattr(flag, "experiment", None)),
                },
                indent=2,
            ),
        )
        client.track_event(event_name, identifier=identifier)
        print(f"[G1a] SDK track_event sent: event={event_name!r}, identifier={identifier!r}")
        client.flush_events()
    finally:
        client.close(timeout_seconds=5)


def run_mcp_checks(
    mcp_url: str,
    api_key: str,
    env_key: str,
    experiment_id: str | None,
) -> None:
    print(f"\n[G1c] Connecting to MCP: {mcp_url}")
    mcp = SimpleMcpClient(mcp_url, api_key)
    try:
        init_res = mcp.initialize()
        print("[G1c] initialize response:", json.dumps(init_res, indent=2))

        tools_res = mcp.rpc("tools/list", {})
        tools = tools_res.get("result", {}).get("tools", [])
        print(f"[G1c] tools/list count: {len(tools)}")
        for tool in tools:
            props = list(tool.get("inputSchema", {}).get("properties", {}).keys())
            req = tool.get("inputSchema", {}).get("required", [])
            print(f"  - {tool['name']} (required={req}, params={props})")

        list_exp = mcp.rpc(
            "tools/call",
            {"name": "list_experiments", "arguments": {"environment_api_key": env_key}},
        )
        print("\n[G1b-MCP] list_experiments raw:", json.dumps(list_exp, indent=2))

        exp_id = experiment_id
        if not exp_id:
            # Try to extract first experiment id from MCP text content if present
            content = list_exp.get("result", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    try:
                        parsed = json.loads(block["text"])
                        results = parsed.get("results", [])
                        if results:
                            exp_id = str(results[0]["id"])
                            break
                    except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                        continue

        if exp_id:
            exposures = mcp.rpc(
                "tools/call",
                {
                    "name": "get_experiment_exposures",
                    "arguments": {"environment_api_key": env_key, "experiment_id": exp_id},
                },
            )
            print("\n[G1b-MCP] get_experiment_exposures raw:", json.dumps(exposures, indent=2))

            results = mcp.rpc(
                "tools/call",
                {
                    "name": "get_experiment_results",
                    "arguments": {"environment_api_key": env_key, "experiment_id": exp_id},
                },
            )
            print("\n[G1b-MCP] get_experiment_results raw:", json.dumps(results, indent=2))
        else:
            print("\n[G1b-MCP] No experiment_id found yet; skipping exposures/results call.")
    finally:
        mcp.close()


def run_rest_checks(
    flagsmith_url: str,
    api_key: str,
    env_key: str,
    experiment_id: str | None,
    refresh: bool,
) -> None:
    base = flagsmith_url.rstrip("/")
    headers = {"Authorization": f"Api-Key {api_key}"}
    print(f"\n[G1b-REST] Querying {base}/api/v1/environments/<env_key>/experiments/")
    with httpx.Client(timeout=20.0, headers=headers) as client:
        exp_list_resp = client.get(f"{base}/api/v1/environments/{env_key}/experiments/")
        print(f"[G1b-REST] GET experiments status={exp_list_resp.status_code}")
        print(exp_list_resp.text)
        exp_list_resp.raise_for_status()
        data = exp_list_resp.json()
        results = data.get("results", data if isinstance(data, list) else [])

        exp_id = experiment_id or (str(results[0]["id"]) if results else None)
        if not exp_id:
            print("[G1b-REST] No experiments found in this environment.")
            return

        if refresh:
            for endpoint in ("exposures/refresh", "results/refresh"):
                ref_resp = client.post(
                    f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/{endpoint}/"
                )
                print(f"\n[G1b-REST] POST {endpoint} status={ref_resp.status_code}")
                if ref_resp.text:
                    print(ref_resp.text)
            time.sleep(2.0)

        exp_detail = client.get(
            f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/"
        )
        print(f"\n[G1b-REST] GET experiment detail status={exp_detail.status_code}")
        print(exp_detail.text)

        exposures_resp = client.get(
            f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/exposures/"
        )
        print(f"\n[G1b-REST] GET exposures status={exposures_resp.status_code}")
        print(exposures_resp.text)

        results_resp = client.get(
            f"{base}/api/v1/environments/{env_key}/experiments/{exp_id}/results/"
        )
        print(f"\n[G1b-REST] GET results status={results_resp.status_code}")
        print(results_resp.text)


def main() -> int:
    load_dotenv(Path(".env"))

    parser = argparse.ArgumentParser(description="Gate G1 check for self-hosted Flagsmith")
    parser.add_argument("--feature", default="result_view", help="Multivariate flag name")
    parser.add_argument("--event", default="shipped_ambiguous", help="Metric event name")
    parser.add_argument(
        "--identifier",
        default=f"g1-probe-{uuid.uuid4().hex[:8]}",
        help="Test identity key",
    )
    parser.add_argument("--experiment-id", default=os.getenv("FLAGSMITH_EXPERIMENT_ID") or None)
    parser.add_argument("--skip-sdk", action="store_true", help="Skip SDK exposure/event send")
    parser.add_argument("--skip-mcp", action="store_true", help="Skip MCP tools/list & calls")
    parser.add_argument("--skip-rest", action="store_true", help="Skip REST API calls")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Trigger POST .../results/refresh/ before reading REST results",
    )
    args = parser.parse_args()

    flagsmith_url = os.getenv("FLAGSMITH_URL", "http://localhost:8000")
    mcp_url = os.getenv("FLAGSMITH_MCP_URL", "http://localhost:8002/mcp")
    env_key = os.getenv("FLAGSMITH_ENV_KEY", "").strip()
    api_key = os.getenv("FLAGSMITH_API_KEY", "").strip()

    if not args.skip_sdk:
        if not env_key:
            print("ERROR: FLAGSMITH_ENV_KEY is required for SDK check.", file=sys.stderr)
            return 2
        send_sdk_exposure_and_event(
            flagsmith_url=flagsmith_url,
            env_key=env_key,
            feature_name=args.feature,
            event_name=args.event,
            identifier=args.identifier,
        )

    if not args.skip_mcp:
        if not api_key or not env_key:
            print("ERROR: FLAGSMITH_API_KEY and FLAGSMITH_ENV_KEY required for MCP check.", file=sys.stderr)
            return 2
        run_mcp_checks(mcp_url=mcp_url, api_key=api_key, env_key=env_key, experiment_id=args.experiment_id)

    if not args.skip_rest:
        if not api_key or not env_key:
            print("ERROR: FLAGSMITH_API_KEY and FLAGSMITH_ENV_KEY required for REST check.", file=sys.stderr)
            return 2
        run_rest_checks(
            flagsmith_url=flagsmith_url,
            api_key=api_key,
            env_key=env_key,
            experiment_id=args.experiment_id,
            refresh=args.refresh,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
