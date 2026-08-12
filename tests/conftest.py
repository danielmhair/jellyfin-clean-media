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

_scratch = tempfile.mkdtemp(prefix="cleanmedia-tests-")

# A throwaway jobs DB, so worker.main's module-level Store()/JobQueue never open
# the real one. Overwrite unconditionally: an inherited value would defeat the
# isolation this file exists to guarantee.
os.environ["CLEANMEDIA_DB"] = os.path.join(_scratch, "test-jobs.db")

# Empty = no rotating file handler, so tests don't append to data/logs/worker.log
# (which the live worker is also writing to).
os.environ.setdefault("CLEANMEDIA_LOG_FILE", "")
