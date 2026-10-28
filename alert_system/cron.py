"""Minimal 5-field cron expressions (minute hour day-of-month month day-of-week).

Supports '*', lists, ranges and steps (e.g. '*/15', '1-5', '0,30').
Day-of-week: 0-7 with 0 and 7 = Sunday. When both day fields are restricted
a day matches if either matches (standard cron semantics).
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


class CronError(ValueError):
    pass


def _parse_field(text, lo, hi):
    values = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"bad step in {text!r}")
            step = int(step_text)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"bad range in {text!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = hi
        else:
            raise CronError(f"bad value in {text!r}")
        if start < lo or end > hi or start > end:
            raise CronError(f"value out of range {lo}-{hi} in {text!r}")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expr):
        fields = expr.split()
        if len(fields) != 5:
            raise CronError(f"cron needs 5 fields: {expr!r}")
        self.expr = expr
        parsed = [_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _BOUNDS)]
        self.minutes, self.hours, self.days, self.months, dow = parsed
        self.dows = {d % 7 for d in dow}
        self.dom_any = fields[2] == "*"
        self.dow_any = fields[4] == "*"

    def _day_matches(self, d):
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.days
        dow_ok = (d.isoweekday() % 7) in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def occurrences(self, after, until, tz_name):
        """Fire times t with after < t <= until (aware datetimes), ascending."""
        tz = ZoneInfo(tz_name)
        start_local = after.astimezone(tz)
        end_local = until.astimezone(tz)
        out = []
        day = start_local.date()
        while day <= end_local.date():
            if self._day_matches(day):
                for h in sorted(self.hours):
                    for m in sorted(self.minutes):
                        t = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                        if after < t <= until:
                            out.append(t)
            day += timedelta(days=1)
        return out
