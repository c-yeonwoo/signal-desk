"""The admin proof read must stay bounded while its calculations run in background."""

import threading
import time

from signal_desk.api import _ProofJob


def _finished(job: _ProofJob) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        out = job.snapshot()
        if not out["proof_job"]["running"]:
            return out
        time.sleep(0.01)
    raise AssertionError("proof worker did not finish")


def test_proof_job_is_single_flight_and_reports_stage():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def collect(progress):
        calls.append(1)
        progress("accuracy")
        entered.set()
        assert release.wait(2)
        return {"A": {"accuracy": {"ready": True}}}

    job = _ProofJob(collect)
    first = job.snapshot()
    assert first["loading"] is True
    assert entered.wait(2)
    second = job.snapshot()
    assert second["loading"] is True
    assert second["proof_job"]["stage"] == "accuracy"
    assert calls == [1]
    release.set()
    done = _finished(job)
    assert done["A"]["accuracy"]["ready"] is True
    assert done["proof_job"]["stale"] is False
    assert job.snapshot()["A"] == done["A"]
    assert calls == [1]


def test_proof_job_failure_is_visible_without_hot_retry():
    calls = []

    def broken(progress):
        calls.append(1)
        progress("prices")
        raise ValueError("test failure")

    job = _ProofJob(broken)
    job.snapshot()
    done = _finished(job)
    assert done["loading"] is False
    assert done["proof_job"]["error"] == "ValueError"
    assert job.snapshot()["proof_job"]["error"] == "ValueError"
    assert calls == [1]


def test_failed_refresh_keeps_last_result_marked_stale():
    job = _ProofJob(lambda progress: {"A": {"value": 1}})
    job.snapshot()
    assert _finished(job)["A"]["value"] == 1

    def broken(progress):
        raise RuntimeError("refresh failed")

    job.collector = broken
    job.snapshot(force=True)
    failed = _finished(job)
    assert failed["A"]["value"] == 1
    assert failed["proof_job"]["stale"] is True
    assert failed["proof_job"]["error"] == "RuntimeError"
