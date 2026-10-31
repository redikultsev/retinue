"""Scheduler: reminders in the owner's time, checked by code — the weekday, the past, a repeat."""

from datetime import datetime, timezone

from retinue.protocol import Store
from retinue.scheduler import ID_NOTE, Scheduler

NOW = datetime(2026, 10, 7, 14, 5, tzinfo=timezone.utc).timestamp()  # Wednesday, 14:05 UTC
FRIDAY_18 = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc).timestamp()  # Friday, 18:00 UTC


def make(tmp_path):
    return Scheduler(Store(str(tmp_path / "r.sqlite")).db)


def test_a_reminder_is_set_in_the_owners_time(tmp_path):
    jobs = make(tmp_path)
    assert jobs.add("позвонить Х", "2026-10-09T18:00", "Friday", NOW) == (
        True, f"Set: Fri 9 October, 18:00 UTC — позвонить Х [id 1]. {ID_NOTE}")
    assert "#" not in ID_NOTE and "do not tell it" in ID_NOTE, "the id is not a label the owner hears"
    (job,) = jobs.reminders()
    assert (job.kind, job.local, job.tz, job.due, job.status) == ("reminder", "2026-10-09T18:00", "UTC",
                                                                  FRIDAY_18, "active")
    assert jobs.add("Позвонить   х", "2026-10-09 18:00", "пт", NOW) == (
        True, f"Already set: Fri 9 October, 18:00 UTC — позвонить Х [id 1]. {ID_NOTE}"), "the same words at the same time: no second"
    assert jobs.add("позвонить Х", "2026-10-09T19:00", "", NOW)[0] and len(jobs.reminders()) == 2, \
        "another time is another reminder; the weekday may be left out"
    assert Scheduler(jobs.db).reminders()[0].text == "позвонить Х", "kept in the router's file"


def test_code_checks_the_weekday_and_the_past(tmp_path):
    jobs = make(tmp_path)
    assert jobs.add("позвонить Х", "2026-10-09T18:00", "Thursday", NOW) == (
        False, "Not set: 2026-10-09 is a Friday, not a Thursday. Check the date.")
    assert jobs.add("позвонить Х", "2026-10-07T14:00", "среда", NOW) == (
        False, "Not set: Wed 7 October, 14:00 UTC has already passed. It is now 2026-10-07 14:05 UTC, Wednesday.")
    ok, text = jobs.add("позвонить Х", "в пятницу", "пятница", NOW)
    assert not ok and "2026-10-09T18:00" in text and "UTC" in text
    assert jobs.add("позвонить Х", "2026-10-09T18:00", "завтра", NOW) == (False, "Not set: «завтра» is not a day of the week.")
    assert jobs.add("  ", "2026-10-09T18:00", "пт", NOW) == (False, "Not set: the reminder has no text.")
    assert jobs.reminders() == []


def test_list_move_cancel(tmp_path):
    jobs = make(tmp_path)
    assert jobs.listing() == "No active reminders."
    jobs.add("позвонить Х", "2026-10-09T18:00", "пт", NOW)
    jobs.add("купить хлеб", "2026-10-08T09:30", "чт", NOW)
    assert jobs.listing() == ("Active reminders, nearest first:\n"
                              "Thu 8 October, 09:30 UTC — купить хлеб [id 2]\n"
                              f"Fri 9 October, 18:00 UTC — позвонить Х [id 1]\n{ID_NOTE}")
    assert jobs.move(1, "2026-10-10T11:00", "Saturday", NOW) == (True, "Moved: Sat 10 October, 11:00 UTC — позвонить Х [id 1].")
    assert jobs.get(1).local == "2026-10-10T11:00"
    assert jobs.move(1, "2026-10-10T11:00", "пт", NOW) == (
        False, "Not moved: 2026-10-10 is a Saturday, not a Friday. Check the date.")
    assert jobs.cancel(2) == (True, "Cancelled: Thu 8 October, 09:30 UTC — купить хлеб [id 2].")
    assert jobs.cancel(2) == (False, "No active reminder with id 2. The list is list_reminders.")
    assert jobs.move(99, "2026-10-10T11:00", "", NOW)[0] is False
    assert [j.id for j in jobs.reminders()] == [1]


def test_a_long_job_taken_by_a_process_that_died_runs_again(tmp_path):
    jobs = make(tmp_path)
    jobs.add("позвонить Х", "2026-10-07T14:30", "ср", NOW)
    jobs.add_retry({"agent": "assistant", "events": ["telegram:1"]}, NOW + 3600, NOW)
    jobs.ensure_summary(NOW)  # Thursday 09:00
    later = NOW + 86400
    taken = jobs.due(later)
    assert [j.kind for j in taken] == ["reminder", "retry", "summary"] and all(jobs.claim(j, later) for j in taken)
    assert [j.kind for j in jobs.due(later)] == [], "taken: the loop does not take them twice"
    # The router restarts while they are running: each runs the model for seconds or minutes, so each is taken
    # again; nothing is lost silently.
    again = Scheduler(jobs.db)
    assert [j.kind for j in again.due(later)] == ["reminder", "retry", "summary"]
    for job in again.due(later):
        assert again.claim(job, later)
        again.done(job)
    assert Scheduler(jobs.db).due(later) == [], "done is done"
    assert jobs.db.execute("SELECT DISTINCT status FROM jobs").fetchall() == [("sent",)]


def test_the_summary_check_spends_no_reminder_numbers(tmp_path):
    jobs = make(tmp_path)
    for tick in range(50):  # the loop checks every 30 seconds
        jobs.ensure_summary(NOW + tick * 30)
    assert jobs.add("позвонить Х", "2026-10-09T18:00", "пт", NOW)[0]
    assert [row[0] for row in jobs.db.execute("SELECT id FROM jobs ORDER BY id")] == [1, 2]


def test_a_price_drop_is_kept_once_and_sent_at_once(tmp_path):
    """The router keeps an alert before it confirms it to travel-ops: a second look after a restart adds nothing,
    and a kept one waits as a due job until it is sent."""
    jobs = make(tmp_path)
    alert = {"alert_id": 3, "watch_id": "wq3m7k2a", "what": "flights AMS→LIS", "price": 99.0, "currency": "EUR",
             "seen_at": "2026-10-07T10:00:00+00:00"}
    assert jobs.add_price(alert, NOW) and not jobs.add_price(dict(alert), NOW + 300), "one row per alert"
    # travel-ops' numbers start again on a new volume: the same number with another watch or moment is another alert
    assert jobs.add_price(dict(alert, seen_at="2026-11-01T10:00:00+00:00"), NOW)
    (job, _) = jobs.due(NOW)
    assert job.kind == "price" and job.due == NOW and {k: v for k, v in job.data.items() if k != "key"} == alert
    assert jobs.claim(job, NOW) and Scheduler(jobs.db).due(NOW)[0].id == job.id, "a restart before `done` resends"
    assert jobs.reminders() == [] and jobs.listing() == "No active reminders.", "not one of the owner's reminders"


def test_a_repeating_reminder_moves_to_its_next_time_after_it_fires(tmp_path):
    """«every Monday», «on weekdays», «the 1st of every month»: one row, moved to its next wall time when it has
    fired; days missed while the router was down are skipped, not sent in a burst; the 31st falls to a shorter
    month's last day and comes back; cancelling ends the series."""
    jobs = make(tmp_path)
    ok, said = jobs.add("оплатить сервер", "2026-10-31T10:00", "суббота", NOW, "monthly")
    assert ok and "(every month)" in said
    assert jobs.add("x", "2026-10-09T18:00", "пт", NOW, "hourly")[0] is False
    assert "weekend" in jobs.add("зарядка", "2026-10-10T08:00", "суббота", NOW, "weekdays")[1]
    jobs.add("зарядка", "2026-10-09T08:00", "пятница", NOW, "weekdays")

    def fire(job_id, at):
        job = jobs.get(job_id)
        assert jobs.claim(job, at)
        jobs.done(jobs.get(job_id), at)
        return jobs.get(job_id)

    pay = jobs.get(1)
    assert [fire(1, pay.due + 60).local, fire(1, jobs.get(1).due + 60).local, fire(1, jobs.get(1).due + 60).local] \
        == ["2026-11-30T10:00", "2026-12-31T10:00", "2027-01-31T10:00"], "the 31st, the 30th, back to the 31st"
    gym = jobs.get(2)
    assert fire(2, gym.due + 60).local == "2026-10-12T08:00", "Friday → Monday: weekends skipped"
    week_later = jobs.get(2).due + 7 * 86400
    assert fire(2, week_later).local == "2026-10-20T08:00", "down for a week: the next one, not seven at once"
    assert jobs.get(2).status == "active" and jobs.fired_since(week_later - 60) == 2, "counted by its last firing"
    assert jobs.cancel(2)[0] and jobs.get(2).status == "cancelled"
    jobs.add("позвонить маме", "2026-10-09T18:00", "пт", NOW)
    once = next(job for job in jobs.reminders() if job.text == "позвонить маме")
    assert fire(once.id, once.due + 60).status == "sent", "a one-off ends"
