"""Test-suite isolation from a live worker.

Several tests do ``from worker.main import app``, and importing ``worker.main``
runs ``store = Store()`` + ``jobs = JobQueue(store)`` at module load — a live
queue whose recovery re-enqueues and re-runs whatever is in its database. If
that database is the production ``data/cleanmedia.db``, running the suite while
the real worker is up resets its running job and thrashes the queue.

pytest imports this conftest before any test module (hence before
``worker.main``), so setting these env vars here points that module-level Store
at a throwaway database and turns off the rotating file log — the suite can no
longer touch production state no matter what order things import in.
"""

import os
import tempfile
from pathlib import Path

_scratch = tempfile.mkdtemp(prefix="cleanmedia-tests-")

# A throwaway jobs DB, so worker.main's module-level Store()/JobQueue never open
# the real one. Overwrite unconditionally: an inherited value would defeat the
# isolation this file exists to guarantee.
os.environ["CLEANMEDIA_DB"] = os.path.join(_scratch, "test-jobs.db")

# Empty = no rotating file handler, so tests don't append to data/logs/worker.log
# (which the live worker is also writing to).
os.environ.setdefault("CLEANMEDIA_LOG_FILE", "")

# worker.update starts a background GitHub-polling thread at import time;
# without this the suite would hit the real network on every run.
os.environ.setdefault("CLEANMEDIA_UPDATE_CHECK", "0")

# worker.schedule and worker.settings persist to DATA_DIR/*.json, and DATA_DIR
# (worker/store.py) has no env override — so without this, a test that calls
# their real get_*/set_* functions would read/write the real repo's data/
# folder, same class of hazard CLEANMEDIA_DB exists to prevent above.
import worker.schedule as _schedule  # noqa: E402
import worker.settings as _settings  # noqa: E402

_schedule._PATH = Path(_scratch) / "test-schedule.json"
_settings._PATH = Path(_scratch) / "test-settings.json"
