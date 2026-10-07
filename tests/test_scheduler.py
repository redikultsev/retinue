"""Scheduler: reminders in the owner's time, checked by code — the weekday, the past, a repeat."""

from datetime import datetime, timezone

from retinue.protocol import Store
from retinue.scheduler import Scheduler

NOW = datetime(2026, 10, 7, 11, 5, tzinfo=timezone.utc).timestamp()  # Wednesday, 14:05 МСК
FRIDAY_18 = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc).timestamp()  # Friday, 18:00 МСК


def make(tmp_path):
    return Scheduler(Store(str(tmp_path / "r.sqlite")).db)


def test_a_reminder_is_set_in_the_owners_time(tmp_path):
    jobs = make(tmp_path)
    assert jobs.add("позвонить Х", "2026-10-09T18:00", "пятница", NOW) == (
        True, "Поставила #1 · пт 9 октября, 18:00 МСК — позвонить Х.")
    (job,) = jobs.reminders()
    assert (job.kind, job.local, job.tz, job.due, job.status) == ("reminder", "2026-10-09T18:00", "Europe/Moscow",
                                                                  FRIDAY_18, "active")
    assert jobs.add("Позвонить   х", "2026-10-09 18:00", "пт", NOW) == (
        True, "Уже стоит: #1 · пт 9 октября, 18:00 МСК — позвонить Х."), "the same words at the same time: no second"
    assert jobs.add("позвонить Х", "2026-10-09T19:00", "", NOW)[0] and len(jobs.reminders()) == 2, \
        "another time is another reminder; the weekday may be left out"
    assert Scheduler(jobs.db).reminders()[0].text == "позвонить Х", "kept in the router's file"


def test_code_checks_the_weekday_and_the_past(tmp_path):
    jobs = make(tmp_path)
    assert jobs.add("позвонить Х", "2026-10-09T18:00", "четверг", NOW) == (
        False, "Не поставила: 2026-10-09 — пятница, а не четверг. Проверь дату.")
    assert jobs.add("позвонить Х", "2026-10-07T14:00", "среда", NOW) == (
        False, "Не поставила: ср 7 октября, 14:00 МСК уже прошло. Сейчас 2026-10-07 14:05 МСК, среда.")
    ok, text = jobs.add("позвонить Х", "в пятницу", "пятница", NOW)
    assert not ok and "2026-10-09T18:00" in text and "МСК" in text
    assert jobs.add("позвонить Х", "2026-10-09T18:00", "завтра", NOW) == (False, "Не поставила: «завтра» — не день недели.")
    assert jobs.add("  ", "2026-10-09T18:00", "пт", NOW) == (False, "Не поставила: нет текста напоминания.")
    assert jobs.reminders() == []


def test_list_move_cancel(tmp_path):
    jobs = make(tmp_path)
    assert jobs.listing() == "Активных напоминаний нет."
    jobs.add("позвонить Х", "2026-10-09T18:00", "пт", NOW)
    jobs.add("купить хлеб", "2026-10-08T09:30", "чт", NOW)
    assert jobs.listing() == ("Активные напоминания, ближайшие первыми:\n"
                              "#2 · чт 8 октября, 09:30 МСК — купить хлеб\n#1 · пт 9 октября, 18:00 МСК — позвонить Х")
    assert jobs.move(1, "2026-10-10T11:00", "суббота", NOW) == (True, "Перенесла #1 · сб 10 октября, 11:00 МСК — позвонить Х.")
    assert jobs.get(1).local == "2026-10-10T11:00"
    assert jobs.move(1, "2026-10-10T11:00", "пт", NOW) == (
        False, "Не перенесла: 2026-10-10 — суббота, а не пятница. Проверь дату.")
    assert jobs.cancel(2) == (True, "Отменила #2 · чт 8 октября, 09:30 МСК — купить хлеб.")
    assert jobs.cancel(2) == (False, "Нет активного напоминания #2. Список — list_reminders.")
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
