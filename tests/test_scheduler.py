import threading

import pytest

from alert_system.jobs import JobFailed, JobInterrupted
from alert_system.scheduler import CURSOR_KEY, Scheduler, retry_delay
from alert_system.state import FAILED, RETRY_SCHEDULED, SKIPPED, SUCCESS, JobStore
from alert_system.timeutil import to_iso
from conftest import Clock, ist, make_job

STOP = threading.Event()


class Runner:
    def __init__(self, fail_times=0, exc=JobFailed):
        self.calls = []
        self.fail_times = fail_times
        self.exc = exc

    def __call__(self, job, run, attempt, stop):
        self.calls.append((run.id, attempt))
        if len(self.calls) <= self.fail_times:
            raise self.exc("boom", exit_code=1) if issubclass(self.exc, JobFailed) else self.exc("boom")


def build(settings, clock, runners, **kw):
    store = JobStore(settings.scheduler_db)
    return Scheduler(settings, store, runners, clock=clock, **kw), store


def drain(sched, clock, max_steps=100):
    for _ in range(max_steps):
        if not sched.run_next(STOP):
            return


def test_scheduled_job_runs_once_at_its_time(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"])])
    clock = Clock(ist(2026, 10, 19, 9, 59))
    runner = Runner()
    sched, store = build(s, clock, {"a": runner})
    sched.materialise()  # initialises cursor
    clock.advance(minutes=1, seconds=5)
    assert len(sched.materialise()) == 1
    assert sched.materialise() == []  # same window: nothing new
    drain(sched, clock)
    assert len(runner.calls) == 1
    assert store.runs("a")[0].status == SUCCESS


def test_restart_does_not_duplicate_occurrence(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"])])
    clock = Clock(ist(2026, 10, 19, 9, 59))
    sched, store = build(s, clock, {"a": Runner()})
    sched.materialise()
    clock.advance(minutes=2)
    sched.materialise()
    # Simulate a crash before the cursor advanced: rewind it and materialise again
    store.set_meta(CURSOR_KEY, to_iso(ist(2026, 10, 19, 9, 59)))
    sched2, _ = build(s, clock, {"a": Runner()})
    assert sched2.materialise() == []
    assert len(store.runs("a")) == 1


def test_retry_with_backoff_then_success(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"], retry_backoff_seconds=60)])
    clock = Clock(ist(2026, 10, 19, 10, 0, 30))
    runner = Runner(fail_times=2)
    sched, store = build(s, clock, {"a": runner})
    sched.trigger_manual("a")
    assert sched.run_next(STOP)
    run = store.runs("a")[0]
    assert run.status == RETRY_SCHEDULED and run.attempts == 1
    assert not sched.run_next(STOP)  # backoff: not due yet (no tight loop)
    clock.advance(seconds=61)
    sched.run_next(STOP)
    assert store.runs("a")[0].attempts == 2
    clock.advance(seconds=119)
    assert not sched.run_next(STOP)  # second backoff is 120s
    clock.advance(seconds=2)
    sched.run_next(STOP)
    run = store.runs("a")[0]
    assert run.status == SUCCESS and run.attempts == 3


def test_max_attempts_then_failed_and_other_jobs_continue(settings_factory):
    jobs = [make_job("bad", ["0 10 * * *"], max_attempts=10, retry_backoff_seconds=30,
                     retry_backoff_max_seconds=30),
            make_job("good", ["0 10 * * *"])]
    s = settings_factory(jobs=jobs)
    clock = Clock(ist(2026, 10, 19, 10, 0))
    bad, good = Runner(fail_times=999), Runner()
    failures = []
    sched, store = build(s, clock, {"bad": bad, "good": good},
                         on_failure=lambda job, run, attempt, status, err: failures.append((attempt, status)))
    sched.trigger_manual("bad")
    sched.trigger_manual("good")
    for _ in range(30):
        sched.run_next(STOP)
        clock.advance(seconds=31)
    bad_run = store.runs("bad")[0]
    assert bad_run.status == FAILED and bad_run.attempts == 10
    assert len(bad.calls) == 10  # never more than max_attempts
    assert failures[-1] == (10, FAILED)
    assert store.runs("good")[0].status == SUCCESS


def test_unexpected_exception_is_recorded_not_raised(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"])])
    clock = Clock(ist(2026, 10, 19, 10, 0))
    sched, store = build(s, clock, {"a": Runner(fail_times=1, exc=ZeroDivisionError)})
    sched.trigger_manual("a")
    assert sched.run_next(STOP) is True
    run = store.runs("a")[0]
    assert run.status == RETRY_SCHEDULED and "ZeroDivisionError" in run.last_error


def test_offline_defers_without_consuming_attempts(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"], needs_internet=True)], CONNECTIVITY_RETRY_SECONDS=60)
    clock = Clock(ist(2026, 10, 19, 10, 0))
    online = {"v": False}
    runner = Runner()
    sched, store = build(s, clock, {"a": runner}, online=lambda: online["v"])
    sched.trigger_manual("a")
    for _ in range(5):
        sched.run_next(STOP)
        clock.advance(seconds=61)
    run = store.runs("a")[0]
    assert run.attempts == 0 and run.note == "waiting for internet" and runner.calls == []
    online["v"] = True
    sched.run_next(STOP)
    assert store.runs("a")[0].status == SUCCESS


@pytest.mark.parametrize("policy, expected_runs, expected_skipped", [
    ("latest", 1, 2), ("all", 3, 0), ("skip", 0, 3)])
def test_catch_up_policies_after_downtime(settings_factory, policy, expected_runs, expected_skipped):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *", "30 13 * * *", "30 15 * * *"],
                                        catch_up=policy, max_lateness_minutes=720)])
    clock = Clock(ist(2026, 10, 19, 9, 0))
    sched, store = build(s, clock, {"a": Runner()})
    sched.materialise()
    clock.now = ist(2026, 10, 19, 17, 0)  # machine was off 09:00-17:00
    sched.materialise()
    runs = store.runs("a")
    waiting = [r for r in runs if r.status == "PENDING"]
    skipped = [r for r in runs if r.status == SKIPPED]
    if policy == "all":  # older ones get superseded by the newest under supersede=true
        assert len(runs) == 3
    else:
        assert len(waiting) == expected_runs and len(skipped) == expected_skipped
    if policy == "latest":
        assert waiting[0].scheduled_for == to_iso(ist(2026, 10, 19, 15, 30))


def test_catch_up_all_without_supersede_runs_every_missed(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *", "30 13 * * *"], catch_up="all", supersede=False,
                                        max_lateness_minutes=720)])
    clock = Clock(ist(2026, 10, 19, 9, 0))
    runner = Runner()
    sched, store = build(s, clock, {"a": runner})
    sched.materialise()
    clock.now = ist(2026, 10, 19, 17, 0)
    sched.materialise()
    drain(sched, clock)
    assert len(runner.calls) == 2


def test_missed_beyond_max_lateness_is_skipped(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"], catch_up="latest", max_lateness_minutes=60)])
    clock = Clock(ist(2026, 10, 19, 9, 0))
    sched, store = build(s, clock, {"a": Runner()})
    sched.materialise()
    clock.now = ist(2026, 10, 19, 12, 0)
    sched.materialise()
    assert [r.status for r in store.runs("a")] == [SKIPPED]


def test_new_occurrence_supersedes_pending_retry(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *", "30 13 * * *"], retry_backoff_seconds=3600,
                                        retry_backoff_max_seconds=3600)])
    clock = Clock(ist(2026, 10, 19, 9, 59))
    runner = Runner(fail_times=1)
    sched, store = build(s, clock, {"a": runner})
    sched.materialise()
    clock.advance(minutes=1, seconds=1)
    sched.materialise()
    sched.run_next(STOP)  # fails -> RETRY_SCHEDULED 11:00
    clock.now = ist(2026, 10, 19, 13, 30, 5)
    sched.materialise()
    runs = {r.scheduled_for: r for r in store.runs("a")}
    assert runs[to_iso(ist(2026, 10, 19, 10, 0))].status == SKIPPED
    drain(sched, clock)
    assert runs[to_iso(ist(2026, 10, 19, 13, 30))].id == store.runs("a")[0].id
    assert store.runs("a")[0].status == SUCCESS


def test_interrupted_run_recovered_after_power_loss(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"], run_on_startup=False)])
    clock = Clock(ist(2026, 10, 19, 10, 0))
    sched, store = build(s, clock, {"a": Runner()})
    run_id = sched.trigger_manual("a")
    store.claim(run_id, clock())  # RUNNING when the power went out
    sched2, _ = build(s, clock, {"a": Runner()})
    sched2.startup()
    run = store.get(run_id)
    assert run.status == RETRY_SCHEDULED and run.attempts == 1
    drain(sched2, clock)
    assert store.get(run_id).status == SUCCESS


def test_shutdown_interrupt_requeues_immediately(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"])])
    clock = Clock(ist(2026, 10, 19, 10, 0))
    failures = []
    sched, store = build(s, clock, {"a": Runner(fail_times=1, exc=JobInterrupted)},
                         on_failure=lambda *a: failures.append(a))
    sched.trigger_manual("a")
    sched.run_next(STOP)
    run = store.runs("a")[0]
    assert run.status == RETRY_SCHEDULED and run.next_attempt_at == to_iso(clock())
    assert failures == []  # no ops alert for a deliberate shutdown


def test_startup_runs_once_per_day(settings_factory):
    s = settings_factory(jobs=[make_job("a", ["0 10 * * *"], run_on_startup=True)])
    clock = Clock(ist(2026, 10, 19, 8, 0))
    sched, store = build(s, clock, {"a": Runner()})
    sched.startup()
    sched.startup()  # service restarted the same day
    assert len(store.runs("a")) == 1
    clock.now = ist(2026, 10, 20, 8, 0)
    sched.startup()
    assert len(store.runs("a")) == 2


def test_retry_delay_is_capped():
    from alert_system.config import JobSpec
    job = JobSpec(id="a", schedules=("0 0 * * *",), builtin="x", retry_backoff_seconds=120,
                  retry_backoff_max_seconds=1800)
    assert [retry_delay(job, n) for n in (1, 2, 3, 4, 5, 9)] == [120, 240, 480, 960, 1800, 1800]
