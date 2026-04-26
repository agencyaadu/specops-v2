"""
Tiny aiohttp web server inside the bot process.

Routes:
  GET  /health              - Railway healthcheck
  GET  /google/start?...    - kicks off Google OAuth
  GET  /google/callback?... - finalises link, updates people row

Used by the [Sign in with Google] button shown after onboarding —
links the user's Google account to the people row keyed by their
PAN + Discord ID.
"""
from __future__ import annotations
import logging
import os
import time
from urllib.parse import urlencode

import httpx
import jwt
from aiohttp import web

from db import pool

log = logging.getLogger("web_oauth")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

STATE_TTL_SECONDS = 600  # 10 min to complete the OAuth dance


def _public_base() -> str:
    return os.environ.get("WEB_PUBLIC_URL", "").rstrip("/")


def _redirect_uri() -> str:
    return f"{_public_base()}/google/callback"


def _secret() -> str:
    return os.environ["JWT_SECRET"]


def _signed_state(payload: dict) -> str:
    body = {**payload, "exp": int(time.time()) + STATE_TTL_SECONDS}
    return jwt.encode(body, _secret(), algorithm="HS256")


def _verify_state(token: str) -> dict:
    return jwt.decode(token, _secret(), algorithms=["HS256"])


def google_signin_url(pan: str, discord_id: str) -> str:
    """Sign + URL-encode an OAuth start URL for a specific PAN + Discord."""
    state = _signed_state({"pan": pan, "did": discord_id})
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def is_configured() -> bool:
    return bool(
        os.environ.get("WEB_PUBLIC_URL")
        and os.environ.get("GOOGLE_CLIENT_ID")
        and os.environ.get("GOOGLE_CLIENT_SECRET")
        and os.environ.get("JWT_SECRET")
    )


# ─── HTTP routes ─────────────────────────────────────────────────────────────

async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def google_start(request: web.Request) -> web.Response:
    state_token = request.query.get("state")
    if not state_token:
        return web.Response(status=400, text="missing state")
    try:
        state = _verify_state(state_token)
    except jwt.PyJWTError:
        return web.Response(status=400, text="bad/expired state")
    return web.HTTPFound(google_signin_url(state["pan"], state["did"]))


async def google_callback(request: web.Request) -> web.Response:
    code = request.query.get("code")
    state_token = request.query.get("state")
    err = request.query.get("error")
    if err:
        return _result_html(
            "Sign-in cancelled",
            f"Google returned: <code>{err}</code>. Close this tab and try the button again.",
            ok=False,
        )
    if not code or not state_token:
        return _result_html("Missing code", "Open the link from Discord again.", ok=False)
    try:
        state = _verify_state(state_token)
    except jwt.PyJWTError:
        return _result_html(
            "Link expired",
            "The sign-in link expired (10 minute window). Run /onboard in Discord again to get a fresh one.",
            ok=False,
        )

    pan = state["pan"]
    discord_id = state["did"]

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            tok = await client.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
            })
            tok.raise_for_status()
            access_token = tok.json()["access_token"]

            info_res = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            info_res.raise_for_status()
            info = info_res.json()
        except Exception as e:
            log.exception("google oauth exchange failed")
            return _result_html("Sign-in failed", f"<code>{e}</code>", ok=False)

    google_id = info.get("id")
    email = info.get("email")
    if not google_id or not email:
        return _result_html("No identity", "Google didn't return an id/email. Try again.", ok=False)

    async with pool().acquire() as con:
        # Make sure THIS Google account isn't already linked to someone else.
        owner = await con.fetchrow(
            "SELECT pan FROM people WHERE google_id = $1 AND pan <> $2",
            google_id, pan,
        )
        if owner:
            return _result_html(
                "Already linked elsewhere",
                f"This Google account is already linked to PAN <code>{owner['pan']}</code>. "
                "Talk to command if this is a mix-up.",
                ok=False,
            )

        result = await con.fetchrow(
            """
            UPDATE people
               SET google_id = $1, email = $2, updated_at = now()
             WHERE pan = $3 AND discord_id = $4
             RETURNING name
            """,
            google_id, email, pan, discord_id,
        )

    if not result:
        return _result_html(
            "No matching profile",
            "Couldn't find a SPEC-OPS profile for this PAN + Discord. Run /onboard again.",
            ok=False,
        )

    return _result_html(
        "Linked",
        f"Welcome, <strong>{result['name']}</strong>. Your Google account "
        f"(<code>{email}</code>) is now linked to PAN <code>{pan}</code>. "
        "You can close this tab.",
        ok=True,
    )


def _result_html(title: str, body_html: str, *, ok: bool) -> web.Response:
    accent = "#4ade80" if ok else "#ff5757"
    icon = "✓" if ok else "✕"
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SPEC-OPS · {title}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; font: 16px/1.5 -apple-system, BlinkMacSystemFont, sans-serif;
    background: #0c0c0c; color: #f5f5f5;
  }}
  .card {{
    max-width: 460px; padding: 32px; border: 1px solid #222; background: #111;
  }}
  .icon {{
    width: 36px; height: 36px; line-height: 36px; text-align: center;
    border: 1px solid {accent}; color: {accent}; font-size: 20px;
    margin-bottom: 16px;
  }}
  h1 {{ font-size: 22px; margin: 0 0 8px; letter-spacing: -0.02em; }}
  p {{ color: #aaa; margin: 0; }}
  code {{ background: #1c1c1c; padding: 1px 6px; }}
  .brand {{ font-size: 11px; color: #555; text-transform: uppercase;
    letter-spacing: 0.12em; margin-bottom: 12px; }}
</style>
</head><body>
  <div class="card">
    <div class="brand">SPEC-OPS</div>
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{body_html}</p>
  </div>
</body></html>"""
    return web.Response(text=html, content_type="text/html",
                        status=200 if ok else 400)


# ─── App + lifecycle ─────────────────────────────────────────────────────────

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/google/start", google_start)
    app.router.add_get("/google/callback", google_callback)
    return app


async def start_server() -> web.TCPSite:
    port = int(os.environ.get("PORT", "8080"))
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("web server listening on :%d (public=%s)",
             port, _public_base() or "<unset>")
    return site
