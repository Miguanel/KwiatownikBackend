"""Panel admina: logowanie, blokada po zlych haslach, CSRF, widok, usuwanie wpisu, eksport CSV."""
import pytest
from fastapi.testclient import TestClient

from app import admin, db, main
from app.config import settings

AUTH = {"Authorization": "Bearer sekret"}


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        with db.engine.begin() as conn:
            conn.execute(db.counters.delete())
            conn.execute(db.live.delete())
            conn.execute(db.state.delete())
        db.set_state("boot", {"boot": main.BOOT_ID, "restored": False})
        admin._fails.clear()
        main._cache.clear()
        yield c


def login(c, password="tajne-haslo"):
    return c.post("/admin/login", content=f"haslo={password}", follow_redirects=False,
                  headers={"content-type": "application/x-www-form-urlencoded"})


def csrf(c) -> str:
    return admin.csrf_token(c.cookies.get(admin.COOKIE).split(".")[1])


def test_login_page_and_headers(client):
    r = client.get("/admin")
    assert r.status_code == 200 and 'name="haslo"' in r.text
    assert "noindex" in r.headers["x-robots-tag"] and r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_login_with_browser_origin(client):
    r = client.post("/admin/login", content="haslo=tajne-haslo", follow_redirects=False,
                    headers={"content-type": "application/x-www-form-urlencoded", "origin": "http://testserver"})
    assert r.status_code == 303
    assert "same-origin" == client.get("/admin").headers["referrer-policy"]


def test_wrong_password_then_block(client):
    for i in range(admin.FAIL_LIMIT):
        r = login(client, "zle")
        assert r.status_code in (401, 429)
    r = login(client)                                 # nawet dobre haslo - blokada na 15 min
    assert r.status_code == 429 and admin.COOKIE not in r.cookies


def test_login_dashboard_and_logout(client):
    db.bump({(main.today(), "plant_view", "krwawnik"): 5, (main.today(), "plantid_click", ""): 2,
             (main.today(), "plant_section", "krwawnik:legendy"): 3, ("archiwum", "plant_view", "krwawnik"): 40})
    client.post("/api/siedziba/heartbeat", headers=AUTH, json={
        "status": "pracuje", "zadania": 2,
        "wpisy": [{"eid": "a1", "ts": "2026-10-08T10:00:00Z", "kind": "wiedza", "text": "<b>Nowe</b> legendy"}]})
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/admin" in cookie
    page = client.get("/admin?dni=7&m=plant_view")
    assert page.status_code == 200
    t = page.text
    assert "Odsłony roślin" in t and "krwawnik · legendy" in t and "pracuje" in t
    assert "&lt;b&gt;Nowe&lt;/b&gt; legendy" in t                 # tekst z dziennika jest escapowany
    assert "razem 45" in t                                          # archiwum liczy sie do "razem"
    assert "SQLite (ulotny)" in t
    # zly parametr -> domyslne wartosci, bez bledu
    assert client.get("/admin?dni=abc&m=<x>").status_code == 200
    # wylogowanie bez CSRF nic nie robi
    client.post("/admin/logout", content="csrf=zly", follow_redirects=False,
                headers={"content-type": "application/x-www-form-urlencoded"})
    assert 'name="haslo"' not in client.get("/admin").text
    r = client.post("/admin/logout", content=f"csrf={csrf(client)}", follow_redirects=False,
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 303
    client.cookies.clear()
    assert 'name="haslo"' in client.get("/admin").text


def test_forged_and_expired_cookie(client):
    client.cookies.set(admin.COOKIE, "9999999999.abc.zlypodpis", path="/admin")
    assert 'name="haslo"' in client.get("/admin").text
    client.cookies.clear()
    login(client)
    good = client.cookies.get(admin.COOKIE)
    old = settings.admin_password
    try:
        settings.admin_password = "nowe-haslo"           # zmiana hasla uniewaznia sesje
        assert 'name="haslo"' in client.get("/admin").text
    finally:
        settings.admin_password = old
    assert good and 'name="haslo"' not in client.get("/admin").text


def test_delete_entry_requires_csrf_and_origin(client):
    client.post("/api/siedziba/heartbeat", headers=AUTH, json={
        "wpisy": [{"eid": "x1", "ts": "2026-10-08T10:00:00Z", "kind": "info", "text": "do usuniecia"}]})
    entry_id = db.live_admin()[0]["id"]
    form = {"content-type": "application/x-www-form-urlencoded"}
    assert client.post(f"/admin/wpis/{entry_id}/usun", content="csrf=x", headers=form).status_code == 403
    login(client)
    assert client.post(f"/admin/wpis/{entry_id}/usun", content="csrf=x", headers=form).status_code == 403
    bad_origin = {**form, "origin": "https://obca.example"}
    assert client.post(f"/admin/wpis/{entry_id}/usun", content=f"csrf={csrf(client)}",
                       headers=bad_origin).status_code == 403
    r = client.post(f"/admin/wpis/{entry_id}/usun", content=f"csrf={csrf(client)}&wroc=https://zly.example",
                    headers=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert db.live_admin() == [] and client.get("/api/live").json()["wpisy"] == []


def test_export_csv(client):
    assert client.get("/admin/eksport.csv", follow_redirects=False).status_code == 303
    db.bump({(main.today(), "search", "mięta; ogród"): 2, ("2020-01-01", "plant_view", "stara"): 1})
    login(client)
    r = client.get("/admin/eksport.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    body = r.content.decode("utf-8-sig")
    assert body.splitlines()[0] == "dzien;rodzaj;klucz;liczba"
    assert '"mięta; ogród"' in body and "2020-01-01" in body
    assert "2020-01-01" not in client.get("/admin/eksport.csv?dni=7").content.decode("utf-8-sig")


def test_disabled_without_password(client):
    old = settings.admin_password
    try:
        settings.admin_password = ""
        r = client.get("/admin")
        assert r.status_code == 503 and "ADMIN_PASSWORD" in r.text
        assert login(client).status_code == 503
    finally:
        settings.admin_password = old
