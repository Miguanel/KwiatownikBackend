"""Backend Kwiatownika (Render, plan darmowy).

Strona Kwiatownika jest statyczna, wiec wszystko, co wymaga pamieci, robi ten maly serwer:
  - liczy klikniecia w rozpoznawanie roslin (plant.id) i inne zdarzenia (odslony roslin, otwierane rozdzialy,
    przepisy, klikniete zrodla, wyszukiwania) - bez ciasteczek i bez zapisywania adresow IP,
  - /api/ping - przegladarka pyta co 10 min, gdy ktos klika po Kwiatowniku (darmowy Render usypia po 15 min),
  - przechowuje stan Siedziby i jej dziennik na zywo (papirus na stronie glownej),
  - Siedziba (lokalnie) wysyla heartbeat, pobiera liczniki do swojej bazy i - gdy serwer zgubil dane
    po restarcie - odtwarza je (restore),
  - /admin - panel admina (haslo ADMIN_PASSWORD): statystyki, stan Siedziby, dziennik, eksport CSV.
"""
import hashlib
import json
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import settings

BOOT_ID = secrets.token_hex(6)            # nowy przy kazdym starcie - Siedziba wie, ze trzeba odtworzyc liczniki
STARTED = time.time()
TZ = ZoneInfo(settings.timezone)

# rodzaj zdarzenia -> jak sprawdzic klucz
ID_RE = re.compile(r"^[a-z0-9_\-]{1,80}$")
KINDS = {
    "page": "path",                # odslona strony (rodzaj: glowna, roslina, przepisy, ...)
    "plant_view": "id",            # wejscie na strone rosliny
    "plant_section": "pair",       # otwarty rozdzial strony rosliny: "<id>:<rozdzial>"
    "plantid_click": "none",       # klikniecie "Rozpoznaj" (plant.id)
    "plantid_result": "text",      # rozpoznana nazwa lacinska ("" = nie rozpoznano)
    "plantid_unknown": "text",     # rozpoznana roslina, ktorej nie ma w Kwiatowniku
    "recipe_open": "text",         # otwarty przepis (id albo tytul)
    "source_click": "text",        # klikniete zrodlo (domena)
    "search": "text",              # wyszukiwanie (male litery, max 40 znakow)
    "papyrus": "text",             # klikniecie w papirus (wpis kroniki)
}
BOT_RE = re.compile(r"bot|crawl|spider|slurp|facebookexternalhit|preview|monitor|uptime", re.I)

_salt = {"day": "", "value": ""}
_seen: set[str] = set()                    # dzisiejsi goscie (skroty z sola dnia - tylko w pamieci)
_rate: dict[str, deque] = defaultdict(deque)
_cache: dict[str, tuple[float, dict]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # dane przetrwaly start (Postgres albo plik SQLite zostal) -> nie trzeba ich odtwarzac z Siedziby
    survived = not settings.database_url.startswith("sqlite") or not db.counters_empty()
    db.set_state("boot", {"boot": BOOT_ID, "restored": survived})
    yield


app = FastAPI(title="Kwiatownik Backend", lifespan=lifespan, docs_url="/docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "Authorization"], max_age=86400)


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def days_ago(n: int) -> str:
    return (datetime.now(TZ) - timedelta(days=n)).strftime("%Y-%m-%d")


def client_hash(request: Request) -> str:
    """Skrot przegladarki z sola zmieniana codziennie (sol tylko w pamieci) - nie da sie z niego odtworzyc IP."""
    day = today()
    if _salt["day"] != day:
        _salt.update(day=day, value=secrets.token_hex(16))
        _seen.clear()
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (
        request.client.host if request.client else "")
    ua = request.headers.get("user-agent", "")
    return hashlib.sha256(f"{_salt['value']}|{ip}|{ua}".encode()).hexdigest()[:20]


def allowed(who: str, n: int) -> bool:
    q = _rate[who]
    cutoff = time.time() - 3600
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) + n > settings.events_per_hour:
        return False
    q.extend([time.time()] * n)
    return True


def clean_key(kind: str, raw) -> str | None:
    rule = KINDS.get(kind)
    value = "" if raw is None else str(raw).strip()
    if rule == "none":
        return ""
    if rule == "id":
        value = value.lower()
        return value if ID_RE.match(value) else None
    if rule == "pair":
        pid, _, sec = value.lower().partition(":")
        sec = re.sub(r"[^a-z0-9_\-]+", "_", sec)[:40].strip("_")
        return f"{pid}:{sec}" if ID_RE.match(pid) and sec else None
    if rule == "path":
        value = value.lower()
        return value if re.match(r"^[a-z0-9_\-]{1,30}$", value) else None
    if rule == "text":
        value = re.sub(r"\s+", " ", value)
        if kind == "search":
            value = value.lower()[:40]
        return value[:120]
    return None


def require_siedziba(request: Request) -> None:
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not settings.siedziba_token or not secrets.compare_digest(token, settings.siedziba_token):
        raise HTTPException(401, "zly token Siedziby")


# ------------------------------------------------------------------ przegladarka
@app.get("/")
def root():
    return {"app": "Kwiatownik Backend", "ok": True, "docs": "/docs", "admin": "/admin"}


@app.api_route("/api/ping", methods=["GET", "HEAD"])
def ping():
    """Keep-alive: przegladarka pyta co 10 min, gdy ktos uzywa Kwiatownika (Render usypia po 15 min ciszy)."""
    return {"ok": True, "boot": BOOT_ID, "uptime_s": int(time.time() - STARTED)}


@app.post("/api/events", status_code=204)
async def events(request: Request):
    """Zdarzenia z Kwiatownika: {"events": [{"k": rodzaj, "v": klucz}, ...]}. Wysylane przez navigator.sendBeacon
    jako text/plain (bez zapytania wstepnego CORS). Liczy tylko liczby - bez ciasteczek, IP i identyfikatorow."""
    if BOT_RE.search(request.headers.get("user-agent", "")):
        return Response(status_code=204)
    body = await request.body()
    if len(body) > 20_000:
        raise HTTPException(413, "za duzo danych")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "zly JSON")
    items = data.get("events") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise HTTPException(400, "brak listy events")
    items = [e for e in items[:40] if isinstance(e, dict) and e.get("k") in KINDS]
    who = client_hash(request)
    if not allowed(who, max(1, len(items))):
        return Response(status_code=204)          # cicho ignorujemy nadmiar (limit na godzine)
    day = today()
    rows: dict[tuple[str, str, str], int] = defaultdict(int)
    for e in items:
        key = clean_key(e["k"], e.get("v"))
        if key is not None:
            rows[(day, e["k"], key)] += 1
    if who not in _seen and items:
        _seen.add(who)
        rows[(day, "visitors", "")] += 1
    db.bump(dict(rows))
    _cache.clear()
    return Response(status_code=204)


def _cached(name: str, ttl: float, fn):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[name] = (time.time(), value)
    return value


@app.get("/api/stats/public")
def stats_public():
    """Liczby do papirusu na stronie glownej (bufor 60 s)."""
    def build():
        d, week = today(), days_ago(6)
        return {
            "dzis": d,
            "plantid": {"razem": db.total("plantid_click"), "dzis": db.total("plantid_click", day=d)},
            "rosliny_odslony": {"razem": db.total("plant_view"), "dzis": db.total("plant_view", day=d),
                                "tydzien": db.total("plant_view", since=week)},
            "goscie": {"dzis": db.total("visitors", day=d), "tydzien": db.total("visitors", since=week)},
            "najczesciej_czytane": [{"id": k, "n": n} for k, n in db.top("plant_view", week, 6)],
            "najczesciej_rozpoznawane": [{"nazwa": k, "n": n} for k, n in db.top("plantid_result", days_ago(29), 5)],
        }
    return _cached("public", 60, build)


@app.get("/api/live")
def live_state():
    """Stan Siedziby (czy pracuje, nad czym) + dziennik na zywo - do papirusu."""
    st, upd = db.get_state("siedziba")
    alive = bool(upd and db.now() - upd < timedelta(minutes=settings.alive_minutes))
    return {"siedziba": {"zyje": alive, "ostatnio": upd.isoformat() if upd else None, **(st or {})},
            "wpisy": db.recent_live(30), "serwer": {"boot": BOOT_ID, "uptime_s": int(time.time() - STARTED)}}


# ------------------------------------------------------------------ Siedziba (token)
@app.post("/api/siedziba/heartbeat", dependencies=[Depends(require_siedziba)])
async def heartbeat(request: Request):
    """Siedziba co kilka minut: stan (status, zadania), wpisy dziennika na zywo. Odpowiedz mowi, czy serwer
    zgubil liczniki (nowy start bez odtworzenia) - wtedy Siedziba wysyla kopie (restore)."""
    data = await request.json()
    status = {k: data.get(k) for k in ("status", "zadania", "info") if k in data}
    db.set_state("siedziba", status)
    added = db.add_live([e for e in (data.get("wpisy") or [])[:50] if isinstance(e, dict)], settings.live_keep)
    boot, _ = db.get_state("boot")
    restored = bool((boot or {}).get("restored"))
    return {"ok": True, "boot": BOOT_ID, "potrzebna_kopia": not restored, "dodano_wpisow": added}


@app.get("/api/siedziba/counters", dependencies=[Depends(require_siedziba)])
def counters_since(since: str):
    """Liczniki od dnia `since` (RRRR-MM-DD) - Siedziba trzyma ich kopie we wlasnej bazie."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", since):
        raise HTTPException(400, "since = RRRR-MM-DD")
    return {"boot": BOOT_ID, "wiersze": db.rows_since(since)}


@app.post("/api/siedziba/restore", dependencies=[Depends(require_siedziba)])
async def restore(request: Request):
    """Odtworzenie licznikow po restarcie darmowego serwera (dysk znika): {"wiersze": [{day, kind, key, n}]}.
    Kopia jest DODAWANA do tego, co przyszlo od startu serwera; raz na start (ponowienie = 409, bez podwojenia)."""
    boot, _ = db.get_state("boot")
    if (boot or {}).get("restored"):
        raise HTTPException(409, "liczniki juz odtworzone po tym starcie")
    data = await request.json()
    rows = [r for r in (data.get("wiersze") or []) if isinstance(r, dict) and r.get("kind") and r.get("day")]
    n = db.restore(rows)
    db.set_state("boot", {"boot": BOOT_ID, "restored": True})
    _cache.clear()
    return {"ok": True, "wiersze": n}


# ------------------------------------------------------------------ panel admina (/admin)
from app.admin import router as admin_router  # noqa: E402  (na koncu - admin korzysta z tego modulu)

app.include_router(admin_router)
