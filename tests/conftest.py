import os
import stat

import pytest

from taskwatch import job_scan


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """CLI tests must never create a transient systemd timer; tests read the calls instead."""
    calls: list[object] = []
    monkeypatch.setattr(job_scan.Systemd, "wake", lambda self: calls.append(self.root))
    return calls


@pytest.fixture(autouse=True, scope="session")
def writable_temp_trees(tmp_path_factory: pytest.TempPathFactory):
    """Review snapshots are read-only (job_text sets 0o444/0o555); pytest cannot delete them.

    Restore write permission on the whole base temp folder when the session ends, before
    pytest's own cleanup, so no run leaves a folder the current user cannot remove.
    """
    yield
    for base, dirs, names in os.walk(tmp_path_factory.getbasetemp()):
        for name in (".", *dirs, *names):
            path = os.path.join(base, name)
            if not os.path.islink(path):
                mode = os.stat(path).st_mode
                os.chmod(path, mode | stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(mode) else 0))
