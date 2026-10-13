"""Trwala kopia danych w Postgresie (np. darmowy Neon) - zmienna DATABASE_URL.

Serwer pracuje na lokalnym SQLite (szybko, bez sieci). Postgres jest tylko trwala kopia:
  - przy starcie: liczniki i dziennik sa wczytywane z Postgresa do SQLite (gdy Postgres jest pusty -
    pierwsze podlaczenie - to, co jest w SQLite, idzie do niego, a liczniki odtwarza Siedziba jak dotad),
  - co PERSIST_MINUTES minut (domyslnie 30) zapisywane sa tylko zmienione liczniki (wartosc bezwzgledna,
    wiec ponowienie niczego nie podwaja) i - gdy sie zmienil - dziennik na zywo,
  - przy zamknieciu serwera (restart, usypianie na Render, nowe wdrozenie) - ostatni zapis.
Dlaczego nie Postgres wprost: darmowy Neon ma 100 godzin obliczen (CU-h) na miesiac i usypia po 5 min ciszy.
Heartbeat Siedziby co 5 min nie pozwolilby mu zasnac (~180 CU-h przy 0,25 CU) i baza stanelaby przed koncem
miesiaca. Tu Neon budzi sie najwyzej raz na PERSIST_MINUTES i tylko wtedy, gdy cos sie zmienilo.
Polaczenia: bez puli (NullPool) - nic nie trzyma bazy na nogach, a po uspieniu Neona nie ma martwych polaczen.
prepare_threshold=None - pooler Neona (PgBouncer, adres z "-pooler") nie lubi przygotowanych zapytan.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine, delete, insert, select
from sqlalchemy.pool import NullPool

from app import db
from app.config import settings

log = logging.getLogger("kwiatownik.persist")

status: dict = {
    "enabled": bool(settings.persist_url),
    "loaded": False,          # wczytano przy starcie - dopiero wtedy wolno zapisywac (inaczej nadpisalibysmy historie)
    "loaded_rows": 0,
    "last_ok": None,          # czas ostatniego udanego zapisu (UTC)
    "last_rows": 0,
    "error": None,
    "error_at": None,
}
_engine = None


def engine():
    global _engine
    if _engine is None:
        args = {}
        if settings.persist_url.startswith("postgresql"):
            args = {"prepare_threshold": None, "connect_timeout": 20, "application_name": "kwiatownik-backend"}
        _engine = create_engine(settings.persist_url, poolclass=NullPool, connect_args=args)
    return _engine


def host() -> str:
    """Host bazy do panelu admina (bez hasla)."""
    try:
        return engine().url.host or engine().url.database or ""
    except Exception:  # noqa: BLE001
        return ""


def _fail(where: str, exc: Exception) -> None:
    msg = f"{where}: {type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"
    status.update(error=msg, error_at=datetime.now(timezone.utc))
    log.warning("trwala kopia - %s", msg)


def load(attempts: int = 3, wait: float = 3.0) -> bool:
    """Start serwera: Postgres -> SQLite. Zwraca True, gdy w Postgresie byly liczniki."""
    if not status["enabled"]:
        return False
    for i in range(attempts):
        try:
            db.meta.create_all(engine(), tables=[db.counters, db.live])
            with engine().connect() as c:
                counters = [dict(r._mapping) for r in c.execute(select(db.counters))]
                live = [dict(r._mapping) for r in c.execute(select(db.live).order_by(db.live.c.id))]
            if counters:
                db.replace_all(counters, live)
            else:
                db.mark_all_dirty()          # pusta trwala baza: wszystko, co jest lokalnie, pojdzie do niej
            status.update(loaded=True, loaded_rows=len(counters), error=None, error_at=None)
            log.info("trwala kopia: wczytano %s licznikow i %s wpisow", len(counters), len(live))
            return bool(counters)
        except Exception as exc:  # noqa: BLE001
            _fail("wczytanie przy starcie", exc)
            if i + 1 < attempts:
                time.sleep(wait * (i + 1))
    return False


def _upsert(conn, rows: list[dict]) -> None:
    name = conn.dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        raise RuntimeError(f"nieobslugiwana baza: {name}")
    for i in range(0, len(rows), 500):
        stmt = dialect_insert(db.counters).values(rows[i:i + 500])
        conn.execute(stmt.on_conflict_do_update(index_elements=["day", "kind", "key"], set_={"n": stmt.excluded.n}))


def flush() -> bool:
    """SQLite -> Postgres: zmienione liczniki i dziennik. False = blad (zmiany czekaja na nastepna probe)."""
    if not status["enabled"] or not status["loaded"]:
        return False
    keys, live_changed = db.take_dirty()
    if not keys and not live_changed:
        return True                          # nic nowego - Neon moze spac
    try:
        rows = db.counter_rows(keys)
        with engine().begin() as c:
            if rows:
                _upsert(c, rows)
            if live_changed:
                c.execute(delete(db.live))
                lrows = db.live_rows()
                if lrows:
                    c.execute(insert(db.live), lrows)
        status.update(last_ok=datetime.now(timezone.utc), last_rows=len(rows), error=None, error_at=None)
        return True
    except Exception as exc:  # noqa: BLE001
        db.give_back_dirty(keys, live_changed)
        _fail("zapis", exc)
        return False


async def loop() -> None:
    """Zapis co PERSIST_MINUTES minut (zadanie w tle uruchamiane w lifespan)."""
    while True:
        await asyncio.sleep(settings.persist_minutes * 60)
        await asyncio.to_thread(flush)
