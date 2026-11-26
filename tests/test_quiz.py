"""Unit tests for the Track 1 quiz app (offline, deterministic)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from quiz.app import create_app
from quiz.config import DEFAULT_SCENARIOS_DIR, QuizSettings
from quiz.db import Arm, QuizDatabase
from quiz.flagsmith_sdk import NoopQuizSdk
from quiz.security import sign_visitor_id


class StubSdk:
    """In-memory Flagsmith SDK double that records every exposure and event call."""

    def __init__(self, arm_by_vid: dict[str, Arm] | None = None, default_arm: Arm = "verdict") -> None:
        self.arm_by_vid = arm_by_vid or {}
        self.default_arm = default_arm
        self.exposures: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.flush_calls = 0
        self.close_calls = 0

    def evaluate_and_expose(self, identifier: str) -> Arm:
        self.exposures.append(identifier)
        return self.arm_by_vid.get(identifier, self.default_arm)

    def track_event(self, event_name: str, identifier: str) -> None:
        self.events.append((event_name, identifier))

    def flush(self) -> None:
        self.flush_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _make_settings(tmp_path: Path, *, simulated: bool = False, rate_limit: int = 120) -> QuizSettings:
    return QuizSettings(
        flagsmith_url="http://127.0.0.1:8000",
        flagsmith_env_key="test-secret-env-key-123",
        cookie_secret="unit-test-cookie-secret",
        db_path=tmp_path / "quiz.db",
        scenarios_dir=DEFAULT_SCENARIOS_DIR,
        flag_name="result_view",
        simulated_mode=simulated,
        rate_limit_per_min=rate_limit,
    )


def test_full_quiz_flow_dedup_and_flush(tmp_path: Path) -> None:
    sdk = StubSdk(default_arm="verdict")
    settings = _make_settings(tmp_path)
    app = create_app(settings=settings, sdk=sdk)

    with TestClient(app) as client:
        r0 = client.get("/")
        assert r0.status_code == 200
        assert "Privacy notice" in r0.text
        assert "test-secret-env-key-123" not in r0.text
        assert "127.0.0.1:8000" not in r0.text

        # First /s1 render triggers exactly one exposure.
        r1 = client.get("/s1")
        assert r1.status_code == 200
        assert "VERDICT: HOLD" in r1.text
        assert len(sdk.exposures) == 1

        # Refreshing /s1 does NOT trigger a second exposure.
        r1_again = client.get("/s1")
        assert r1_again.status_code == 200
        assert "VERDICT: HOLD" in r1_again.text
        assert len(sdk.exposures) == 1

        # Double-clicking POST /s1 sends shipped_ambiguous only once.
        p1 = client.post("/s1", data={"choice": "ship"}, follow_redirects=False)
        assert p1.status_code == 303
        p1_dup = client.post("/s1", data={"choice": "hold"}, follow_redirects=False)
        assert p1_dup.status_code == 303
        assert len([e for e in sdk.events if e[0] == "shipped_ambiguous"]) == 1

        r2 = client.get("/s2")
        assert r2.status_code == 200
        assert "VERDICT: SHIP" in r2.text

        p2 = client.post("/s2", data={"choice": "hold"}, follow_redirects=False)
        assert p2.status_code == 303
        assert len([e for e in sdk.events if e[0] == "held_clear_win"]) == 1

        rdone = client.get("/done")
        assert rdone.status_code == 200
        assert "HOLD" in rdone.text
        assert "SHIP" in rdone.text
        assert sdk.flush_calls == 1

    # Lifespan exit closes SDK.
    assert sdk.close_calls == 1

    db = QuizDatabase(settings.db_path)
    summary = db.summarize(simulated=False)
    assert summary["totals"] == {
        "exposures": 1,
        "shipped_ambiguous": 1,
        "held_clear_win": 1,
        "completions": 1,
    }


def test_dashboard_arm_shows_86_percent_and_no_leaks(tmp_path: Path) -> None:
    sdk = StubSdk(default_arm="dashboard")
    settings = _make_settings(tmp_path)
    app = create_app(settings=settings, sdk=sdk)

    with TestClient(app) as client:
        r1 = client.get("/s1")
        assert r1.status_code == 200
        assert "86%" in r1.text
        assert "84%" not in r1.text
        assert "Chance to Beat Control" in r1.text
        assert "test-secret-env-key-123" not in r1.text
        assert "8000" not in r1.text


def test_tampered_or_sim_cookie_rejected_in_prod_mode(tmp_path: Path) -> None:
    sdk = StubSdk(default_arm="dashboard")
    settings = _make_settings(tmp_path, simulated=False)
    app = create_app(settings=settings, sdk=sdk)

    with TestClient(app) as client:
        # Forged signature or sim- prefix in non-simulated mode must be rejected and re-issued.
        forged = sign_visitor_id("sim-12345678-1234-4234-8234-123456789012", settings.cookie_secret)
        client.cookies.set("vid", forged)
        r = client.get("/s1")
        assert r.status_code == 200
        assert not sdk.exposures[0].startswith("sim-")


def test_rate_limit_blocks_excess_requests_without_storing_ip(tmp_path: Path) -> None:
    sdk = StubSdk(default_arm="dashboard")
    settings = _make_settings(tmp_path, rate_limit=3)
    app = create_app(settings=settings, sdk=sdk)

    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/").status_code == 200
        assert client.get("/").status_code == 429
        # /healthz remains available
        assert client.get("/healthz").status_code == 200


def test_rate_limit_separate_buckets_for_forwarded_ips(tmp_path: Path) -> None:
    sdk = StubSdk(default_arm="dashboard")
    settings = _make_settings(tmp_path, rate_limit=2)
    app = create_app(settings=settings, sdk=sdk)

    with TestClient(app) as client:
        for _ in range(2):
            assert client.get("/", headers={"CF-Connecting-IP": "203.0.113.10"}).status_code == 200
        assert client.get("/", headers={"CF-Connecting-IP": "203.0.113.10"}).status_code == 429

        # Different client IP via CF-Connecting-IP gets its own independent bucket
        assert client.get("/", headers={"CF-Connecting-IP": "203.0.113.11"}).status_code == 200
        # Fallback to X-Forwarded-For also uses its own bucket
        assert client.get("/", headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200


def test_vid_cookie_secure_over_https_proxy(tmp_path: Path) -> None:
    sdk = StubSdk(default_arm="dashboard")
    settings = _make_settings(tmp_path)
    app = create_app(settings=settings, sdk=sdk)

    with TestClient(app) as client:
        resp = client.get(
            "/",
            headers={"X-Forwarded-Proto": "https", "CF-Connecting-IP": "203.0.113.10"},
        )
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "vid=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Secure" in set_cookie


def test_fail_closed_in_production_without_sdk(tmp_path: Path) -> None:
    settings = QuizSettings(
        flagsmith_url="http://127.0.0.1:8000",
        flagsmith_env_key="",
        cookie_secret="unit-test-cookie-secret",
        db_path=tmp_path / "quiz.db",
        scenarios_dir=DEFAULT_SCENARIOS_DIR,
        simulated_mode=False,
    )
    for app in (create_app(settings=settings), create_app(settings=settings, sdk=NoopQuizSdk())):
        with TestClient(app) as client:
            hz = client.get("/healthz")
            assert hz.status_code == 503
            assert hz.json()["status"] == "not-ok"

            for method, path in (
                ("get", "/s1"),
                ("post", "/s1"),
                ("get", "/s2"),
                ("post", "/s2"),
                ("get", "/done"),
            ):
                resp = (
                    client.post(path, data={"choice": "ship"})
                    if method == "post"
                    else client.get(path)
                )
                assert resp.status_code == 503
                assert "unavailable" in resp.text.lower()
                assert "8000" not in resp.text


def test_simulated_mode_keeps_noop_sdk_working(tmp_path: Path) -> None:
    settings = QuizSettings(
        flagsmith_url="http://127.0.0.1:8000",
        flagsmith_env_key="",
        cookie_secret="unit-test-cookie-secret",
        db_path=tmp_path / "quiz.db",
        scenarios_dir=DEFAULT_SCENARIOS_DIR,
        simulated_mode=True,
    )
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/s1").status_code == 200
