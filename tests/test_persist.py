"""Trwala kopia w Postgresie/Neon (app/persist.py). W testach "Neona" udaje drugi plik SQLite - logika
(wczytanie przy starcie, zapis tylko zmian, wartosci bezwzgledne, ponowienie po bledzie) jest ta sama."""
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from app import admin, db, main, persist
from app.config import postgres_url, settings

AUTH = {"Authorization": "Bearer sekret"}
UA = {"user-agent": "Mozilla/5.0 test", "content-type": "text/plain"}


def _wipe(engine):
    with engine.begin() as c:
        for t in (db.counters, db.live, db.state):
            if engine.dialect.has_table(c, t.name):
                c.execute(t.delete())


@pytest.fixture()
def neon(monkeypatch):
    """Wlaczona trwala kopia w osobnym pliku SQLite; po tescie wszystko wraca do stanu bez kopii."""
    path = os.path.join(tempfile.mkdtemp(prefix="kwneon-"), "neon.db")
    url = f"sqlite:///{path}"
    monkeypatch.setattr(settings, "persist_url", url)
    monkeypatch.setattr(persist, "_engine", None)
    saved = dict(persist.status)
    persist.status.update(enabled=True, loaded=False, loaded_rows=0, last_ok=None, last_rows=0,
                          error=None, error_at=None)
    _wipe(db.engine)
    db.take_dirty()
    yield create_engine(url)
    persist.status.clear()
    persist.status.update(saved)
    persist._engine = None
    _wipe(db.engine)
    db.take_dirty()


def remote(engine) -> dict:
    with engine.connect() as c:
        return {(r.day, r.kind, r.key): r.n for r in c.execute(select(db.counters))}


def local() -> dict:
    with db.engine.connect() as c:
        return {(r.day, r.kind, r.key): r.n for r in c.execute(select(db.counters))}


def remote_live(engine) -> list[str]:
    with engine.connect() as c:
        return [r.text for r in c.execute(select(db.live).order_by(db.live.c.id))]


def test_postgres_url_variants():
    assert postgres_url("") == ""
    assert postgres_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    neon_url = "postgresql://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
    assert postgres_url(neon_url) == neon_url.replace("postgresql://", "postgresql+psycopg://")
    assert postgres_url(f"psql '{neon_url}'") == neon_url.replace("postgresql://", "postgresql+psycopg://")


def test_first_connect_sends_local_data(neon):
    db.bump({("2026-10-01", "plant_view", "mniszek"): 5, ("archiwum", "plant_view", "mniszek"): 40})
    db.add_live([{"eid": "e1", "ts": "2026-10-01T10:00:00+02:00", "kind": "info", "text": "pierwszy"}], 300)
    assert persist.load() is False                 # pusta trwala baza
    assert persist.status["loaded"]
    assert persist.flush()
    assert remote(neon) == local() and remote(neon)[("archiwum", "plant_view", "mniszek")] == 40
    assert remote_live(neon) == ["pierwszy"]
    assert db.pending_changes() == 0


def test_flush_writes_absolute_values_only_for_changes(neon):
    persist.load()
    db.bump({("2026-10-02", "search", "kaszel"): 2})
    assert persist.flush()
    db.bump({("2026-10-02", "search", "kaszel"): 3})
    assert persist.flush() and persist.status["last_rows"] == 1
    assert remote(neon)[("2026-10-02", "search", "kaszel")] == 5      # 5, nie 2 + 5
    # bez zmian: Neon nie jest w ogole dotykany (zepsuty adres nie przeszkadza)
    persist._engine = create_engine("sqlite:////nie/ma/takiego/katalogu/x.db")
    assert persist.flush() and persist.status["error"] is None


def test_failed_flush_keeps_changes_for_next_try(neon):
    persist.load()
    good = persist.engine()
    persist._engine = create_engine("sqlite:////nie/ma/takiego/katalogu/x.db")
    db.bump({("2026-10-03", "plantid_click", ""): 7})
    assert persist.flush() is False
    assert persist.status["error"] and db.pending_changes() == 1
    persist._engine = good
    assert persist.flush() and persist.status["error"] is None
    assert remote(neon)[("2026-10-03", "plantid_click", "")] == 7


def test_start_loads_from_persistent_copy(neon):
    persist.load()
    db.bump({("2026-10-04", "plant_view", "pokrzywa"): 9})
    db.add_live([{"eid": "e2", "ts": "2026-10-04T08:00:00Z", "kind": "wiedza", "text": "z Neona"}], 300)
    persist.flush()
    _wipe(db.engine)                                # restart Render: dysk pusty
    db.take_dirty()
    persist.status["loaded"] = False
    assert persist.load() is True
    assert local() == {("2026-10-04", "plant_view", "pokrzywa"): 9}
    assert [e["text"] for e in db.recent_live()] == ["z Neona"]
    assert db.recent_live()[0]["ts"].startswith("2026-10-04T08:00:00")


def test_unreachable_database_at_start_disables_writes(neon, monkeypatch):
    persist._engine = create_engine("sqlite:////nie/ma/takiego/katalogu/x.db")
    assert persist.load(attempts=2, wait=0) is False
    assert not persist.status["loaded"] and persist.status["error"]
    db.bump({("2026-10-05", "search", "x"): 1})
    assert persist.flush() is False               # nie nadpisujemy historii w Neonie niepelnymi liczbami


def test_server_lifecycle_with_persistent_copy(neon):
    persist.load()
    db.bump({("archiwum", "plant_view", "dab"): 100})
    persist.flush()
    _wipe(db.engine)
    db.take_dirty()
    persist.status["loaded"] = False
    main._rate.clear()
    main._seen.clear()
    main._cache.clear()
    with TestClient(main.app) as c:                # start: wczytanie z Neona -> kopia z Siedziby niepotrzebna
        r = c.post("/api/siedziba/heartbeat", json={"status": "pracuje", "wpisy": [
            {"eid": "h1", "ts": "2026-10-13T20:00:00Z", "kind": "info", "text": "heartbeat"}]}, headers=AUTH)
        assert r.json()["potrzebna_kopia"] is False
        assert c.post("/api/events", content=json.dumps({"events": [{"k": "plant_view", "v": "dab"}]}),
                      headers=UA).status_code == 204
        admin._fails.clear()
        c.post("/admin/login", content="haslo=tajne-haslo", follow_redirects=False,
               headers={"content-type": "application/x-www-form-urlencoded"})
        page = c.get("/admin").text
        assert "Trwała kopia" in page and "czeka 3 zmian" in page   # 2 liczniki (odslona + gosc) + dziennik
    # zamkniecie serwera = ostatni zapis
    got = remote(neon)
    assert got[("archiwum", "plant_view", "dab")] == 100
    assert sum(n for (d, k, _), n in got.items() if k == "plant_view" and d != "archiwum") == 1
    assert remote_live(neon) == ["heartbeat"]


def test_deleted_live_entry_disappears_from_copy(neon):
    persist.load()
    db.add_live([{"eid": "a", "ts": "2026-10-06T08:00:00Z", "kind": "info", "text": "zostaje"},
                 {"eid": "b", "ts": "2026-10-06T09:00:00Z", "kind": "info", "text": "do usuniecia"}], 300)
    persist.flush()
    victim = next(e["id"] for e in db.live_admin() if e["text"] == "do usuniecia")
    assert db.delete_live(victim)
    assert persist.flush()
    assert remote_live(neon) == ["zostaje"]
