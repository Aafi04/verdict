"""FastAPI + Jinja2 quiz application (Track 1 experiment surface)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from quiz.config import QuizSettings
from quiz.db import Arm, Choice, QuizDatabase
from quiz.flagsmith_sdk import FlagsmithQuizSdk, NoopQuizSdk, QuizSdk
from quiz.security import (
    EphemeralRateLimiter,
    new_visitor_id,
    sign_visitor_id,
    verify_visitor_cookie,
)
from verdict.memo import memo_from_scenario

COOKIE_NAME = "vid"
PRIVACY_NOTICE = (
    "Privacy notice: This quiz uses a single signed random cookie ID to keep your "
    "experiment view consistent. No name, email, IP address, or fingerprint is stored."
)
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _load_scenarios(scenarios_dir: Path) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for sid in ("s1", "s2"):
        path = scenarios_dir / f"{sid}.json"
        loaded[sid] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


_TRUSTED_PROXIES = {"127.0.0.1", "::1", "testclient"}


def _is_trusted_proxy(request: Request) -> bool:
    return bool(request.client and request.client.host in _TRUSTED_PROXIES)


def _client_ip_for_rate_limit(request: Request) -> str | None:
    """Extract client IP solely for ephemeral salted hashing in RAM (never stored)."""
    if _is_trusted_proxy(request):
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    if _is_trusted_proxy(request):
        if request.headers.get("x-forwarded-proto", "").strip().lower() == "https":
            return True
        if '"https"' in request.headers.get("cf-visitor", "").lower():
            return True
    return False


def _get_or_issue_vid(
    request: Request, settings: QuizSettings
) -> tuple[str, str | None]:
    """Return (vid, cookie_value_to_set_or_None)."""
    existing = verify_visitor_cookie(
        request.cookies.get(COOKIE_NAME),
        settings.cookie_secret,
        allow_sim_prefix=settings.simulated_mode,
    )
    if existing:
        return existing, None
    vid = new_visitor_id(simulated=settings.simulated_mode)
    return vid, sign_visitor_id(vid, settings.cookie_secret)


def _set_vid_cookie(request: Request, response: Response, signed_val: str | None) -> None:
    if signed_val is None:
        return
    response.set_cookie(
        key=COOKIE_NAME,
        value=signed_val,
        secure=_is_https_request(request),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )


def create_app(
    settings: QuizSettings | None = None,
    sdk: QuizSdk | None = None,
) -> FastAPI:
    """Build and configure the FastAPI quiz application."""
    cfg = settings or QuizSettings.from_env()
    db = QuizDatabase(cfg.db_path)
    scenarios = _load_scenarios(cfg.scenarios_dir)
    rate_limiter = EphemeralRateLimiter(max_requests=cfg.rate_limit_per_min, window_seconds=60.0)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    if sdk is not None:
        quiz_sdk: QuizSdk = sdk
    elif cfg.flagsmith_env_key and cfg.flagsmith_url:
        quiz_sdk = FlagsmithQuizSdk(
            base_url=cfg.flagsmith_url,
            env_key=cfg.flagsmith_env_key,
            flag_name=cfg.flag_name,
        )
    else:
        quiz_sdk = NoopQuizSdk()

    prod_sdk_unavailable = (not cfg.simulated_mode) and isinstance(quiz_sdk, NoopQuizSdk)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            quiz_sdk.close()

    app = FastAPI(title="Verdict Ship-or-Hold Quiz", docs_url=None, redoc_url=None, lifespan=lifespan)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def security_and_rate_limit(request: Request, call_next):
        if request.url.path != "/healthz" and not rate_limiter.allow(_client_ip_for_rate_limit(request)):
            return JSONResponse({"detail": "Too many requests. Please slow down."}, status_code=429)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def _ensure_exposed(vid: str) -> dict[str, Any]:
        existing = db.get_visitor(vid)
        if existing is not None:
            return existing
        arm: Arm = quiz_sdk.evaluate_and_expose(vid)
        row, _ = db.record_exposure_once(vid, arm, simulated=cfg.simulated_mode)
        return row

    def _prod_unavailable_response() -> Response | None:
        if prod_sdk_unavailable:
            return PlainTextResponse(
                "Experiment service unavailable: Flagsmith SDK is not configured.",
                status_code=503,
            )
        return None

    @app.get("/healthz")
    def healthz() -> Response:
        if prod_sdk_unavailable:
            return JSONResponse(
                {"status": "not-ok", "reason": "flagsmith_sdk_unconfigured"},
                status_code=503,
            )
        return JSONResponse({"status": "ok"}, status_code=200)

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        _, cookie_to_set = _get_or_issue_vid(request, cfg)
        resp = templates.TemplateResponse(
            request=request,
            name="landing.html",
            context={"privacy_notice": PRIVACY_NOTICE, "simulated": cfg.simulated_mode},
        )
        _set_vid_cookie(request, resp, cookie_to_set)
        return resp

    @app.get("/s1", response_class=HTMLResponse)
    def scenario_1(request: Request) -> Response:
        if unavail := _prod_unavailable_response():
            return unavail
        vid, cookie_to_set = _get_or_issue_vid(request, cfg)
        visitor = _ensure_exposed(vid)
        scenario = scenarios["s1"]
        memo = memo_from_scenario(scenario)
        resp = templates.TemplateResponse(
            request=request,
            name="scenario.html",
            context={
                "step": 1,
                "total_steps": 2,
                "action_url": "/s1",
                "arm": visitor["arm"],
                "scenario": scenario,
                "memo": memo,
                "already_answered": visitor["s1_choice"],
                "privacy_notice": PRIVACY_NOTICE,
                "simulated": cfg.simulated_mode,
            },
        )
        _set_vid_cookie(request, resp, cookie_to_set)
        return resp

    @app.post("/s1")
    def submit_s1(request: Request, choice: str = Form(...)) -> Response:
        if unavail := _prod_unavailable_response():
            return unavail
        if choice not in ("ship", "hold"):
            return JSONResponse({"detail": "Invalid choice"}, status_code=400)
        vid, cookie_to_set = _get_or_issue_vid(request, cfg)
        _ensure_exposed(vid)
        typed_choice: Choice = choice  # type: ignore[assignment]
        if db.record_choice_once(vid, "s1", typed_choice) and typed_choice == "ship":
            quiz_sdk.track_event("shipped_ambiguous", vid)
        resp = RedirectResponse(url="/s2", status_code=303)
        _set_vid_cookie(request, resp, cookie_to_set)
        return resp

    @app.get("/s2", response_class=HTMLResponse)
    def scenario_2(request: Request) -> Response:
        if unavail := _prod_unavailable_response():
            return unavail
        vid = verify_visitor_cookie(
            request.cookies.get(COOKIE_NAME),
            cfg.cookie_secret,
            allow_sim_prefix=cfg.simulated_mode,
        )
        if not vid:
            _, replacement = _get_or_issue_vid(request, cfg)
            resp = RedirectResponse(url="/s1", status_code=303)
            _set_vid_cookie(request, resp, replacement)
            return resp
        visitor = db.get_visitor(vid)
        if visitor is None:
            return RedirectResponse(url="/s1", status_code=303)
        scenario = scenarios["s2"]
        memo = memo_from_scenario(scenario)
        return templates.TemplateResponse(
            request=request,
            name="scenario.html",
            context={
                "step": 2,
                "total_steps": 2,
                "action_url": "/s2",
                "arm": visitor["arm"],
                "scenario": scenario,
                "memo": memo,
                "already_answered": visitor["s2_choice"],
                "privacy_notice": PRIVACY_NOTICE,
                "simulated": cfg.simulated_mode,
            },
        )

    @app.post("/s2")
    def submit_s2(request: Request, choice: str = Form(...)) -> Response:
        if unavail := _prod_unavailable_response():
            return unavail
        if choice not in ("ship", "hold"):
            return JSONResponse({"detail": "Invalid choice"}, status_code=400)
        vid = verify_visitor_cookie(
            request.cookies.get(COOKIE_NAME),
            cfg.cookie_secret,
            allow_sim_prefix=cfg.simulated_mode,
        )
        if not vid or db.get_visitor(vid) is None:
            _, replacement = _get_or_issue_vid(request, cfg) if not vid else (vid, None)
            resp = RedirectResponse(url="/s1", status_code=303)
            _set_vid_cookie(request, resp, replacement)
            return resp
        typed_choice: Choice = choice  # type: ignore[assignment]
        if db.record_choice_once(vid, "s2", typed_choice) and typed_choice == "hold":
            quiz_sdk.track_event("held_clear_win", vid)
        return RedirectResponse(url="/done", status_code=303)

    @app.get("/done", response_class=HTMLResponse)
    def done(request: Request) -> Response:
        if unavail := _prod_unavailable_response():
            return unavail
        vid = verify_visitor_cookie(
            request.cookies.get(COOKIE_NAME),
            cfg.cookie_secret,
            allow_sim_prefix=cfg.simulated_mode,
        )
        if not vid:
            _, replacement = _get_or_issue_vid(request, cfg)
            resp = RedirectResponse(url="/", status_code=303)
            _set_vid_cookie(request, resp, replacement)
            return resp
        visitor = db.get_visitor(vid)
        if visitor is None or not visitor["s1_choice"]:
            return RedirectResponse(url="/s1", status_code=303)
        if not visitor["s2_choice"]:
            return RedirectResponse(url="/s2", status_code=303)

        db.mark_completed(vid)
        quiz_sdk.flush()
        return templates.TemplateResponse(
            request=request,
            name="done.html",
            context={
                "visitor": visitor,
                "s1": scenarios["s1"],
                "s2": scenarios["s2"],
                "privacy_notice": PRIVACY_NOTICE,
                "simulated": cfg.simulated_mode,
            },
        )

    return app


app = create_app()
