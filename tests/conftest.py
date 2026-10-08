import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="kwbackend-")
os.environ.update({"SQLITE_PATH": os.path.join(_TMP, "t.db"), "SIEDZIBA_TOKEN": "sekret", "DATABASE_URL": "",
                   "EVENTS_PER_HOUR": "50", "ADMIN_PASSWORD": "tajne-haslo",
                   "SESSION_SECRET": "klucz-testowy"})
