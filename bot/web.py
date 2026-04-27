"""
Embedded aiohttp web server: OAuth (link + login), session cookies,
profile view + edit. Runs inside the bot process on $PORT.
"""
from __future__ import annotations
import logging
import os
import time
from datetime import date, datetime
from html import escape as h
from urllib.parse import urlencode

import httpx
import jwt
from aiohttp import web

from db import pool

log = logging.getLogger("web")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

STATE_TTL_SECONDS = 600
SESSION_COOKIE = "specops_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days


# ─── Config helpers ──────────────────────────────────────────────────────────

def _public_base() -> str:
    return os.environ.get("WEB_PUBLIC_URL", "").rstrip("/")


def _redirect_uri() -> str:
    return f"{_public_base()}/google/callback"


def _secret() -> str:
    return os.environ["JWT_SECRET"]


def is_configured() -> bool:
    return bool(
        os.environ.get("WEB_PUBLIC_URL")
        and os.environ.get("GOOGLE_CLIENT_ID")
        and os.environ.get("GOOGLE_CLIENT_SECRET")
        and os.environ.get("JWT_SECRET")
    )


# ─── JWT helpers ─────────────────────────────────────────────────────────────

def _signed(payload: dict, ttl: int) -> str:
    return jwt.encode({**payload, "exp": int(time.time()) + ttl},
                      _secret(), algorithm="HS256")


def _verify(token: str) -> dict:
    return jwt.decode(token, _secret(), algorithms=["HS256"])


def _state_link(pan: str, discord_id: str) -> str:
    return _signed({"mode": "link", "pan": pan, "did": discord_id}, STATE_TTL_SECONDS)


def _state_login(expected_pan: str | None = None) -> str:
    payload: dict = {"mode": "login"}
    if expected_pan:
        payload["expected_pan"] = expected_pan
    return _signed(payload, STATE_TTL_SECONDS)


def _session_cookie_value(pan: str, discord_id: str, name: str, email: str | None) -> str:
    return _signed(
        {"pan": pan, "did": discord_id, "name": name, "email": email or ""},
        SESSION_TTL_SECONDS,
    )


def _session_from_request(request: web.Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _verify(raw)
    except jwt.PyJWTError:
        return None


def _set_session_cookie(response: web.Response, value: str):
    response.set_cookie(
        SESSION_COOKIE, value,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )


def _clear_session_cookie(response: web.Response):
    response.del_cookie(SESSION_COOKIE, path="/")


# ─── OAuth URL builders ──────────────────────────────────────────────────────

def _google_authorize(state_token: str) -> str:
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state_token,
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def google_signin_url(pan: str, discord_id: str) -> str:
    """For the [Sign in with Google] button in Discord — links a Google
    account to a specific (pan, discord_id) on the people row."""
    return _google_authorize(_state_link(pan, discord_id))


def google_login_url(expected_pan: str | None = None) -> str:
    """For the /login page — looks up an existing people row by Google
    identity and issues a session cookie. Optional PAN binds the session
    to a specific roster row (rejects mismatches)."""
    return _google_authorize(_state_login(expected_pan))


# ─── Templates ───────────────────────────────────────────────────────────────

def _layout(title: str, body_html: str, *, status: int = 200) -> web.Response:
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SPEC-OPS · {title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Inter", sans-serif;
    background: #0c0c0c; color: #f5f5f5;
  }}
  .wrap {{ max-width: 640px; margin: 0 auto; padding: 40px 20px 80px; }}
  .brand {{ font-size: 11px; color: #555; text-transform: uppercase;
    letter-spacing: 0.12em; margin-bottom: 12px; }}
  h1 {{ font-size: 28px; margin: 0 0 4px; letter-spacing: -0.02em; }}
  h2 {{ font-size: 18px; margin: 32px 0 12px; letter-spacing: -0.01em; }}
  .sub {{ color: #888; margin: 0 0 24px; font-size: 13px; }}
  .topbar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }}
  .topbar a {{ color: #888; text-decoration: none; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.08em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td {{ padding: 12px 0; border-bottom: 1px solid #1c1c1c; vertical-align: top; }}
  td:first-child {{ color: #888; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.08em; width: 40%; }}
  td:last-child {{ color: #f5f5f5; }}
  code {{ background: #1c1c1c; padding: 1px 6px; }}
  .empty {{ color: #555; }}

  form .field {{ margin: 16px 0; }}
  form label {{ display: block; color: #888; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.08em; margin-bottom: 6px; }}
  form input[type=text], form input[type=date], form input[type=url],
  form input[type=email], form textarea {{
    width: 100%; padding: 10px 12px; background: #161616; color: #f5f5f5;
    border: 1px solid #222; font: inherit; outline: none;
  }}
  form input:focus, form textarea:focus {{ border-color: #4f9dff; }}
  form textarea {{ min-height: 80px; resize: vertical; }}
  .row {{ display: flex; gap: 12px; }}
  .row > * {{ flex: 1; }}

  button, a.btn {{
    display: inline-block; padding: 11px 18px; font: inherit; font-weight: 600;
    cursor: pointer; border: none; text-decoration: none;
    background: #f5f5f5; color: #000;
  }}
  button.secondary, a.btn.secondary {{ background: transparent; color: #f5f5f5;
    border: 1px solid #2a2a2a; }}

  .note {{ margin-top: 24px; padding: 12px; border: 1px solid #222;
    color: #888; font-size: 13px; }}
  .err {{ border-color: #ff5757; color: #ff5757; }}
  .ok  {{ border-color: #4ade80; color: #4ade80; }}

  .stats {{ display: flex; gap: 12px; margin: 24px 0; }}
  .stat {{ flex: 1; padding: 18px 14px; border: 1px solid #1c1c1c; }}
  .stat .num {{ font-size: 32px; font-weight: 700; letter-spacing: -0.02em; }}
  .stat .lbl {{ font-size: 11px; color: #666; text-transform: uppercase;
    letter-spacing: 0.1em; margin-top: 4px; }}

  .opgrid {{ display: grid; grid-template-columns: 1fr; gap: 8px; margin-top: 12px; }}
  @media (min-width: 540px) {{ .opgrid {{ grid-template-columns: 1fr 1fr; }} }}
  .opcard {{ padding: 14px; border: 1px solid #1c1c1c; }}
  .opmeta {{ font-size: 11px; color: #666; text-transform: uppercase;
    letter-spacing: 0.1em; }}
  .opname {{ font-size: 16px; margin: 4px 0 6px; }}
  .opid code {{ font-size: 12px; }}
</style>
</head><body>
  <div class="wrap">
    <div class="brand">SPEC-OPS</div>
    {body_html}
  </div>
</body></html>"""
    return web.Response(text=html, content_type="text/html", status=status)


def _topbar(session: dict | None) -> str:
    if session:
        return (
            f'<div class="topbar">'
            f'<span>signed in as <code>{h(session["pan"])}</code></span>'
            f'<a href="/logout">sign out</a>'
            f'</div>'
        )
    return (
        f'<div class="topbar">'
        f'<span>SPEC-OPS</span>'
        f'<a href="/login">sign in</a>'
        f'</div>'
    )


# ─── Health + landing ────────────────────────────────────────────────────────

async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def landing(request: web.Request) -> web.Response:
    session = _session_from_request(request)
    if session:
        raise web.HTTPFound("/home")
    raise web.HTTPFound("/login")


async def home(request: web.Request) -> web.Response:
    session = _session_from_request(request)
    if not session:
        raise web.HTTPFound("/login")

    pan = session["pan"]
    name = session.get("name", "")

    async with pool().acquire() as con:
        row = await con.fetchrow(
            """
            SELECT name, email, wa_number, created_at,
                   (SELECT count(*)::int FROM op_assignments WHERE person_pan = people.pan AND state = 'ACTIVE') AS active_ops,
                   (SELECT count(*)::int FROM attendance     WHERE pp_pan = people.pan)                             AS total_attendance,
                   (SELECT count(*)::int FROM attendance     WHERE pp_pan = people.pan AND validation = 'CONFIRMED') AS confirmed
              FROM people WHERE pan = $1
            """,
            pan,
        )
        active = await con.fetch(
            """
            SELECT a.operation_id, a.role, o.city, f.name AS factory_name
              FROM op_assignments a
              JOIN operations o ON o.operation_id = a.operation_id
              JOIN factories  f ON f.factory_id = o.factory_id
             WHERE a.person_pan = $1 AND a.state = 'ACTIVE'
             ORDER BY a.role, a.operation_id
            """,
            pan,
        )

    if row is None:
        raise web.HTTPFound("/logout")

    ops_html = ""
    if active:
        items = "".join(
            f'<div class="opcard"><div class="opmeta">{h(a["role"])}</div>'
            f'<div class="opname">{h(a["factory_name"])}</div>'
            f'<div class="opid"><code>{h(a["operation_id"])}</code></div></div>'
            for a in active
        )
        ops_html = f'<h2>Active operations</h2><div class="opgrid">{items}</div>'
    else:
        ops_html = (
            '<h2>Active operations</h2>'
            '<div class="note">You\'re not assigned to any op yet. '
            'Once command assigns you, your op cards show up here.</div>'
        )

    body = f"""
    {_topbar(session)}
    <h1>Welcome, {h(row['name'])}</h1>
    <p class="sub"><code>{h(pan)}</code> · joined {row['created_at'].strftime('%d %b %Y')}</p>

    <div class="stats">
      <div class="stat"><div class="num">{row['active_ops']}</div><div class="lbl">Active ops</div></div>
      <div class="stat"><div class="num">{row['total_attendance']}</div><div class="lbl">Total tours</div></div>
      <div class="stat"><div class="num">{row['confirmed']}</div><div class="lbl">Confirmed</div></div>
    </div>

    {ops_html}

    <h2>Account</h2>
    <p style="margin:12px 0 24px;">
      <a class="btn secondary" href="/profile/{h(pan)}">View profile</a>
      <a class="btn"           href="/profile/{h(pan)}/edit">Edit profile</a>
    </p>
    """
    return _layout(f"Home · {row['name']}", body)


# ─── Login + logout ──────────────────────────────────────────────────────────

async def login_page(request: web.Request) -> web.Response:
    session = _session_from_request(request)
    if session:
        raise web.HTTPFound("/home")
    err = request.query.get("err", "")
    err_html = ""
    if err == "pan_mismatch":
        err_html = '<div class="note err">PAN doesn\'t match the Google account you signed in with.</div>'
    elif err == "no_profile":
        err_html = '<div class="note err">No SPEC-OPS profile linked to that Google account. Run /onboard in Discord first.</div>'
    elif err == "bad_pan":
        err_html = '<div class="note err">PAN format invalid. Expected like ABCDE1234F.</div>'

    body = f"""
    {_topbar(None)}
    <h1>Sign in</h1>
    <p class="sub">Enter your PAN, then sign in with the Google account you linked at <code>/onboard</code>.</p>
    {err_html}
    <form method="get" action="/auth/google/login">
      <div class="field">
        <label>PAN</label>
        <input type="text" name="pan" required minlength="10" maxlength="10"
               style="text-transform:uppercase" placeholder="ABCDE1234F" autocomplete="off">
      </div>
      <button type="submit">Continue with Google</button>
    </form>
    <div class="note">
      Haven't onboarded? Run <code>/onboard</code> in <code>#onboarding</code> on Discord first.
    </div>
    """
    return _layout("Sign in", body)


import re as _re
_PAN_RE = _re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


async def auth_google_login(request: web.Request) -> web.Response:
    pan = (request.query.get("pan") or "").strip().upper()
    if pan and not _PAN_RE.match(pan):
        raise web.HTTPFound("/login?err=bad_pan")
    return web.HTTPFound(google_login_url(pan or None))


async def logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    _clear_session_cookie(resp)
    return resp


# ─── OAuth callback (handles both link + login) ──────────────────────────────

async def google_callback(request: web.Request) -> web.Response:
    code = request.query.get("code")
    state_token = request.query.get("state")
    err = request.query.get("error")
    if err:
        return _layout("Sign-in cancelled",
                       f'{_topbar(None)}<h1>Cancelled</h1>'
                       f'<p>Google returned <code>{h(err)}</code>.</p>'
                       f'<a class="btn secondary" href="/login">Back to sign in</a>',
                       status=400)
    if not code or not state_token:
        return _layout("Missing code",
                       f'{_topbar(None)}<h1>Missing code</h1>'
                       f'<p>Try again.</p>'
                       f'<a class="btn secondary" href="/login">Back to sign in</a>',
                       status=400)
    try:
        state = _verify(state_token)
    except jwt.PyJWTError:
        return _layout("Link expired",
                       f'{_topbar(None)}<h1>Expired</h1>'
                       f'<p>Sign-in link expired. Try again.</p>'
                       f'<a class="btn secondary" href="/login">Back to sign in</a>',
                       status=400)

    info = await _exchange_code(code)
    if isinstance(info, web.Response):
        return info

    google_id = info.get("id")
    email = info.get("email")
    if not google_id or not email:
        return _layout("No identity",
                       f'{_topbar(None)}<h1>Sign-in failed</h1>'
                       f'<p>Google didn\'t return an id/email.</p>',
                       status=400)

    mode = state.get("mode", "link")
    if mode == "link":
        return await _handle_link(state, google_id, email)
    elif mode == "login":
        return await _handle_login(google_id, email, state.get("expected_pan"))
    else:
        return _layout("Unknown flow",
                       f'{_topbar(None)}<h1>Unknown flow</h1>',
                       status=400)


async def _exchange_code(code: str):
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
            return info_res.json()
        except Exception as e:
            log.exception("google oauth exchange failed")
            return _layout("Sign-in failed",
                           f'{_topbar(None)}<h1>Sign-in failed</h1>'
                           f'<p><code>{h(str(e))}</code></p>',
                           status=400)


async def _handle_link(state: dict, google_id: str, email: str) -> web.Response:
    pan = state["pan"]
    discord_id = state["did"]

    async with pool().acquire() as con:
        owner = await con.fetchrow(
            "SELECT pan FROM people WHERE google_id = $1 AND pan <> $2",
            google_id, pan,
        )
        if owner:
            return _layout("Already linked elsewhere",
                           f'{_topbar(None)}<h1>Already linked</h1>'
                           f'<p>This Google account is already linked to PAN '
                           f'<code>{h(owner["pan"])}</code>. Talk to command if this '
                           'is a mix-up.</p>',
                           status=400)

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
        return _layout("No matching profile",
                       f'{_topbar(None)}<h1>Not found</h1>'
                       f'<p>Run /onboard in Discord first.</p>',
                       status=400)

    # Issue a session cookie immediately so the user lands signed in.
    resp = web.HTTPFound(f"/profile/{pan}")
    _set_session_cookie(resp, _session_cookie_value(pan, discord_id, result["name"], email))
    return resp


async def _handle_login(google_id: str, email: str, expected_pan: str | None = None) -> web.Response:
    async with pool().acquire() as con:
        row = await con.fetchrow(
            """
            SELECT pan, discord_id, name FROM people
             WHERE google_id = $1 OR (google_id IS NULL AND email = $2)
             ORDER BY (google_id IS NOT NULL) DESC LIMIT 1
            """,
            google_id, email,
        )

    if not row:
        raise web.HTTPFound("/login?err=no_profile")

    if expected_pan and row["pan"] != expected_pan:
        raise web.HTTPFound("/login?err=pan_mismatch")

    resp = web.HTTPFound("/home")
    _set_session_cookie(resp, _session_cookie_value(
        row["pan"], row["discord_id"], row["name"], email,
    ))
    return resp


# ─── Profile page ────────────────────────────────────────────────────────────

PROFILE_FIELDS = [
    ("Name",                   "name",            False),
    ("PAN",                    "pan",             False),
    ("WhatsApp",               "wa_number",       False),
    ("Email (Google)",         "email",           False),
    ("DOB",                    "dob",             True),
    ("Location",               "location",        True),
    ("Languages",              "languages",       True),
    ("Hardest problem solved", "hardest_problem", True),
    ("Headshot URL",           "headshot_url",    True),
    ("Intro video URL",        "intro_video_url", True),
    ("Bank name",              "bank_name",       True),
    ("Account number",         "account_number",  True),
    ("IFSC",                   "ifsc",            True),
    ("UPI ID",                 "upi_id",          True),
]

EDITABLE_FIELDS = [k for _, k, fillable in PROFILE_FIELDS if fillable]


def _can_view(session: dict | None, pan: str) -> bool:
    if not session:
        return False
    if session["pan"] == pan:
        return True
    return False  # admin gating later


async def profile_view(request: web.Request) -> web.Response:
    pan = request.match_info.get("pan", "").strip().upper()
    session = _session_from_request(request)

    if not session:
        raise web.HTTPFound(f"/login?next=/profile/{pan}")
    if not _can_view(session, pan):
        return _layout("Forbidden",
                       f'{_topbar(session)}<h1>Forbidden</h1>'
                       f'<p>You can only view your own profile right now. '
                       f'Yours: <a href="/profile/{h(session["pan"])}">/profile/{h(session["pan"])}</a></p>',
                       status=403)

    async with pool().acquire() as con:
        row = await con.fetchrow("SELECT * FROM people WHERE pan = $1", pan)

    if not row:
        return _layout("Profile not found",
                       f'{_topbar(session)}<h1>Not found</h1>'
                       f'<p>No profile for PAN <code>{h(pan)}</code>.</p>',
                       status=404)

    rows_html = []
    for label, key, fillable in PROFILE_FIELDS:
        val = row[key]
        if val in (None, ""):
            val_str = '<span class="empty">—</span>'
        elif key == "account_number":
            val_str = h("•" * 4 + str(val)[-4:])
        else:
            val_str = h(str(val))
        rows_html.append(f'<tr><td>{h(label)}</td><td>{val_str}</td></tr>')

    body = f"""
    {_topbar(session)}
    <h1>{h(row['name'])}</h1>
    <p class="sub"><code>{h(row['pan'])}</code> · joined {row['created_at'].strftime('%d %b %Y')}</p>
    <table>{''.join(rows_html)}</table>
    <p style="margin-top:24px;">
      <a class="btn" href="/profile/{h(pan)}/edit">Edit profile</a>
    </p>
    """
    return _layout(f"Profile · {row['name']}", body)


async def profile_edit_form(request: web.Request) -> web.Response:
    pan = request.match_info.get("pan", "").strip().upper()
    session = _session_from_request(request)
    if not session:
        raise web.HTTPFound(f"/login?next=/profile/{pan}/edit")
    if not _can_view(session, pan):
        return _layout("Forbidden", f'{_topbar(session)}<h1>Forbidden</h1>', status=403)

    async with pool().acquire() as con:
        row = await con.fetchrow("SELECT * FROM people WHERE pan = $1", pan)
    if not row:
        return _layout("Profile not found",
                       f'{_topbar(session)}<h1>Not found</h1>',
                       status=404)

    def _val(key):
        v = row[key]
        return "" if v is None else str(v)

    body = f"""
    {_topbar(session)}
    <h1>Edit profile</h1>
    <p class="sub"><code>{h(row['pan'])}</code> · {h(row['name'])}</p>

    <form method="post" action="/profile/{h(pan)}">
      <h2>Personal</h2>
      <div class="row">
        <div class="field">
          <label>DOB</label>
          <input type="date" name="dob" value="{h(_val('dob'))}">
        </div>
        <div class="field">
          <label>Location</label>
          <input type="text" name="location" value="{h(_val('location'))}" placeholder="Mumbai, IN">
        </div>
      </div>
      <div class="field">
        <label>Languages (comma-separated)</label>
        <input type="text" name="languages" value="{h(_val('languages'))}" placeholder="English, Hindi, Marathi">
      </div>
      <div class="field">
        <label>Hardest problem you've solved</label>
        <textarea name="hardest_problem" placeholder="Brief paragraph">{h(_val('hardest_problem'))}</textarea>
      </div>

      <h2>Media</h2>
      <div class="field">
        <label>Headshot URL</label>
        <input type="url" name="headshot_url" value="{h(_val('headshot_url'))}" placeholder="https://...">
      </div>
      <div class="field">
        <label>Intro video URL</label>
        <input type="url" name="intro_video_url" value="{h(_val('intro_video_url'))}" placeholder="https://...">
      </div>

      <h2>Bank</h2>
      <div class="row">
        <div class="field">
          <label>Bank name</label>
          <input type="text" name="bank_name" value="{h(_val('bank_name'))}">
        </div>
        <div class="field">
          <label>IFSC</label>
          <input type="text" name="ifsc" value="{h(_val('ifsc'))}">
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>Account number</label>
          <input type="text" name="account_number" value="{h(_val('account_number'))}">
        </div>
        <div class="field">
          <label>UPI ID</label>
          <input type="text" name="upi_id" value="{h(_val('upi_id'))}" placeholder="name@upi">
        </div>
      </div>

      <p style="margin-top:24px;">
        <button type="submit">Save</button>
        <a class="btn secondary" href="/profile/{h(pan)}">Cancel</a>
      </p>
    </form>
    """
    return _layout(f"Edit · {row['name']}", body)


async def profile_save(request: web.Request) -> web.Response:
    pan = request.match_info.get("pan", "").strip().upper()
    session = _session_from_request(request)
    if not session:
        raise web.HTTPFound(f"/login?next=/profile/{pan}/edit")
    if not _can_view(session, pan):
        return _layout("Forbidden", f'{_topbar(session)}<h1>Forbidden</h1>', status=403)

    form = await request.post()

    updates: dict[str, object | None] = {}
    for key in EDITABLE_FIELDS:
        raw = (form.get(key) or "").strip()
        if not raw:
            updates[key] = None
            continue
        if key == "dob":
            try:
                updates[key] = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                return _layout("Save failed",
                               f'{_topbar(session)}<h1>Invalid date</h1>'
                               f'<p>DOB must be YYYY-MM-DD. Got <code>{h(raw)}</code>.</p>'
                               f'<a class="btn secondary" href="/profile/{h(pan)}/edit">Back</a>',
                               status=400)
        else:
            updates[key] = raw

    set_cols = []
    args = []
    i = 1
    for col, val in updates.items():
        set_cols.append(f"{col} = ${i}")
        args.append(val)
        i += 1
    args.append(pan)
    sql = f"UPDATE people SET {', '.join(set_cols)}, updated_at = now() WHERE pan = ${i}"

    try:
        async with pool().acquire() as con:
            await con.execute(sql, *args)
    except Exception as e:
        log.exception("profile save failed")
        return _layout("Save failed",
                       f'{_topbar(session)}<h1>Save failed</h1>'
                       f'<p><code>{h(str(e))}</code></p>'
                       f'<a class="btn secondary" href="/profile/{h(pan)}/edit">Back</a>',
                       status=400)

    raise web.HTTPFound(f"/profile/{pan}?saved=1")


# ─── App + lifecycle ─────────────────────────────────────────────────────────

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", landing)
    app.router.add_get("/home", home)

    app.router.add_get("/login", login_page)
    app.router.add_get("/logout", logout)
    app.router.add_get("/auth/google/login", auth_google_login)

    app.router.add_get("/google/start", google_start_link)  # legacy link path
    app.router.add_get("/google/callback", google_callback)

    app.router.add_get("/profile/{pan}", profile_view)
    app.router.add_get("/profile/{pan}/edit", profile_edit_form)
    app.router.add_post("/profile/{pan}", profile_save)
    return app


async def google_start_link(request: web.Request) -> web.Response:
    """Used by the Discord button — state is already signed by the caller."""
    state_token = request.query.get("state")
    if not state_token:
        return web.Response(status=400, text="missing state")
    try:
        _verify(state_token)
    except jwt.PyJWTError:
        return web.Response(status=400, text="bad/expired state")
    return web.HTTPFound(_google_authorize(state_token))


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
