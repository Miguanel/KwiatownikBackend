"""Baza backendu: liczniki zdarzen (dzien, rodzaj, klucz -> liczba), dziennik Siedziby na zywo, stan Siedziby."""
import json
import os
import threading
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, delete, func,
                        insert, select, update)

from app.config import settings

# baza robocza: zawsze plik SQLite (trwala kopia w Postgresie/Neon - app/persist.py)
folder = os.path.dirname(settings.database_url[len("sqlite:///"):])
if folder:
    os.makedirs(folder, exist_ok=True)
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})

# co zmienilo sie od ostatniego zapisu trwalej kopii (persist.flush)
_dirty_lock = threading.Lock()
_dirty_keys: set[tuple[str, str, str]] = set()
_dirty_live = False

meta = MetaData()
counters = Table(
    "counters", meta,
    Column("day", String(10), primary_key=True),      # RRRR-MM-DD albo "archiwum" (suma starszych dni z Siedziby)
    Column("kind", String(32), primary_key=True),     # plant_view, plantid_click, ...
    Column("key", String(120), primary_key=True),     # np. id rosliny ("" = bez klucza)
    Column("n", Integer, nullable=False, default=0),
)
live = Table(
    "live_events", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("eid", String(64), unique=True),           # id nadane przez Siedzibe (bez powtorek przy ponowieniu)
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("kind", String(32), nullable=False),       # wiedza | przepisy | wdrozenie | audyt | zadanie | info
    Column("text", Text, nullable=False),
    Column("plant_id", String(80)),
    Column("url", String(300)),
)
state = Table(
    "state", meta,
    Column("key", String(40), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated", DateTime(timezone=True), nullable=False),
)


def init_db() -> None:
    meta.create_all(engine)


def _mark(keys=(), live_changed: bool = False) -> None:
    global _dirty_live
    with _dirty_lock:
        _dirty_keys.update(keys)
        _dirty_live = _dirty_live or live_changed


def take_dirty() -> tuple[set[tuple[str, str, str]], bool]:
    """Zabiera liste zmian do zapisu w trwalej kopii (po bledzie zapisu - give_back_dirty)."""
    global _dirty_keys, _dirty_live
    with _dirty_lock:
        keys, lv = _dirty_keys, _dirty_live
        _dirty_keys, _dirty_live = set(), False
    return keys, lv


def give_back_dirty(keys, live_changed: bool) -> None:
    _mark(keys, live_changed)


def pending_changes() -> int:
    with _dirty_lock:
        return len(_dirty_keys) + (1 if _dirty_live else 0)


def mark_all_dirty() -> None:
    """Cala zawartosc SQLite do zapisania (pierwsze podlaczenie pustej trwalej bazy)."""
    with engine.connect() as c:
        keys = [(r.day, r.kind, r.key) for r in c.execute(select(counters.c.day, counters.c.kind, counters.c.key))]
        has_live = bool(c.execute(select(func.count()).select_from(live)).scalar())
    _mark(keys, has_live)


def counter_rows(keys) -> list[dict]:
    """Aktualne wartosci wskazanych licznikow."""
    keys = list(keys)
    out = []
    with engine.connect() as c:
        for i in range(0, len(keys), 300):
            part = set(keys[i:i + 300])
            days = {k[0] for k in part}
            q = select(counters).where(counters.c.day.in_(days))
            out += [dict(r._mapping) for r in c.execute(q) if (r.day, r.kind, r.key) in part]
    return out


def live_rows() -> list[dict]:
    with engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(select(live).order_by(live.c.id))]
    for r in rows:
        r["ts"] = _utc(r["ts"])
    return rows


def replace_all(counter_list: list[dict], live_list: list[dict]) -> None:
    """Zawartosc SQLite zastapiona danymi z trwalej kopii (start serwera)."""
    with engine.begin() as c:
        c.execute(delete(counters))
        c.execute(delete(live))
        for i in range(0, len(counter_list), 500):
            c.execute(insert(counters), counter_list[i:i + 500])
        if live_list:
            c.execute(insert(live), [{**r, "ts": _utc(r["ts"])} for r in live_list])


def _utc(ts: datetime) -> datetime:
    """SQLite nie trzyma strefy - wszystkie czasy zapisujemy jako UTC."""
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def bump(rows: dict[tuple[str, str, str], int]) -> None:
    """Dodaje do licznikow: {(dzien, rodzaj, klucz): ile}."""
    if not rows:
        return
    with engine.begin() as c:
        for (day, kind, key), n in rows.items():
            res = c.execute(update(counters).where(counters.c.day == day, counters.c.kind == kind,
                                                   counters.c.key == key).values(n=counters.c.n + n))
            if res.rowcount == 0:
                c.execute(insert(counters).values(day=day, kind=kind, key=key, n=n))
    _mark(rows.keys())


def restore(rows: list[dict]) -> int:
    """Kopia licznikow z Siedziby (stan sprzed restartu) dodana do tego, co przyszlo od startu. Zwraca liczbe wierszy."""
    merged: dict[tuple[str, str, str], int] = {}
    for r in rows:
        try:
            k = (str(r["day"])[:10], str(r["kind"])[:32], str(r.get("key") or "")[:120])
            merged[k] = merged.get(k, 0) + max(0, int(r["n"]))
        except (KeyError, TypeError, ValueError):
            continue
    bump(merged)
    return len(merged)


def rows_since(day: str) -> list[dict]:
    with engine.connect() as c:
        res = c.execute(select(counters).where(counters.c.day >= day, counters.c.day != "archiwum"))
        return [dict(r._mapping) for r in res]


def total(kind: str, day: str | None = None, since: str | None = None) -> int:
    q = select(func.coalesce(func.sum(counters.c.n), 0)).where(counters.c.kind == kind)
    if day:
        q = q.where(counters.c.day == day)
    if since:
        q = q.where(counters.c.day >= since, counters.c.day != "archiwum")
    with engine.connect() as c:
        return int(c.execute(q).scalar() or 0)


def top(kind: str, since: str, limit: int = 8) -> list[tuple[str, int]]:
    q = (select(counters.c.key, func.sum(counters.c.n).label("n")).where(
        counters.c.kind == kind, counters.c.day >= since, counters.c.day != "archiwum", counters.c.key != "")
         .group_by(counters.c.key).order_by(func.sum(counters.c.n).desc()).limit(limit))
    with engine.connect() as c:
        return [(r.key, int(r.n)) for r in c.execute(q)]


def counters_empty() -> bool:
    with engine.connect() as c:
        return not c.execute(select(func.count()).select_from(counters)).scalar()


def set_state(key: str, value) -> None:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    with engine.begin() as c:
        if c.execute(update(state).where(state.c.key == key).values(value=raw, updated=now())).rowcount == 0:
            c.execute(insert(state).values(key=key, value=raw, updated=now()))


def get_state(key: str) -> tuple[dict | None, datetime | None]:
    with engine.connect() as c:
        r = c.execute(select(state).where(state.c.key == key)).first()
    if not r:
        return None, None
    upd = r.updated if r.updated.tzinfo else r.updated.replace(tzinfo=timezone.utc)
    return json.loads(r.value), upd


def add_live(events: list[dict], keep: int) -> int:
    added = 0
    with engine.begin() as c:
        for e in events:
            eid = str(e.get("eid") or "")[:64] or None
            if eid and c.execute(select(live.c.id).where(live.c.eid == eid)).first():
                continue
            try:
                ts = datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00"))
                ts = _utc(ts)
            except ValueError:
                ts = now()
            c.execute(insert(live).values(eid=eid, ts=ts, kind=str(e.get("kind") or "info")[:32],
                                          text=str(e.get("text") or "")[:400],
                                          plant_id=(str(e["plant_id"])[:80] if e.get("plant_id") else None),
                                          url=(str(e["url"])[:300] if str(e.get("url") or "").startswith(("https://", "/"))
                                               else None)))
            added += 1
        ids = [r.id for r in c.execute(select(live.c.id).order_by(live.c.id.desc()).offset(keep))]
        if ids:
            c.execute(delete(live).where(live.c.id.in_(ids)))
    if added or ids:
        _mark(live_changed=True)
    return added


def recent_live(limit: int = 30) -> list[dict]:
    with engine.connect() as c:
        res = c.execute(select(live).order_by(live.c.ts.desc(), live.c.id.desc()).limit(limit))
        out = []
        for r in res:
            d = dict(r._mapping)
            ts = d["ts"] if d["ts"].tzinfo else d["ts"].replace(tzinfo=timezone.utc)
            d["ts"] = ts.isoformat()
            d.pop("id", None)
            out.append(d)
        return out


# ------------------------------------------------------------------ panel admina
def daily(kind: str, since: str) -> dict[str, int]:
    """Suma dzienna jednego rodzaju zdarzen od dnia `since`: {RRRR-MM-DD: liczba}."""
    q = (select(counters.c.day, func.sum(counters.c.n).label("n"))
         .where(counters.c.kind == kind, counters.c.day >= since, counters.c.day != "archiwum")
         .group_by(counters.c.day))
    with engine.connect() as c:
        return {r.day: int(r.n) for r in c.execute(q)}


def all_rows(since: str | None = None) -> list[dict]:
    """Wszystkie liczniki (z archiwum, gdy bez `since`) - do eksportu CSV."""
    q = select(counters).order_by(counters.c.day, counters.c.kind, counters.c.key)
    if since:
        q = q.where(counters.c.day >= since, counters.c.day != "archiwum")
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(q)]


def counts_overview() -> dict:
    with engine.connect() as c:
        return {"liczniki": int(c.execute(select(func.count()).select_from(counters)).scalar() or 0),
                "wpisy": int(c.execute(select(func.count()).select_from(live)).scalar() or 0),
                "najstarszy_dzien": c.execute(select(func.min(counters.c.day))
                                              .where(counters.c.day != "archiwum")).scalar()}


def live_admin(limit: int = 100) -> list[dict]:
    with engine.connect() as c:
        res = c.execute(select(live).order_by(live.c.ts.desc(), live.c.id.desc()).limit(limit))
        out = []
        for r in res:
            d = dict(r._mapping)
            if d["ts"].tzinfo is None:
                d["ts"] = d["ts"].replace(tzinfo=timezone.utc)
            out.append(d)
        return out


def delete_live(entry_id: int) -> bool:
    with engine.begin() as c:
        gone = c.execute(delete(live).where(live.c.id == entry_id)).rowcount > 0
    if gone:
        _mark(live_changed=True)
    return gone
