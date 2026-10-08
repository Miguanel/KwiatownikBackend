"""Backend Kwiatownika: zdarzenia bez ciasteczek, limity, statystyki, stan Siedziby, kopia licznikow."""
import json

import pytest
from fastapi.testclient import TestClient

from app import db, main

AUTH = {"Authorization": "Bearer sekret"}
UA = {"user-agent": "Mozilla/5.0 test", "content-type": "text/plain"}


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        with db.engine.begin() as conn:
            conn.execute(db.counters.delete())
            conn.execute(db.live.delete())
            conn.execute(db.state.delete())
        db.set_state("boot", {"boot": main.BOOT_ID, "restored": False})
        main._rate.clear()
        main._seen.clear()
        main._cache.clear()
        yield c


def send(client, events, headers=UA):
    return client.post("/api/events", content=json.dumps({"events": events}), headers=headers)


def test_ping_and_cors(client):
    r = client.get("/api/ping", headers={"Origin": "https://kwiatownik.onrender.com"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["boot"] == main.BOOT_ID
    assert r.headers["access-control-allow-origin"] == "https://kwiatownik.onrender.com"
    r = client.get("/api/ping", headers={"Origin": "https://obca.example"})
    assert "access-control-allow-origin" not in r.headers


def test_events_counted_and_cleaned(client):
    r = send(client, [{"k": "plantid_click"}, {"k": "plantid_click"}, {"k": "plant_view", "v": "Krwawnik"},
                      {"k": "plant_view", "v": "../etc/passwd"}, {"k": "plant_section", "v": "krwawnik:Wiedza z sieci (12)"},
                      {"k": "search", "v": "  Kaszel   NA NOC " + "x" * 80}, {"k": "nieznany", "v": "1"},
                      {"k": "plantid_result", "v": "Achillea millefolium"}])
    assert r.status_code == 204
    rows = {(x["kind"], x["key"]): x["n"] for x in db.rows_since("2000-01-01")}
    assert rows[("plantid_click", "")] == 2 and rows[("plant_view", "krwawnik")] == 1
    assert ("plant_view", "../etc/passwd") not in rows and not any(k == "nieznany" for k, _ in rows)
    assert rows[("plant_section", "krwawnik:wiedza_z_sieci_12")] == 1
    assert ("search", "kaszel na noc " + "x" * 26) in rows
    assert rows[("visitors", "")] == 1
    send(client, [{"k": "plant_view", "v": "krwawnik"}])           # ta sama przegladarka - jeden gosc
    assert db.total("visitors") == 1 and db.total("plant_view") == 2


def test_bots_bad_json_and_rate_limit(client):
    assert send(client, [{"k": "plantid_click"}], {"user-agent": "Googlebot/2.1"}).status_code == 204
    assert db.total("plantid_click") == 0
    assert client.post("/api/events", content="nie json", headers=UA).status_code == 400
    for _ in range(3):
        send(client, [{"k": "plantid_click"}] * 20)
    assert db.total("plantid_click") == 40                          # limit 50/h: trzecia paczka odrzucona


def test_public_stats(client):
    send(client, [{"k": "plantid_click"}, {"k": "plant_view", "v": "bez_czarny"}, {"k": "plant_view", "v": "bez_czarny"},
                  {"k": "plant_view", "v": "lipa"}, {"k": "plantid_result", "v": "Sambucus nigra"}])
    s = client.get("/api/stats/public").json()
    assert s["plantid"] == {"razem": 1, "dzis": 1} and s["rosliny_odslony"]["dzis"] == 3
    assert s["najczesciej_czytane"][0] == {"id": "bez_czarny", "n": 2}
    assert s["najczesciej_rozpoznawane"] == [{"nazwa": "Sambucus nigra", "n": 1}] and s["goscie"]["dzis"] == 1


def test_siedziba_heartbeat_live_and_auth(client):
    assert client.post("/api/siedziba/heartbeat", json={}).status_code == 401
    assert client.post("/api/siedziba/heartbeat", json={}, headers={"Authorization": "Bearer zly"}).status_code == 401
    body = {"status": "pracuje", "zadania": [{"opis": "zbiera wiedze o: Kasztanowiec", "od": "2026-10-08T18:00:00Z"}],
            "wpisy": [{"eid": "job:1", "ts": "2026-10-08T18:05:00+00:00", "kind": "wiedza",
                       "text": "Zebrano 14 informacji o kasztanowcu (zh, uk)", "plant_id": "kasztanowiec",
                       "url": "/plant/kasztanowiec/"},
                      {"eid": "job:2", "kind": "x", "text": "zly link", "url": "javascript:alert(1)"}]}
    r = client.post("/api/siedziba/heartbeat", json=body, headers=AUTH).json()
    assert r["dodano_wpisow"] == 2 and r["potrzebna_kopia"] is True and r["boot"] == main.BOOT_ID
    assert client.post("/api/siedziba/heartbeat", json=body, headers=AUTH).json()["dodano_wpisow"] == 0  # bez powtorek
    live = client.get("/api/live").json()
    assert live["siedziba"]["zyje"] and live["siedziba"]["status"] == "pracuje"
    texts = {w["eid"]: w for w in live["wpisy"]}
    assert texts["job:1"]["url"] == "/plant/kasztanowiec/" and texts["job:2"]["url"] is None


def test_restore_adds_once_and_counters_export(client):
    send(client, [{"k": "plant_view", "v": "lipa"}])                # po restarcie przyszlo 1 zdarzenie
    rows = [{"day": "archiwum", "kind": "plant_view", "key": "lipa", "n": 40},
            {"day": main.today(), "kind": "plant_view", "key": "lipa", "n": 5},
            {"day": main.today(), "kind": "plantid_click", "key": "", "n": 7}]
    assert client.post("/api/siedziba/restore", json={"wiersze": rows}, headers=AUTH).json()["wiersze"] == 3
    assert db.total("plant_view") == 46 and db.total("plantid_click") == 7
    assert client.post("/api/siedziba/restore", json={"wiersze": rows}, headers=AUTH).status_code == 409
    assert client.post("/api/siedziba/heartbeat", json={}, headers=AUTH).json()["potrzebna_kopia"] is False
    got = client.get(f"/api/siedziba/counters?since={main.today()}", headers=AUTH).json()["wiersze"]
    assert {(r["kind"], r["key"], r["n"]) for r in got} == {("plant_view", "lipa", 6), ("plantid_click", "", 7),
                                                            ("visitors", "", 1)}
    assert client.get("/api/siedziba/counters?since=zle", headers=AUTH).status_code == 400
