"""Panel admina backendu: /admin.

Logowanie haslem z ADMIN_PASSWORD (bez niego panel jest wylaczony). Sesja = podpisane ciasteczko (HMAC kluczem
SESSION_SECRET), HttpOnly + SameSite=Strict (+ Secure na https). Zmiana hasla uniewaznia wszystkie sesje.
Akcje (wyloguj, usun wpis) wymagaja tokenu CSRF. Po 5 nieudanych probach w 15 min z jednego adresu - blokada.

Widok: stan serwera i Siedziby, kafelki z licznikami (dzis / okres / poprzedni okres / razem), wykres dzienny,
rankingi (rosliny, rozdzialy, plant.id, wyszukiwania, zrodla, przepisy, strony), dziennik na zywo, eksport CSV.
"""
import csv
import hashlib
import hmac
import io
import json
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import db
from app.config import settings

router = APIRouter(prefix="/admin", include_in_schema=False)
env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                  autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)

COOKIE = "kw_admin"
SECRET = settings.session_secret or secrets.token_hex(32)
FAIL_LIMIT, FAIL_WINDOW = 5, 15 * 60          # 5 zlych hasel na 15 min z jednego adresu
GLOBAL_FAIL_LIMIT = 40                        # i najwyzej 40 zlych hasel na 15 min ze wszystkich adresow
_fails: dict[str, deque] = defaultdict(deque)

PERIODS = (7, 30, 90)
# rodzaj -> (nazwa kafelka, opis)
METRICS = {
    "visitors": ("Goście", "różne przeglądarki danego dnia (bez ciasteczek); suma dzienna"),
    "plant_view": ("Odsłony roślin", "wejścia na strony roślin"),
    "plantid_click": ("Kliknięcia plant.id", "kliknięcia „Rozpoznaj” – każde zużywa klucz plant.id"),
    "recipe_open": ("Otwarte przepisy", "otwarcia okna przepisu"),
    "source_click": ("Kliknięte źródła", "przejścia do źródeł przepisów"),
    "search": ("Wyszukiwania", "wpisane wyszukiwania"),
    "page": ("Odsłony stron", "wszystkie odsłony stron Kwiatownika"),
}
# rankingi: rodzaj -> tytul
TOPS = [
    ("plant_view", "Najczęściej oglądane rośliny"),
    ("plant_section", "Najczęściej czytane rozdziały"),
    ("plantid_result", "Rozpoznane przez plant.id"),
    ("plantid_unknown", "Rozpoznane, a brak w Kwiatowniku"),
    ("search", "Wyszukiwania"),
    ("recipe_open", "Otwierane przepisy"),
    ("source_click", "Klikane źródła"),
    ("page", "Strony"),
    ("papyrus", "Kliknięcia w papirus"),
]
KIND_PL = {"wiedza": "wiedza", "przepisy": "przepisy", "wdrozenie": "wdrożenie", "audyt": "audyt",
           "zadanie": "zadanie", "info": "info"}


def _core():
    from app import main            # leniwie - main importuje ten modul
    return main


# ------------------------------------------------------------------ sesja
def _pw_tag() -> str:
    return hashlib.sha256(("kw|" + settings.admin_password).encode()).hexdigest()[:16]


def _sign(msg: str) -> str:
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    body = f"{int(time.time() + settings.admin_session_days * 86400)}.{secrets.token_hex(12)}"
    return f"{body}.{_sign(body + '.' + _pw_tag())}"


def session_nonce(request: Request) -> str | None:
    """Zwraca losowy identyfikator sesji, gdy ciasteczko jest wazne (podpis, termin, aktualne haslo)."""
    if not settings.admin_password:
        return None
    raw = request.cookies.get(COOKIE) or ""
    parts = raw.split(".")
    if len(parts) != 3 or not parts[0].isdigit():
        return None
    exp, nonce, sig = parts
    if int(exp) < time.time():
        return None
    if not hmac.compare_digest(sig, _sign(f"{exp}.{nonce}.{_pw_tag()}")):
        return None
    return nonce


def csrf_token(nonce: str) -> str:
    return _sign("csrf." + nonce)[:32]


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    return urlparse(origin).netloc == request.headers.get("host", request.url.netloc)


async def _form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 10_000:
        return {}
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}


def _ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (
        request.client.host if request.client else "?")


def _prune(q: deque) -> deque:
    cutoff = time.time() - FAIL_WINDOW
    while q and q[0] < cutoff:
        q.popleft()
    return q


def _blocked(ip: str) -> bool:
    return len(_prune(_fails[ip])) >= FAIL_LIMIT or len(_prune(_fails["*"])) >= GLOBAL_FAIL_LIMIT


# ------------------------------------------------------------------ odpowiedzi
def _page(template: str, status: int = 200, **ctx) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    html = env.get_template(template).render(nonce=nonce, **ctx)
    resp = HTMLResponse(html, status_code=status)
    _secure_headers(resp, nonce)
    return resp


def _secure_headers(resp: Response, nonce: str = "") -> None:
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    resp.headers.update({
        "Content-Security-Policy": (f"default-src 'none'; script-src {script}; style-src 'unsafe-inline'; "
                                    "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
                                    "base-uri 'none'"),
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": "no-store",
        "Referrer-Policy": "same-origin",       # "no-referrer" daje Origin: null w formularzach
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    })


def _redirect(url: str = "/admin") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    _secure_headers(resp)
    return resp


def _params(request: Request) -> tuple[int, str]:
    try:
        days = int(request.query_params.get("dni", "30"))
    except ValueError:
        days = 30
    metric = request.query_params.get("m", "plant_view")
    return (days if days in PERIODS else 30), (metric if metric in METRICS else "plant_view")


# ------------------------------------------------------------------ dane widoku
def _fmt_uptime(sec: int) -> str:
    d, rest = divmod(sec, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    return (f"{d} d " if d else "") + (f"{h} h " if d or h else "") + f"{m} min"


def _ago(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "nigdy"
    s = int((now - dt).total_seconds())
    if s < 90:
        return "przed chwilą"
    if s < 3600:
        return f"{s // 60} min temu"
    if s < 172800:
        return f"{s // 3600} h temu"
    return f"{s // 86400} dni temu"


def _label(kind: str, key: str) -> str:
    if kind == "plant_section":
        pid, _, sec = key.partition(":")
        return f"{pid} · {sec.replace('_', ' ')}"
    return key


def dashboard_data(days: int, metric: str) -> dict:
    core = _core()
    tz = core.TZ
    now_local = datetime.now(tz)
    today = now_local.strftime("%Y-%m-%d")
    day_list = [(now_local - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    prev_start = (now_local - timedelta(days=2 * days - 1)).strftime("%Y-%m-%d")
    since = day_list[0]

    tiles = []
    for kind, (name, desc) in METRICS.items():
        per_day = db.daily(kind, prev_start)
        cur = sum(n for d, n in per_day.items() if d >= since)
        prev = sum(n for d, n in per_day.items() if d < since)
        change = None if not prev else round((cur - prev) * 100 / prev)
        tiles.append({"kind": kind, "name": name, "desc": desc, "today": per_day.get(today, 0), "period": cur,
                      "prev": prev, "change": change, "total": db.total(kind), "active": kind == metric})

    series_raw = db.daily(metric, since)
    series = [{"d": d, "n": series_raw.get(d, 0)} for d in day_list]

    tops = []
    for kind, title in TOPS:
        rows = db.top(kind, since, 10)
        biggest = rows[0][1] if rows else 0
        tops.append({"kind": kind, "title": title, "sum": db.total(kind, since=since),
                     "rows": [{"label": _label(kind, k), "n": n, "pct": round(n * 100 / biggest) if biggest else 0}
                              for k, n in rows]})

    st, upd = db.get_state("siedziba")
    st = st or {}
    boot, _ = db.get_state("boot")
    now_utc = db.now()
    alive = bool(upd and now_utc - upd < timedelta(minutes=settings.alive_minutes))
    sqlite = settings.database_url.startswith("sqlite")
    entries = []
    for e in db.live_admin(100):
        entries.append({**e, "kind_pl": KIND_PL.get(e["kind"], e["kind"]),
                        "when": e["ts"].astimezone(tz).strftime("%d.%m.%Y %H:%M"),
                        "ago": _ago(e["ts"], now_utc)})
    return {
        "days": days, "periods": PERIODS, "metric": metric, "metric_name": METRICS[metric][0],
        "metric_desc": METRICS[metric][1], "tiles": tiles, "series": series,
        "series_sum": sum(p["n"] for p in series), "tops": tops, "entries": entries,
        "server": {"boot": core.BOOT_ID, "uptime": _fmt_uptime(int(time.time() - core.STARTED)),
                   "db": "SQLite (ulotny)" if sqlite else "PostgreSQL (trwały)", "sqlite": sqlite,
                   "restored": bool((boot or {}).get("restored")), **db.counts_overview(),
                   "live_keep": settings.live_keep, "tz": settings.timezone,
                   "now": now_local.strftime("%d.%m.%Y %H:%M")},
        "siedziba": {"alive": alive, "ago": _ago(upd, now_utc),
                     "when": upd.astimezone(tz).strftime("%d.%m.%Y %H:%M") if upd else None,
                     "status": st.get("status"), "zadania": st.get("zadania"), "info": st.get("info"),
                     "alive_minutes": settings.alive_minutes},
        "site_url": settings.site_url,
    }


def _status_text(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:300]
    return str(value)[:300]


env.filters["num"] = lambda n: f"{int(n):,}".replace(",", "\u202f")   # waska twarda spacja
env.filters["status"] = _status_text


# ------------------------------------------------------------------ trasy
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_home(request: Request):
    if not settings.admin_password:
        return _page("admin_login.html", status=503, disabled=True)
    nonce = session_nonce(request)
    if not nonce:
        return _page("admin_login.html", error=None, blocked=_blocked(_ip(request)))
    days, metric = _params(request)
    data = dashboard_data(days, metric)

    def link(**kw):
        q = {"dni": days, "m": metric, **kw}
        return "/admin?" + urlencode(q)

    return _page("admin.html", csrf=csrf_token(nonce), link=link, **data)


@router.post("/login")
async def admin_login(request: Request):
    if not settings.admin_password:
        return _page("admin_login.html", status=503, disabled=True)
    ip = _ip(request)
    if _blocked(ip):
        return _page("admin_login.html", status=429, blocked=True, error=None)
    if not _same_origin(request):
        return _page("admin_login.html", status=403, error="Nieprawidłowe źródło formularza.")
    form = await _form(request)
    given = form.get("haslo", "")
    if not hmac.compare_digest(given.encode(), settings.admin_password.encode()):
        _fails[ip].append(time.time())
        _fails["*"].append(time.time())
        left = max(0, FAIL_LIMIT - len(_fails[ip]))
        return _page("admin_login.html", status=401, blocked=_blocked(ip),
                     error=f"Złe hasło. Pozostałe próby: {left}.")
    _fails.pop(ip, None)
    resp = _redirect("/admin")
    resp.set_cookie(COOKIE, make_session(), max_age=settings.admin_session_days * 86400, path="/admin",
                    httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return resp


async def _checked(request: Request) -> dict | None:
    """Formularz akcji: wazna sesja + token CSRF + to samo zrodlo. Zwraca pola albo None."""
    nonce = session_nonce(request)
    if not nonce or not _same_origin(request):
        return None
    form = await _form(request)
    if not hmac.compare_digest(form.get("csrf", ""), csrf_token(nonce)):
        return None
    return form


@router.post("/logout")
async def admin_logout(request: Request):
    if await _checked(request) is None:
        return _redirect("/admin")
    resp = _redirect("/admin")
    resp.delete_cookie(COOKIE, path="/admin")
    return resp


@router.post("/wpis/{entry_id}/usun")
async def admin_delete_entry(entry_id: int, request: Request):
    form = await _checked(request)
    if form is None:
        return Response("Brak uprawnień", status_code=403)
    db.delete_live(entry_id)
    _core()._cache.clear()
    back = form.get("wroc", "/admin")
    return _redirect(back if back.startswith("/admin") else "/admin")


@router.get("/eksport.csv")
def admin_export(request: Request):
    if not session_nonce(request):
        return _redirect("/admin")
    try:
        days = int(request.query_params.get("dni", "0"))
    except ValueError:
        days = 0
    since = None
    if days > 0:
        since = (datetime.now(_core().TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["dzien", "rodzaj", "klucz", "liczba"])
    for r in db.all_rows(since):
        w.writerow([r["day"], r["kind"], r["key"], r["n"]])
    name = f"kwiatownik-liczniki-{datetime.now(_core().TZ):%Y-%m-%d}" + (f"-{days}dni" if since else "") + ".csv"
    resp = Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
    _secure_headers(resp)
    return resp
