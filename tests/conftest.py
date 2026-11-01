import pytest

from taskwatch import job_scan


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """CLI tests must never create a transient systemd timer; tests read the calls instead."""
    calls: list[object] = []
    monkeypatch.setattr(job_scan.Systemd, "wake", lambda self: calls.append(self.root))
    return calls
