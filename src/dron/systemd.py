from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from itertools import groupby
import json
import os
from pathlib import Path
import re
import shlex
from subprocess import run, PIPE, Popen
from tempfile import TemporaryDirectory
from typing import Optional, Iterator, Any, Sequence


from .common import (
    IS_SYSTEMD,
    Unit, Body,
    UnitState, State,
    MANAGED_MARKER, is_managed,
    Command,
    TimerSpec,
    logger,
    escape,
)
from .api import (
    When, OnCalendar,
    OnFailureAction,
)


def is_missing_systemd() -> Optional[str]:
    if not IS_SYSTEMD:
        return "running on macos"
    return None


def _systemctl(*args: Path | str) -> list[Path | str]:
    return ['systemctl', '--user', *args]


def managed_header() -> str:
    return f'''
# {MANAGED_MARKER}
# If you do any manual changes, they will be overridden on the next dron run
'''.lstrip()


# TODO how to come up with good implicit job name?
# TODO do we need a special target for dron?
def timer(*, unit_name: str, when: When) -> str:
    spec: TimerSpec
    if isinstance(when, str):
        spec = {'OnCalendar': when}
    else:
        spec = when

    specs = '\n'.join(f'{k}={v}' for k, v in spec.items())

    return f'''
{managed_header()}
[Unit]
Description=Timer for {unit_name} {MANAGED_MARKER}

[Timer]
{specs}

[Install]
WantedBy=timers.target
'''.lstrip()


# TODO add Restart=always and RestartSec?
# TODO allow to pass extra args
def service(
        *,
        unit_name: str,
        command: Command,
        on_failure: Sequence[OnFailureAction],
        **kwargs: str,
) -> str:
    # TODO not sure if something else needs to be escaped for ExecStart??
    # todo systemd-escape? but only can be used for names

    # ok OnFailure is quite annoying since it can't take arguments etc... seems much easier to use ExecStopPost
    # (+ can possibly run on success too that way?)
    # https://unix.stackexchange.com/a/441662/180307
    cmd = escape(command)

    exec_stop_posts = [
        f"ExecStopPost=/bin/sh -c 'if [ $$EXIT_STATUS != 0 ]; then {action}; fi'"
        for action in on_failure
    ]

    sections: dict[str, list[str]] = {}
    sections['[Unit]'] = [f'''
Description=Service for {unit_name} {MANAGED_MARKER}
'''.strip()]

    sections['[Service]'] = [
        f'ExecStart={cmd}',
        *exec_stop_posts,
    ]

    for k, value in kwargs.items():
        # ideally it would have section name
        m = re.search(r'(\[\w+\])(.*)', k)
        if m is not None:
            section = m.group(1)
            key = m.group(2)
        else:
            # 'legacy' behaviour, by default put into [Service]
            section = '[Service]'
            key = k
        if section not in sections:
            sections[section] = []
        sections[section].append(f'{key}={value}')

    res = managed_header()
    for section_name, lines in sections.items():
        res += '\n\n' + '\n'.join([section_name, *lines])
    res += '\n'

    return res


def test_managed() -> None:
    skip_if_no_systemd()
    from . import verify_unit

    assert is_managed(timer(unit_name='whatever', when='daily'))

    custom = '''
[Service]
ExecStart=/bin/echo 123
'''
    verify_unit(unit_name='other.service', body=custom)  # precondition
    assert not is_managed(custom)


def verify_units(pre_units: list[tuple[Unit, Body]]) -> None:
    # ugh. systemd-analyze takes about 0.2 seconds for each unit for some reason
    # oddly enough, in bulk it works just as fast :thinking_face:
    # also doesn't work in parallel (i.e. parallel processes)
    # that ends up with some weird errors trying to connect to socket
    with TemporaryDirectory() as _tdir:
        tdir = Path(_tdir)
        for unit, body in pre_units:
            (tdir / unit).write_text(body)
        res = run(['systemd-analyze', '--user', 'verify', *tdir.glob('*')], stdout=PIPE, stderr=PIPE)
        # ugh. apparently even exit code 0 doesn't guarantee correct output??
        out = res.stdout.decode('utf8')
        err = res.stderr.decode('utf8')
        assert out == '', out
        if err == '':
            return

        err_lines = err.splitlines(keepends=True)
        unique_err_lines = []
        # uhh.. in bulk mode it spams with tons of 'Cannot add dependency job' for some reason
        # I guess it kinda treats everything as dependent on each other??
        # https://github.com/systemd/systemd/blob/b692ad36b99909453cf4f975a346e41d6afc68a0/src/core/transaction.c#L978
        for l in err_lines:
            if l not in unique_err_lines:
                unique_err_lines.append(l)
        err_lines = unique_err_lines

        if len(err_lines) == 0:
            return

        msg = f'failed checking , exit code {res.returncode}'
        logger.error(msg)
        logger.error('systemd-analyze output:')
        for line in err_lines:
            logger.error(line.strip())
        raise RuntimeError(msg)


def test_verify_systemd() -> None:
    skip_if_no_systemd()
    from . import verify_unit

    def fails(body: str) -> None:
        import pytest
        with pytest.raises(Exception):
            verify_unit(unit_name='whatever.service', body=body)

    def ok(body: str) -> None:
        verify_unit(unit_name='ok.service', body=body)

    ok(body='''
[Service]
ExecStart=/bin/echo 123
''')

    from .api import notify
    on_failure = (
        notify.email('test@gmail.com'),
        notify.desktop_notification,
    )
    ok(body=service(unit_name='alala', command='/bin/echo 123', on_failure=on_failure))

    # garbage
    fails(body='fewfewf')

    # no execstart
    fails(body='''
[Service]
StandardOutput=journal
''')

    fails(body='''
[Service]
ExecStart=yes
StandardOutput=baaad
''')


def _sd(s: str) -> str:
    return f'org.freedesktop.systemd1{s}'


class BusManager:
    def __init__(self) -> None:
        from dbus import SessionBus, Interface  # type: ignore[import]
        self.Interface = Interface  # meh

        self.bus = SessionBus()  # note: SystemBus is for system-wide services
        systemd = self.bus.get_object(_sd(''), '/org/freedesktop/systemd1')
        self.manager = Interface(systemd, dbus_interface=_sd('.Manager'))

    def properties(self, u: Unit):
        service_unit = self.manager.GetUnit(u)
        service_proxy = self.bus.get_object(_sd(''), str(service_unit))
        return self.Interface(service_proxy, dbus_interface='org.freedesktop.DBus.Properties')

    @staticmethod  # meh
    def prop(obj, schema: str, name: str):
        return obj.Get(_sd(schema), name)


def systemd_state(*, with_body: bool) -> State:
    bus = BusManager()
    states = bus.manager.ListUnits()  # ok nice, it's basically instant

    for state in states:
        name  = state[0]
        descr = state[1]
        if not is_managed(descr):
            continue

        # todo annoying, this call still takes some time... but whatever ok
        props = bus.properties(name)
        # stale = int(bus.prop(props, '.Unit', 'NeedDaemonReload')) == 1
        unit_file = Path(str(bus.prop(props, '.Unit', 'FragmentPath'))).resolve()
        body = unit_file.read_text() if with_body else None
        yield UnitState(unit_file=unit_file, body=body)


def test_managed_units() -> None:
    skip_if_no_systemd()
    # TODO wonder if i'd be able to use launchd on ci...
    from . import managed_units, cmd_monitor
    from .common import MonParams

    # shouldn't fail at least
    list(managed_units(with_body=True))

    # TODO ugh. doesn't work on circleci, fails with
    # dbus.exceptions.DBusException: org.freedesktop.DBus.Error.BadAddress: Address does not contain a colon
    # todo maybe don't need it anymore with 20.04 circleci?
    if 'CI' not in os.environ:
        cmd_monitor(MonParams(with_success_rate=True, with_command=True))


def skip_if_no_systemd() -> None:
    import pytest
    reason = is_missing_systemd()
    if reason is not None:
        pytest.skip(f'No systemd: {reason}')


class MonitorHelper:
    def __init__(self) -> None:
        import pytz
        self.utc = pytz.utc
        self.utcmax = self.utc.localize(datetime.max)

    def from_usec(self, usec) -> datetime:
        u = int(usec)
        if u == 2 ** 64 - 1: # apparently systemd uses max uint64
            # happens if the job is running ATM?
            return self.utcmax
        else:
            return self.utc.localize(datetime.utcfromtimestamp(u / 10 ** 6))

    @property
    @lru_cache
    def local_tz(self):
        # TODO warning if tzlocal isn't installed?
        try:
            from tzlocal import get_localzone
            return get_localzone()
        except:
            return self.utc


from .common import MonParams
def _cmd_monitor(managed: State, *, params: MonParams):
    logger.debug('starting monitor...')
    # TODO reorder timers and services so timers go before?
    sd = lambda s: f'org.freedesktop.systemd1{s}'

    mon = MonitorHelper()

    UTCNOW = datetime.now(tz=mon.utc)

    bus = BusManager()

    from .common import MonitorEntry, print_monitor
    entries: list[MonitorEntry] = []
    names = sorted(s.unit_file.name for s in managed)
    uname = lambda full: full.split('.')[0]
    for k, _gr in groupby(names, key=uname):
        gr = list(_gr)
        # if timer is None, guess that means the job is always running?
        timer: Optional[str]
        service: str
        if len(gr) == 2:
            [service, timer] = gr
        else:
            assert len(gr) == 1, gr
            [service] = gr
            timer = None

        if timer is not None:
            props = bus.properties(timer)
            cal   = bus.prop(props, '.Timer', 'TimersCalendar')
            last  = bus.prop(props, '.Timer', 'LastTriggerUSec')
            next_ = bus.prop(props, '.Timer', 'NextElapseUSecRealtime')

            schedule = cal[0][1]  # TODO is there a more reliable way to retrieve it??
            # todo not sure if last is really that useful..

            last_dt = mon.from_usec(last)
            next_dt = mon.from_usec(next_)
            nexts = next_dt.astimezone(mon.local_tz).replace(tzinfo=None, microsecond=0).isoformat() # type: ignore[arg-type]

            if next_dt == datetime.max:
                left_delta = timedelta(0)
            else:
                left_delta   = next_dt - UTCNOW
        else:
            left_delta = timedelta(0) # TODO
            last_dt = UTCNOW
            nexts = 'n/a'
            schedule = 'always'

        # TODO maybe format seconds prettier. dunno
        def fmt_delta(d: timedelta) -> str:
            # format to reduce constant countdown...
            ad = abs(d)
            # get rid of microseconds
            ad = ad - timedelta(microseconds=ad.microseconds)

            day    = timedelta(days=1)
            hour   = timedelta(hours=1)
            minute = timedelta(minutes=1)
            gt = False
            if ad > day:
                full_days  = ad // day
                hours = (ad % day) // hour
                ads = f'{full_days}d {hours}h'
                gt = True
            elif ad > minute:
                full_mins  = ad // minute
                ad = timedelta(minutes=full_mins)
                ads = str(ad)
                gt = True
            else:
                # show exact
                ads = str(ad)
            if len(ads) == 7:
                ads = '0' + ads # meh. fix missing leading zero in hours..
            ads = ('>' if gt else '') + ads
            return ads


        left   = f'{str(fmt_delta(left_delta)):<9}'
        if last_dt.timestamp() == 0:
            ago = 'never' # TODO yellow?
        else:
            passed_delta = UTCNOW - last_dt
            ago = str(fmt_delta(passed_delta))
        # TODO instead of hacking microsecond, use 'NOW' or something?

        props = bus.properties(service)
        # TODO some summary too? e.g. how often in failed
        # TODO make defensive?
        exec_start = bus.prop(props, '.Service', 'ExecStart')
        result     = bus.prop(props, '.Service', 'Result')
        command =  ' '.join(map(shlex.quote, exec_start[0][1])) if params.with_command else None
        _pid: Optional[int] = int(bus.prop(props, '.Service', 'MainPID'))
        pid  = None if _pid == 0 else str(_pid)

        if params.with_success_rate:
            rate = _unit_success_rate(service)
            rates = f' {rate:.2f}'
        else:
            rates = ''

        status_ok = result == 'success'
        status = f'{result:<9} {ago:<8}{rates}'

        entries.append(MonitorEntry(
            unit=k,
            status=status,
            left=left,
            next=nexts,
            schedule=schedule,
            command=command,
            pid=pid,
            status_ok=status_ok,
        ))
    print_monitor(entries)


Json = dict[str, Any]
def _unit_logs(unit: Unit) -> Iterator[Json]:
    # TODO so do I need to parse logs to get failure stats? perhaps json would be more reliable
    cmd = f'journalctl --user -u {unit} -o json -t systemd --output-fields UNIT_RESULT,JOB_TYPE,MESSAGE'
    with Popen(cmd.split(), stdout=PIPE) as po:
        stdout = po.stdout; assert stdout is not None
        for line in stdout:
            j = json.loads(line.decode('utf8'))
            # apparently, successful runs aren't getting logged? not sure why
            jt = j.get('JOB_TYPE')
            ur = j.get('UNIT_RESULT')
            # not sure about this..
            yield j


def _unit_success_rate(unit: Unit) -> float:
    started = 0
    failed  = 0
    # TODO not sure how much time it takes to query all journals?
    for j in _unit_logs(unit):
        jt = j.get('JOB_TYPE')
        ur = j.get('UNIT_RESULT')
        if jt is not None:
            assert ur is None
            started += 1
        elif ur is not None:
            assert jt is None
            failed += 1
        else:
            # TODO eh? sometimes jobs also report Succeeded status
            # e.g. syncthing-paranoid
            pass
    if started == 0:
        assert failed == 0, unit
        return 1.0
    success = started - failed
    return success / started


def cmd_past(unit: Unit) -> None:
    mon = MonitorHelper()
    for j in _unit_logs(unit):
        ts = mon.from_usec(j['__REALTIME_TIMESTAMP'])
        msg = j['MESSAGE']
        print(ts.isoformat(), msg)


# used to use this, keeping for now just for the refernce
# def old_systemd_emailer() -> None:
#     user = getpass.getuser()
#     X = textwrap.dedent(f'''
#     [Unit]
#     Description=status email for %i to {user}
#
#     [Service]
#     Type=oneshot
#     ExecStart={SYSTEMD_EMAIL} --to {user} --unit %i --journalctl-args "-o cat"
#     # TODO why these were suggested??
#     # User=nobody
#     # Group=systemd-journal
#     ''')
#
#     write_unit(unit=f'status-email@.service', body=X, prefix=SYSTEMD_USER_DIR)
#     # I guess makes sense to reload here; fairly atomic step
#     _daemon_reload()
