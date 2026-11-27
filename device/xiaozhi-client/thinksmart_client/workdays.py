"""Which days are workdays, by the national holiday schedule: a public holiday is off, a weekend day
worked in exchange (调休) counts, and a day the table doesn't list goes by Monday to Friday. The same
rule as the home page's reminders and the morning wake.

The table is the one those two use (lunar_python's HolidayUtil, vendored on the device). Only its data
string is read, 18 characters a day: importing the package costs this process, which is playing sound,
half a second and 9 MB. A new year's table is picked up without a restart.
"""
import datetime
import json
import logging
import pathlib
import re
import time

log = logging.getLogger(__name__)

TABLE = pathlib.Path('/opt/thinksmart-addons/vendor/lunar_python/util/HolidayUtil.py')
EXTRA = pathlib.Path('/var/lib/thinksmart-home/holidays_extra.json')
DATA = re.compile(r'__DATA = "([^"]+)"')
RECORD = 18   # YYYYMMDD, which holiday, 0 worked / 1 off, the holiday's own date
CHECK = 60    # seconds between looks at whether the file changed


def read_extra(path):
    """Optional JSON overlay {YYYY-MM-DD: bool} or {'workdays': [...], 'holidays': [...]}."""
    raw = json.loads(path.read_text('utf-8'))
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, val in (('workdays', True), ('worked', True), ('holidays', False), ('off', False)):
        items = raw.get(k)
        if isinstance(items, list):
            for ds in items:
                try:
                    out[datetime.date.fromisoformat(str(ds).strip()[:10])] = val
                except ValueError:
                    pass
    for k, v in raw.items():
        if len(k) == 10 and k[4] == '-' and k[7] == '-':
            try:
                d = datetime.date.fromisoformat(k)
            except ValueError:
                continue
            if isinstance(v, bool):
                out[d] = v
            elif isinstance(v, str):
                s = v.strip().lower()
                if s in ('0', 'work', 'workday', 'true'):
                    out[d] = True
                elif s in ('1', 'off', 'holiday', 'false'):
                    out[d] = False
            elif isinstance(v, dict) and 'work' in v:
                out[d] = bool(v['work'])
    return out


def read(path):
    """{date: worked} from HolidayUtil.py. Raises OSError, or ValueError when it isn't that table."""
    found = DATA.search(path.read_text('utf-8'))
    data = found.group(1) if found else ''
    if not data or len(data) % RECORD:
        raise ValueError('not the holiday table')
    days = {}
    for i in range(0, len(data), RECORD):
        record = data[i:i + RECORD]
        if record[9] not in '01':
            raise ValueError(f'neither worked nor off: {record}')
        days[datetime.date(int(record[:4]), int(record[4:6]), int(record[6:8]))] = record[9] == '0'
    return days


class Workdays:
    def __init__(self, path=TABLE, extra_path=EXTRA):
        self.path = pathlib.Path(path)
        self.extra_path = pathlib.Path(extra_path) if extra_path else None
        self.days = {}
        self.extra_days = {}
        self.stamp = None       # of the file the days are from
        self.extra_stamp = None
        self.next_look = 0.0    # monotonic
        self.trouble = ''       # said once

    def __call__(self, date):
        self._look()
        if date in self.extra_days:
            return self.extra_days[date]
        worked = self.days.get(date)
        return date.isoweekday() <= 5 if worked is None else worked

    def _look(self):
        now = time.monotonic()
        if now < self.next_look:
            return
        self.next_look = now + CHECK
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
            if stamp != self.stamp:
                self.stamp = stamp   # half written: its time changes again, and it is read again then
                self.days = read(self.path)
                self.trouble = ''
                log.info('holiday table: %d days, up to %s', len(self.days), max(self.days))
        except (OSError, ValueError) as e:   # what was read before stays; nothing yet: Monday to Friday
            if str(e) != self.trouble:
                self.trouble = str(e)
                log.warning('no holiday table (%s): %s', e, 'the one read before' if self.days else 'Monday to Friday')
        if self.extra_path is not None:
            try:
                est = self.extra_path.stat()
                estamp = (est.st_mtime_ns, est.st_size, est.st_ino)
                if estamp != self.extra_stamp:
                    self.extra_stamp = estamp
                    self.extra_days = read_extra(self.extra_path)
            except FileNotFoundError:
                self.extra_stamp = None
                self.extra_days = {}
            except (OSError, ValueError):
                pass


is_workday = Workdays()
