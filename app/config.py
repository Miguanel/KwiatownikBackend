"""Ustawienia backendu (zmienne srodowiskowe - na Render w panelu albo w render.yaml)."""
import os


def _list(name: str, default: str) -> list[str]:
    return [x.strip().rstrip("/") for x in os.getenv(name, default).split(",") if x.strip()]


def postgres_url(raw: str) -> str:
    """Adres Postgresa z panelu Neon / Supabase -> adres SQLAlchemy z psycopg 3. Przyjmuje tez wklejona
    cala linie `psql 'postgresql://...'` z panelu Neon."""
    url = raw.strip()
    if url.startswith("psql "):
        url = url[5:].strip()
    url = url.strip("'\"")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings:
    def __init__(self) -> None:
        # Baza robocza: zawsze SQLite w pliku (szybki, lokalny). Na darmowym Render plik znika przy restarcie.
        self.database_url = "sqlite:///" + os.getenv("SQLITE_PATH", "data/backend.db")
        # Trwala kopia (opcjonalnie): Postgres, np. darmowy Neon. Przy starcie dane sa z niej wczytywane do SQLite,
        # a zmiany zapisywane do niej co PERSIST_MINUTES minut i przy zamknieciu serwera. Dzieki temu darmowy
        # Neon moze usypiac (5 min bez ruchu) i nie zuzywa limitu godzin obliczen na kazdy heartbeat Siedziby.
        # Bez DATABASE_URL: liczniki po restarcie odtwarza Siedziba (restore).
        self.persist_url = postgres_url(os.getenv("DATABASE_URL", ""))
        self.persist_minutes = max(1, int(os.getenv("PERSIST_MINUTES", "30")))
        # strony, ktore moga czytac /api/live i /api/stats (CORS)
        self.allowed_origins = _list("ALLOWED_ORIGINS", "https://kwiatownik.onrender.com,http://localhost:5000,"
                                                       "http://127.0.0.1:5000,http://localhost:8000")
        self.siedziba_token = os.getenv("SIEDZIBA_TOKEN", "").strip()   # haslo Siedziby (Bearer)
        self.events_per_hour = int(os.getenv("EVENTS_PER_HOUR", "400"))  # limit zdarzen z jednej przegladarki
        self.alive_minutes = int(os.getenv("SIEDZIBA_ALIVE_MINUTES", "15"))  # Siedziba "zyje", gdy odezwala sie niedawno
        self.live_keep = int(os.getenv("LIVE_EVENTS_KEEP", "300"))      # ile wpisow dziennika na zywo trzymac
        self.timezone = os.getenv("TZ_NAME", "Europe/Warsaw")
        # panel admina (/admin): bez ADMIN_PASSWORD panel jest wylaczony
        self.admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
        # klucz podpisu ciasteczka sesji; bez niego losowy przy starcie (restart = ponowne logowanie)
        self.session_secret = os.getenv("SESSION_SECRET", "").strip()
        self.admin_session_days = int(os.getenv("ADMIN_SESSION_DAYS", "7"))
        self.site_url = os.getenv("SITE_URL", "https://kwiatownik.onrender.com").rstrip("/")


settings = Settings()
