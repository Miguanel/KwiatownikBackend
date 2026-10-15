# Kwiatownik – Backend

Mały serwer (FastAPI) dla statycznej strony [Kwiatownika](https://kwiatownik.onrender.com). Działa na darmowym planie Render.

- **Liczniki** – kliknięcia „Rozpoznaj” (plant.id), odsłony roślin, otwierane rozdziały, przepisy, kliknięte źródła i wyszukiwania. Nie używa ciasteczek i nie zapisuje adresów IP: gość = skrót z solą zmienianą codziennie, trzymaną tylko w pamięci.
- **Keep-alive** – `/api/ping`. Strona pyta co 10 min, gdy ktoś klika po Kwiatowniku, bo darmowy Render usypia usługę po 15 min bez ruchu. Przy życiu trzyma ją też heartbeat Siedziby co 5 min, gdy Siedziba działa.
- Stan Siedziby, dziennik na żywo i statystyki **nie są publiczne** – tylko w panelu `/admin` (dawne `/api/live` i `/api/stats/public` usunięte 2026-10-15).
- **Siedziba** (z tokenem): `/api/siedziba/heartbeat`, `/api/siedziba/counters?since=`, `/api/siedziba/restore`.
- **Panel admina** – `/admin` (hasło `ADMIN_PASSWORD`).

## Panel admina (`/admin`)
- **Stan**: jak długo działa serwer, jaka baza (ulotny SQLite czy trwały Postgres), czy liczniki zostały odtworzone po starcie; czy Siedziba pracuje, kiedy ostatnio się odezwała, status i liczba zadań.
- **Kafelki** (okres 7 / 30 / 90 dni): goście, odsłony roślin, kliknięcia plant.id, otwarte przepisy, kliknięte źródła, wyszukiwania, odsłony stron. Każdy pokazuje: dziś, okres, zmianę względem poprzedniego okresu i sumę od początku. Kliknięcie kafelka zmienia wykres.
- **Wykres dzienny** wybranego licznika z podpowiedzią po najechaniu i tabelą liczb.
- **Rankingi**: rośliny, czytane rozdziały, rośliny rozpoznane przez plant.id, rozpoznane, a brakujące w Kwiatowniku (pomysły na nowe rośliny), wyszukiwania, przepisy, źródła, strony.
- **Dziennik na żywo** – ostatnie 100 wpisów papirusu; wpis można usunąć (znika też ze strony).
- **Eksport CSV** liczników (okres albo całość, separator `;`, otwiera się w Excelu).

Bezpieczeństwo: bez `ADMIN_PASSWORD` panel jest wyłączony. Sesja to podpisane ciasteczko (`SESSION_SECRET`) ważne 7 dni, tylko dla `/admin`, niedostępne dla skryptów. Zmiana hasła wylogowuje wszystkich. Po 5 złych hasłach w 15 min z jednego adresu logowanie jest zablokowane na kwadrans. Akcje mają ochronę CSRF, a strony nie są indeksowane.

## Pamięć na darmowym planie
Dysk darmowej usługi Render znika przy restarcie i uśpieniu, a darmowy Postgres Render wygasa po 30 dniach. Serwer zawsze pracuje na lokalnym SQLite, a trwałość daje jedno z dwóch:
- **Bez dodatkowej usługi**: Siedziba co 5 min pobiera liczniki do swojej bazy, a po restarcie serwera wysyła kopię z powrotem (`restore`, raz na start). Tracisz najwyżej zdarzenia z kilku minut przed restartem i z czasu, gdy Siedziba była wyłączona, a serwer się zrestartował.
- **Neon (zalecane)**: ustaw `DATABASE_URL`. Przy starcie serwer wczytuje liczniki i dziennik z Neona, zapisuje zmiany co `PERSIST_MINUTES` minut (30) oraz przy zamknięciu (restart, uśpienie, wdrożenie). Kopia z Siedziby jest potrzebna tylko raz – przy pierwszym podłączeniu pustej bazy.

### Dlaczego nie zapis do Neona przy każdym zdarzeniu
Darmowy Neon ma **100 godzin obliczeń (CU-h) miesięcznie** na projekt i usypia po 5 min ciszy. Heartbeat Siedziby co 5 min nie pozwoliłby mu zasnąć: ok. 180 CU-h przy najmniejszej maszynie (0,25 CU), więc baza stanęłaby w połowie miesiąca. Przy zapisie co 30 min, i tylko gdy coś się zmieniło, Neon budzi się najwyżej 48 razy na dobę na kilka minut: kilkanaście–kilkadziesiąt CU-h miesięcznie. Połączenia są otwierane tylko na czas zapisu (bez puli), więc nic nie trzyma bazy na nogach.

### Podłączenie Neona
1. Neon → projekt (np. `KwiatownikDataBase`) → **Connect** → gałąź `production`, baza `neondb`, włącz **Connection pooling** → skopiuj adres `postgresql://…-pooler….neon.tech/neondb?sslmode=require&channel_binding=require`.
2. Render → usługa `kwiatownik-backend` → **Environment** → `DATABASE_URL` = ten adres → **Save, rebuild and deploy**. Tabele (`counters`, `live_events`) serwer tworzy sam.
3. Panel `/admin` → karta „Serwer”: „SQLite + trwała kopia w PostgreSQL (…neon.tech)”. Przy pierwszym starcie z pustym Neonem Siedziba w ciągu 5 min wyśle kopię liczników; zapisze się do Neona przy najbliższym zapisie.
4. Region: usługa Render jest we Frankfurcie, więc najlepiej projekt Neon w AWS Europe Central 1 (Frankfurt). Inny region też działa, tylko zapis trwa trochę dłużej.

Adres zawiera hasło – trzymaj go tylko w Environment na Render (i ewentualnie w lokalnym `.env`, który jest w `.gitignore`), nigdy w repozytorium. Gdy Neon jest niedostępny przy starcie, serwer działa dalej na SQLite (z kopią z Siedziby), a w panelu widać ostrzeżenie; zapis do Neona wraca po następnym restarcie.

## Wdrożenie (Blueprint)
1. Wypchnij to repozytorium na GitHub.
2. Render → **New → Blueprint** → wybierz repozytorium → **Apply** (`render.yaml`).
3. W usłudze → **Environment**: hasło do panelu to `ADMIN_PASSWORD` (możesz wpisać własne). Skopiuj `SIEDZIBA_TOKEN` do `.env` Siedziby jako `BACKEND_TOKEN`, a adres usługi wpisz jako `BACKEND_URL`.
4. W stronie statycznej Kwiatownika (Render → Environment) ustaw `KW_BACKEND_URL` na adres usługi, potem zrób ponowny build.

## Lokalnie
    python -m venv venv && venv\Scripts\activate
    pip install -r requirements-dev.txt
    set SIEDZIBA_TOKEN=test
    set ADMIN_PASSWORD=haslo
    uvicorn app.main:app --reload --port 8001
    pytest

Zmienne: `SIEDZIBA_TOKEN`, `ALLOWED_ORIGINS`, `DATABASE_URL` (opcjonalnie, Neon), `PERSIST_MINUTES` (30), `SQLITE_PATH` (data/backend.db), `EVENTS_PER_HOUR` (400), `SIEDZIBA_ALIVE_MINUTES` (15), `LIVE_EVENTS_KEEP` (300), `TZ_NAME` (Europe/Warsaw), `ADMIN_PASSWORD`, `SESSION_SECRET`, `ADMIN_SESSION_DAYS` (7), `SITE_URL` (adres Kwiatownika – link w panelu i w dzienniku).
