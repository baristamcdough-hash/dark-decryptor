"""PoriotCloud Vault — FastAPI server.

Serves:
  /              landing page with drag-&-drop decode (animated progress)
  /api/decode    web upload → decode → vault link
  /api/vault     Telegram bot upload (X-Vault-Token) → vault link
  /v/<id>        the vault page: signed JSON + copy + auto-destroy countdown
  /admin         dashboard: stats, vaults, Adsterra ad manager

The Telegram bot is co-hosted in this same process (lifespan) when BOT_TOKEN
is set — perfect for a single Railway hobby service.

Env vars:
  VAULT_API_TOKEN      shared secret for bot uploads (required)
  VAULT_ADMIN_PASSWORD admin login password (required for /admin)
  VAULT_SECRET         session signing secret (optional, random per boot)
  VAULT_PUBLIC_URL     public base URL, e.g. https://vault.poriot.ke (optional)
  VAULT_TTL_HOURS      vault lifetime in hours (default 6)
  BOT_TOKEN            Telegram bot token → starts the bot alongside the web
"""
import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (FastAPI, File, Form, Header, HTTPException, Request,
                     UploadFile)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware

import decoder
import storage

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["fmt_ts"] = lambda ts: time.strftime(
    "%Y-%m-%d %H:%M", time.gmtime(float(ts))
)
templates.env.globals["icon"] = lambda name, cls="": (
    f'<svg class="ic {cls}" aria-hidden="true"><use href="#i-{name}"/></svg>'
)
log = logging.getLogger("poriotcloud")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


SECRET = env("VAULT_SECRET") or secrets.token_hex(32)
ADMIN_SESSION = URLSafeTimedSerializer(SECRET, salt="poriot-admin")
ADMIN_COOKIE = "poriot_admin"
ADMIN_MAX_AGE = 7 * 24 * 3600

# When the vault is served on a custom HTTPS domain, enforce it at the app
# level too (Railway's proxy terminates TLS and sets X-Forwarded-Proto).
HTTPS_MODE = env("VAULT_PUBLIC_URL").strip().startswith("https://") or env("FORCE_HTTPS") == "1"


def _forwarded_proto(request: Request) -> str:
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip()


def _is_https(request: Request) -> bool:
    return _forwarded_proto(request) == "https"


class HttpsMiddleware(BaseHTTPMiddleware):
    """Redirect http → https and send HSTS, but only when we're behind a
    TLS-terminating proxy (X-Forwarded-Proto present) — local dev and tests
    keep working over plain http."""

    async def dispatch(self, request: Request, call_next):
        proto = _forwarded_proto(request)
        if HTTPS_MODE and proto == "http":
            return RedirectResponse(
                str(request.url.replace(scheme="https")), status_code=301
            )
        response = await call_next(request)
        if HTTPS_MODE and proto == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


# --------------------------------------------------------------------------
# Lifecycle: cleanup loop + optional Telegram bot co-host
# --------------------------------------------------------------------------

async def _cleanup_loop():
    while True:
        try:
            n = await asyncio.to_thread(storage.cleanup_expired)
            if n:
                log.info("cleanup: destroyed %d expired vault(s)", n)
        except Exception:
            log.exception("cleanup failed")
        await asyncio.sleep(300)


async def _start_bot():
    """Run the Telegram bot (non-blocking) inside the FastAPI process."""
    import bot  # local import keeps server importable without telegram libs

    try:
        await bot.run_bot_async()
        log.info("Telegram bot started")
    except Exception:
        log.exception("Telegram bot failed to start — web only")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_cleanup_loop())]
    if env("BOT_TOKEN"):
        tasks.append(asyncio.create_task(_start_bot()))
    log.info("PoriotCloud Vault started (TTL=%sh)", env("VAULT_TTL_HOURS", "6"))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="PoriotCloud Vault", lifespan=lifespan)
app.add_middleware(HttpsMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def public_base(request: Request) -> str:
    """Public base URL for vault links. Always ends up with a scheme —
    a bare domain like 'vault.poriot.ke' becomes https://vault.poriot.ke."""
    custom = env("VAULT_PUBLIC_URL").strip().rstrip("/")
    if custom:
        if not custom.startswith(("http://", "https://")):
            custom = "https://" + custom
        return custom
    return str(request.base_url).rstrip("/")


def vault_url(request: Request, vid: str) -> str:
    return f"{public_base(request)}/v/{vid}"


def _sign_and_store(config, request=None) -> dict:
    """Sign (credits), stamp the vault link, serialize, store."""
    signed = decoder.sign_result(config)
    name = str(
        (config if isinstance(config, dict) else {}).get("name")
        or (config if isinstance(config, dict) else {}).get("remark")
        or "Config"
    )
    text = json.dumps(signed, indent=2, ensure_ascii=False)
    rec = storage.create_vault(text, name=name)
    if request is not None:
        signed["_vault"] = vault_url(request, rec["id"])
        text = json.dumps(signed, indent=2, ensure_ascii=False)
        (storage.VAULTS_DIR / f"{rec['id']}.json").write_text(text, encoding="utf-8")
    return rec


def _check_admin(request: Request) -> bool:
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw:
        return False
    try:
        ADMIN_SESSION.loads(raw, max_age=ADMIN_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _ad_context() -> dict:
    s = storage.get_settings()
    return {
        "ad_enabled": s["ad_enabled"] == "1",
        "ad_position": s["ad_position"],
        "ad_code": s["ad_code"],
        "ad_gate": s["ad_gate"],
        "ad_link": s["ad_link"],
        "ad_popunder_code": s["ad_popunder_code"],
    }


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html",
        {"ad": _ad_context()},
    )


@app.get("/v/{vid}", response_class=HTMLResponse)
async def vault_page(request: Request, vid: str):
    row = storage.get_row(vid)
    if row is None or storage.is_expired(row):
        storage.delete_vault(vid)
        return templates.TemplateResponse(
            request, "destroyed.html", {"ad": _ad_context()}, status_code=404
        )

    config_text = storage.get_config_text(vid)
    if config_text is None:
        storage.delete_vault(vid)
        return templates.TemplateResponse(
            request, "destroyed.html", {"ad": _ad_context()}, status_code=404
        )

    storage.incr_views(vid)
    try:
        cfg = json.loads(config_text)
    except json.JSONDecodeError:
        cfg = {"name": row["name"]}

    remaining = max(0, int(row["expires_at"] - time.time()))
    return templates.TemplateResponse(
        request, "vault.html",
        {
            "vid": vid,
            "name": row["name"],
            "cfg": cfg,
            "config_text": config_text,
            "filename": decoder.suggest_filename(cfg),
            "remaining": remaining,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "views": row["views"],
            "ad": _ad_context(),
        },
    )


# --------------------------------------------------------------------------
# Upload APIs
# --------------------------------------------------------------------------

@app.post("/api/decode")
async def api_decode(request: Request, file: UploadFile = File(...)):
    """Web upload: decode + store, return the vault link."""
    data = await file.read()
    try:
        config = decoder.decode_darktunnel(data)
    except decoder.DecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("decode failed")
        raise HTTPException(status_code=400, detail=f"Decode error: {exc}")

    rec = _sign_and_store(config, request=request)
    return {
        "ok": True,
        "id": rec["id"],
        "url": vault_url(request, rec["id"]),
        "expires_at": rec["expires_at"],
    }


@app.post("/api/vault")
async def api_vault(request: Request, x_vault_token: str = Header(default="")):
    """Bot upload: JSON body {name?, config}. Requires X-Vault-Token."""
    expected = env("VAULT_API_TOKEN")
    if not expected or not secrets.compare_digest(expected, x_vault_token):
        raise HTTPException(status_code=401, detail="Invalid vault token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON body")

    config = body.get("config")
    if config is None:
        raise HTTPException(status_code=400, detail="Missing 'config' field")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="'config' must be a decoded JSON object")

    rec = _sign_and_store(config, request=request)
    return {
        "ok": True,
        "id": rec["id"],
        "url": vault_url(request, rec["id"]),
        "expires_at": rec["expires_at"],
    }


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    if not _check_admin(request):
        return templates.TemplateResponse(
            request, "login.html",
            {"ad": _ad_context(), "error": ""},
        )
    st = storage.stats()
    settings = storage.get_settings()
    vaults = storage.list_vaults()
    return templates.TemplateResponse(
        request, "admin.html",
        {
            "ad": _ad_context(),
            "stats": st,
            "vaults": vaults,
            "settings": settings,
            "admin_configured": bool(env("VAULT_ADMIN_PASSWORD")),
        },
    )


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    expected = env("VAULT_ADMIN_PASSWORD")
    if not expected:
        raise HTTPException(status_code=500, detail="Admin password not configured")
    if not secrets.compare_digest(expected, password):
        return templates.TemplateResponse(
            request, "login.html",
            {"ad": _ad_context(), "error": "Wrong password"},
            status_code=401,
        )
    token = ADMIN_SESSION.dumps({"user": "admin"})
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        ADMIN_COOKIE, token, max_age=ADMIN_MAX_AGE,
        httponly=True, samesite="lax", secure=_is_https(request),
    )
    return resp


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


def _require_admin(request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


@app.post("/admin/delete/{vid}")
async def admin_delete(request: Request, vid: str):
    _require_admin(request)
    storage.delete_vault(vid)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/settings")
async def admin_settings(
    request: Request,
    ad_enabled: str = Form("0"),
    ad_position: str = Form("all"),
    ad_code: str = Form(""),
    ad_gate: str = Form("off"),
    ad_link: str = Form(""),
    ad_popunder_code: str = Form(""),
):
    _require_admin(request)
    if ad_position not in ("top", "bottom", "all"):
        ad_position = "all"
    if ad_gate not in ("off", "popunder", "redirect"):
        ad_gate = "off"
    storage.save_settings({
        "ad_enabled": "1" if ad_enabled == "1" else "0",
        "ad_position": ad_position,
        "ad_code": ad_code,
        "ad_gate": ad_gate,
        "ad_link": ad_link.strip(),
        "ad_popunder_code": ad_popunder_code,
    })
    return RedirectResponse("/admin?saved=1", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=int(env("PORT", "8000")))
