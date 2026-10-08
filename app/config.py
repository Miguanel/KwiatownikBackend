"""Ustawienia backendu (zmienne srodowiskowe - na Render w panelu albo w render.yaml)."""
import os


def _list(name: str, default: str) -> list[str]:
    return [x.strip().rstrip("/") for x in os.getenv(name, default).split(",") if x.strip()]


class Settings:
    def __init__(self) -> None:
        # Postgres (np. darmowy Neon / Supabase) - dane przetrwaja restart. Bez tego: SQLite w pliku, ktory
        # na darmowym Render znika przy kazdym restarcie / uspieniu - wtedy Siedziba odtwarza liczniki (restore).
        url = os.getenv("DATABASE_URL", "").strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        self.database_url = url or "sqlite:///" + os.getenv("SQLITE_PATH", "data/backend.db")
        # strony, ktore moga czytac /api/live i /api/stats (CORS)
        self.allowed_origins = _list("ALLOWED_ORIGINS", "https://kwiatownik.onrender.com,http://localhost:5000,"
                                                       "http://127.0.0.1:5000,http://localhost:8000")
        self.siedziba_token = os.getenv("SIEDZIBA_TOKEN", "").strip()   # haslo Siedziby (Bearer)
        self.events_per_hour = int(os.getenv("EVENTS_PER_HOUR", "400"))  # limit zdarzen z jednej przegladarki
        self.alive_minutes = int(os.getenv("SIEDZIBA_ALIVE_MINUTES", "15"))  # Siedziba "zyje", gdy odezwala sie niedawno
        self.live_keep = int(os.getenv("LIVE_EVENTS_KEEP", "300"))      # ile wpisow dziennika na zywo trzymac
        self.timezone = os.getenv("TZ_NAME", "Europe/Warsaw")


settings = Settings()
