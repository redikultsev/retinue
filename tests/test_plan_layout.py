"""The day's layout (`plan_layout`): the window in its own zone whatever the owner's, buffers after meetings,
pauses after a run of tasks that a short task cannot slip into, pieces of 30 to 90 minutes, overdue first — on made-up
shapes of a window (10:00 to 01:00 in a zone of its own): a weekday evening with a meeting, an empty weekend, midnight, a busy invitation he never answered
is not the caller's busy."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from retinue.plan_layout import Hours, Task, lay, left_s, meeting_busy, need_s, ordered, piece_s, window_at as _at, window_of as _of

TUE = date(2026, 11, 3)
H = Hours("Europe/Berlin", time(10, 0), time(1, 0))  # a day that runs past midnight, in a zone of its own


def window_of(day):
    return _of(day, H)


def window_at(now):
    return _at(now, H)


def zone():
    return ZoneInfo(H.zone)


def at(day: date, hm: str) -> float:
    h, m = map(int, hm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=zone()).timestamp()


def hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, zone()).strftime("%H:%M")


def shown(layout) -> list[tuple[str, str, int]]:
    return [(task, hm(start), minutes) for task, start, minutes in sorted(layout.placed, key=lambda p: p[1])]


def test_the_window_runs_past_midnight_in_its_zone_and_switches_when_less_than_an_hour_is_left():
    w = window_of(TUE)
    assert (hm(w.start), hm(w.end)) == ("10:00", "01:00") and w.end - w.start == 15 * 3600
    assert window_at(at(TUE, "09:00")).day == TUE, "before it opens: today's"
    assert window_at(at(TUE, "23:30")).day == TUE
    assert window_at(at(TUE, "00:20")).day == TUE, "00:20: forty minutes of last night's window left — today's"
    assert window_at(at(TUE, "23:55")).day == TUE, "an hour and five minutes left: still tonight's"
    assert window_at(at(TUE, "00:05") + 86400).day == date(2026, 11, 4), "00:05: under an hour — tomorrow's"
    assert window_at(at(TUE, "03:00")).day == TUE, "the night between: the day ahead"


def test_a_weekday_evening_fills_around_a_meeting_with_its_buffer_and_pauses_after_ninety_minutes():
    w = window_of(TUE)
    events = [{"start": at(TUE, "10:00"), "end": at(TUE, "19:00"), "busy": True, "all_day": False},
              {"start": at(TUE, "20:00"), "end": at(TUE, "21:00"), "busy": True, "all_day": False}]
    busy = meeting_busy(events, w.start, w.end)
    tasks = [Task("homework", 90, due=TUE), Task("rent", 15), Task("talk", 180, due=date(2026, 11, 13)),
             Task("bank", 30)]
    out = lay(w, at(TUE, "18:00"), busy, [], tasks)
    assert shown(out) == [("rent", "19:15", 15), ("bank", "19:30", 30), ("homework", "21:15", 90),
                          ("talk", "23:00", 90)], "the buffer after each, the 50 minutes before 20:00 taken by what fits"
    placed = {task: (start, minutes) for task, start, minutes in out.placed}
    assert hm(placed["homework"][0]) >= "19:10", "nothing in the meeting's ten minutes after"
    starts = sorted((s, s + m * 60) for s, m in placed.values())
    run = 0
    for (a, b), prev in zip(starts, [None, *starts]):
        run = run + (b - a) if prev and a - prev[1] < 600 else b - a
        assert run <= 90 * 60, "no run of tasks over ninety minutes"


def test_due_today_goes_first_and_overdue_before_it_even_when_longer():
    w = window_of(TUE)
    tasks = [Task("short", 15, created=1), Task("today", 60, due=TUE), Task("late", 90, due=date(2026, 11, 1))]
    assert [t.id for t in ordered(tasks, w)] == ["late", "today", "short"]
    out = lay(w, at(TUE, "21:30"), [], [], tasks)
    assert [p[0] for p in sorted(out.placed, key=lambda p: p[1])][0] == "late"


def test_a_short_task_cannot_slip_into_a_pause():
    w = window_of(TUE)
    standing = [(at(TUE, "20:00"), at(TUE, "21:30"))]  # a 90-minute piece already in the window
    out = lay(w, at(TUE, "19:55"), [(at(TUE, "19:00"), at(TUE, "20:00"))], standing, [Task("mail", 15)])
    assert shown(out) == [("mail", "21:45", 15)], "not 21:30, against the run; 15 minutes later"


def test_pieces_are_thirty_to_ninety_one_a_window_and_counted_time_never_empties_a_task_with_a_due_date():
    assert [piece_s(n * 60) // 60 for n in (10, 30, 90, 100, 150, 180)] == [15, 30, 90, 60, 90, 90]
    assert need_s(Task("talk", 180, done=180, due=TUE)) == 90 * 60, "half of it stays until Done"
    assert need_s(Task("talk", 180, done=180)) == 30 * 60, "no due date: planned on in half hours, no question"
    assert need_s(Task("x", None)) == 30 * 60
    w = window_of(date(2026, 11, 7))
    out = lay(w, at(date(2026, 11, 7), "09:00"), [], [], [Task("talk", 180)])
    assert shown(out) == [("talk", "10:00", 90)], "one piece a window: the rest is the next plan's"


def test_an_empty_weekend_starts_at_ten_and_nothing_runs_past_one():
    w = window_of(date(2026, 11, 7))
    out = lay(w, at(date(2026, 11, 7), "08:00"), [], [], [Task(str(n), 90) for n in range(12)])
    times = shown(out)
    assert times[0][1] == "10:00" and all(t[1] != "00:00" or t[2] <= 60 for t in times)
    assert max(s + m * 60 for _, s, m in out.placed) <= w.end
    assert out.left, "what does not fit is said, with why"


def test_whole_day_events_and_free_time_keep_no_one_busy():
    w = window_of(TUE)
    events = [{"start": w.start - 36000, "end": w.end, "busy": False, "all_day": True},
              {"start": at(TUE, "12:00"), "end": at(TUE, "13:00"), "busy": False, "all_day": False}]
    assert meeting_busy(events, w.start, w.end) == []


def test_hours_are_two_times_and_a_zone_and_anything_off_is_none():
    assert Hours.of({"zone": "Europe/Berlin", "opens": "10:00", "closes": "01:00"}) == H
    assert Hours.of({"zone": "UTC", "opens": "23:00", "closes": "01:00"}) == Hours("UTC", time(23, 0), time(1, 0))
    assert Hours.of(H.shape()) == H and H.shape() == {"zone": "Europe/Berlin", "opens": "10:00", "closes": "01:00"}
    for off in ({"zone": "Europe/Berlin", "opens": "nine", "closes": "17:00"},
                {"zone": "Mars/Olympus", "opens": "10:00", "closes": "01:00"},
                {"zone": "Europe/Berlin", "opens": "10:00", "closes": "10:00"},
                {"zone": "Europe/Berlin", "opens": "23:30", "closes": "01:00"},
                {"zone": "", "opens": "10:00", "closes": "01:00"},
                {"opens": "10:00", "closes": "01:00"}, "10:00-01:00", None):
        assert Hours.of(off) is None, off
    day = _of(TUE)
    assert (datetime.fromtimestamp(day.start, ZoneInfo("UTC")).hour, day.end - day.start) == (9, 14 * 3600)


def test_what_other_windows_hold_is_not_laid_again():
    w = window_of(TUE)
    assert left_s(Task("talk", 180, planned=90)) == 90 * 60 and left_s(Task("bill", 15, planned=15)) == 0
    out = lay(w, at(TUE, "18:00"), [], [], [Task("bill", 15, planned=15), Task("talk", 180, planned=90)])
    assert shown(out) == [("talk", "18:00", 90)] and not out.left, "the bill is another day's; nothing said about it"


def test_no_piece_ends_after_its_no_later_than_and_a_passed_one_lays_it_anywhere():
    window = window_of(TUE)
    lesson = [(at(TUE, "18:00"), at(TUE, "19:00"))]
    homework = Task("homework", 60, by=at(TUE, "18:00"))
    layout = lay(window, at(TUE, "16:30"), lesson, [], [homework])
    assert shown(layout) == [("homework", "16:30", 60)], "done before the lesson"
    late = lay(window, at(TUE, "17:30"), lesson, [], [homework])
    assert late.placed == [] and late.left == [("homework", "no free 60 min before its deadline")]
    missed = lay(window, at(TUE, "18:30"), lesson, [], [homework])
    assert shown(missed) == [("homework", "19:00", 60)], "its moment passed: laid anyway, not lost"
