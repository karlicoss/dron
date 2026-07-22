from __future__ import annotations

import itertools
import json
import os
import plistlib
import re
import shlex
import sys
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from subprocess import PIPE, Popen, check_call, check_output
from tempfile import TemporaryDirectory

from .api import (
    OnCalendar,
    OnFailureAction,
    When,
)
from .common import (
    ALWAYS,
    DRON_UNITS_DIR,
    MANAGED_MARKER,
    Command,
    LaunchdUnitState,
    MonitorEntry,
    MonitorParams,
    State,
    Unit,
    UnitFile,
    is_managed,
    logger,
)

# TODO custom launchd domain?? maybe instead could do dron/ or something?
_LAUNCHD_DOMAIN = f'gui/{os.getuid()}'


# in principle not necessary...
# but makes it much easier to filter out logs & lobs from launchctl dump
DRON_PREFIX = 'dron.'


def _launchctl(*args: Path | str) -> list[Path | str]:
    return ['launchctl', *args]


def _launch_agent(path: str) -> Path:
    # symlink for autostart
    assert path.endswith('.plist'), path  # meh
    assert not Path(path).is_absolute(), path

    LA = Path('~/Library/LaunchAgents').expanduser()
    link = LA / path
    return link


def fqn(name: Unit) -> str:
    return _LAUNCHD_DOMAIN + '/' + DRON_PREFIX + name


def launchctl_load(*, unit_file: UnitFile) -> None:
    # bootstrap is nicer than load
    # load is super defensive, returns code 0 on errors
    check_call(_launchctl('bootstrap', _LAUNCHD_DOMAIN, unit_file))
    _launch_agent(unit_file.name).symlink_to(unit_file)


def launchctl_unload(*, unit: Unit) -> None:
    # bootout is more verbose than unload
    # in addition unload is super defensive, returns code 0 on errors
    check_call(_launchctl('bootout', fqn(unit)))
    _launch_agent(unit + '.plist').unlink()


def launchctl_kickstart(*, unit: Unit) -> None:
    check_call(_launchctl('kickstart', fqn(unit)))


def launchctl_reload(*, unit: Unit, unit_file: UnitFile) -> None:
    # don't think there is a better way?
    launchctl_unload(unit=unit)
    launchctl_load(unit_file=unit_file)


def launchd_wrapper(*, job: str, on_failure: list[str]) -> list[str]:
    return [
        sys.executable,
        '-B',  # do not write byte code, otherwise it shits into dron directory if we're using editable install
        '-m', 'dron.launchd_wrapper',
        *itertools.chain.from_iterable(('--notify', n) for n in on_failure),
        '--job', job,
        '--',
    ]  # fmt: skip


def remove_launchd_wrapper(cmd: str) -> str:
    if ' dron.launchd_wrapper ' not in cmd:
        return cmd
    # uhh... not super reliable, but this is only used for monitor so hopefully fine
    [_, cmd] = cmd.split(' -- ', maxsplit=1)
    return cmd


def _calendar_minutes(*, step: int) -> list[dict[str, int]]:
    assert 0 < step <= 60, step
    return [{'Minute': minute} for minute in range(0, 60, step)]


def _format_calendar_interval(interval: object) -> str:
    entries = [interval] if isinstance(interval, dict) else interval
    assert isinstance(entries, list), interval
    assert len(entries) > 0, interval

    hours: list[int | None] = []
    minutes: list[int | None] = []
    for entry in entries:
        assert isinstance(entry, dict), entry
        assert set(entry) <= {'Hour', 'Minute'}, entry

        hour = entry.get('Hour')
        minute = entry.get('Minute')
        assert hour is None or isinstance(hour, int), entry
        assert minute is None or isinstance(minute, int), entry
        hours.append(hour)
        minutes.append(minute)

    unique_hours = set(hours)
    assert len(unique_hours) == 1, entries
    [hour] = unique_hours

    def format_component(value: int | None) -> str:
        return '*' if value is None else f'{value:02}'

    formatted_minutes = ','.join(format_component(minute) for minute in minutes)
    return f'{format_component(hour)}:{formatted_minutes}'


def plist(
    *,
    unit_name: str,
    command: Command,
    on_failure: Sequence[OnFailureAction],
    when: When | None = None,
) -> str:
    """
    Generate a launchd plist.

    Supported ``when`` values:

    - ``None`` for manual jobs
    - ``always``
    - ``daily``, ``hourly``, and ``minutely``
    - ``HH:MM`` calendar times
    - ``*:0/N`` for 1 to 60-minute wall-clock steps
    - ``*:*:0/N`` for 1 to 3600-second steps, rounded up to whole minutes

    systemd ``TimerSpec`` mappings are unsupported.
    """
    # TODO hmm, kinda mirrors 'escape' method, not sure
    cmd: Sequence[str]
    if isinstance(command, Path):
        cmd = [str(command)]
    elif isinstance(command, str):
        if ' ' not in command:
            cmd = [command]
        else:
            # unquoting and splitting is way trickier than quoting and joining...
            # not sure how to implement it p
            # maybe we just want bash -c in this case, dunno how to implement properly
            raise RuntimeError(command)  # too ambiguous?
    else:  # must be an actual sequence of path-like things
        cmd = tuple(map(str, command))
    del command

    schedule: dict[str, object] = {}
    if when == ALWAYS:
        schedule = {'KeepAlive': True}
    elif when is not None:
        assert isinstance(when, OnCalendar), when

        if when == 'daily':
            schedule = {'StartCalendarInterval': {'Hour': 0, 'Minute': 0}}
        elif when == 'hourly':
            schedule = {'StartCalendarInterval': {'Minute': 0}}
        elif when == 'minutely':
            schedule = {'StartCalendarInterval': {}}
        elif (minute_step_match := re.fullmatch(re.escape('*:0/') + r'(\d+)', when)) is not None:
            minute_step = int(minute_step_match.group(1))
            schedule = {'StartCalendarInterval': _calendar_minutes(step=minute_step)}
        elif (second_step_match := re.fullmatch(re.escape('*:*:0/') + r'(\d+)', when)) is not None:
            seconds = int(second_step_match.group(1))
            assert seconds > 0, when

            # launchd calendars have no seconds field.
            # Round up so the approximation never runs more often than requested.
            minutes = max(1, (seconds + 59) // 60)
            assert minutes <= 60, (unit_name, when, minutes)
            logger.warning(
                f"launchd job '{unit_name}' rounds second-based schedule '{when}' up to a {minutes}-minute wall-clock schedule"
            )
            schedule = {'StartCalendarInterval': _calendar_minutes(step=minutes)}
        elif (time_match := re.fullmatch(r'(\d\d):(\d\d)', when)) is not None:
            schedule = {
                'StartCalendarInterval': {
                    'Hour': int(time_match.group(1)),
                    'Minute': int(time_match.group(2)),
                }
            }

    assert when is None or len(schedule) > 0, unit_name

    # meh.. not sure how to reconcile it better with systemd
    on_failure = [x.replace('--job %n', f'--job {unit_name}') + ' --stdin' for x in on_failure]

    # attempt to set argv[0] properly
    # hmm I was hoping it would make desktop notifications ('background service added' nicer)
    # but even after that it still only shows executable script name. ugh
    # program_argv = (unit_name, *cmd[1:])
    program_argv = (
        *launchd_wrapper(job=unit_name, on_failure=on_failure),
        *cmd,
    )
    del cmd

    # TODO add log file, although mailer is already capturing stdout
    # TODO hmm maybe use the same log file for all dron jobs? would make it easier to rotate?
    properties: dict[str, object] = {
        'Label': DRON_PREFIX + unit_name,
        'ProgramArguments': program_argv,
        **schedule,
        'Comment': MANAGED_MARKER,
    }
    return plistlib.dumps(properties, sort_keys=False).decode()


# Managed plist files are the configuration source of truth.
# launchctl documents its printed output as unstable,
#   so only parse it for runtime fields unavailable on disk.
def launchd_units(*, with_body: bool) -> Iterator[LaunchdUnitState]:
    for unit_file in sorted(DRON_UNITS_DIR.glob('*.plist')):
        raw = unit_file.read_bytes()
        properties = plistlib.loads(raw)

        comment = properties.get('Comment')
        if not isinstance(comment, str):
            continue
        if not is_managed(comment):
            continue

        program_arguments = properties.get('ProgramArguments')
        assert isinstance(program_arguments, list), (unit_file, program_arguments)
        assert all(isinstance(arg, str) for arg in program_arguments), (unit_file, program_arguments)

        start_interval = properties.get('StartInterval')
        if start_interval is not None:
            assert isinstance(start_interval, int), (unit_file, start_interval)
            schedule = f'every {start_interval} seconds'
        elif (calendar_interval := properties.get('StartCalendarInterval')) is not None:
            schedule = _format_calendar_interval(calendar_interval)
        elif properties.get('KeepAlive') is True:
            schedule = 'always'
        else:
            schedule = 'manual'

        yield LaunchdUnitState(
            unit_file=unit_file,
            body=raw.decode() if with_body else None,
            cmdline=tuple(program_arguments),
            last_exit_code=None,
            pid=None,
            schedule=schedule,
        )


def _launchd_runtime(*, unit: Unit) -> tuple[str | None, str | None]:
    output = check_output(_launchctl('print', fqn(unit))).decode()
    last_exit_code = None
    pid = None
    for line in output.splitlines():
        if line.startswith('\tlast exit code = '):
            last_exit_code = line.removeprefix('\tlast exit code = ')
        elif line.startswith('\tpid = '):
            pid = line.removeprefix('\tpid = ')
    return last_exit_code, pid


def launchd_state(*, with_body: bool) -> Iterator[LaunchdUnitState]:
    for state in launchd_units(with_body=with_body):
        last_exit_code, pid = _launchd_runtime(unit=state.unit_file.stem)
        yield LaunchdUnitState(
            unit_file=state.unit_file,
            body=state.body,
            cmdline=state.cmdline,
            last_exit_code=last_exit_code,
            pid=pid,
            schedule=state.schedule,
        )


def verify_unit(*, unit_name: str, body: str) -> None:
    with TemporaryDirectory() as tdir:
        tfile = Path(tdir) / unit_name
        tfile.write_text(body)
        check_call(
            [
                'plutil',
                '-lint',
                '-s',  # silent on success
                tfile,
            ]
        )


def cmd_past(unit: Unit) -> None:
    sub = fqn('dron.' + unit)
    # fmt: off
    cmd = [
        # todo maybe use 'stream'??
        'log', 'show', '--info',
        # '--last', '24h',
        # hmm vvv that doesn't work, if we pass pid, predicate is ignored?
        # '--process', '1',
        # hmm, oddly enough "&&" massively slows the predicate??
        #'--predicate', f'processIdentifier=1 && (subsystem contains "gui/501/dron.{unit}")',
        '--predicate', f'subsystem contains "{sub}"',
        '--style', 'ndjson',
        '--color', 'always',
    ]
    # fmt: on
    with Popen(cmd, stdout=PIPE, encoding='utf8') as p:
        out = p.stdout
        assert out is not None
        for line in out:
            j = json.loads(line)
            if j.get('finished') == 1:
                # last event at the very end
                continue
            subsystem = j['subsystem']
            # sometimes subsystem contains pid at the end, need to chop it off
            # also that's wjy we can't use "subsystem = " predicate :(
            subsystem = subsystem.split(' ')[0]
            if sub != subsystem:
                continue
            msg = j['eventMessage']

            interesting = re.search(' spawned .* because', msg) or 'exited ' in msg
            if not interesting:
                continue
            ts = j['timestamp']
            print(ts, sub, msg)


def cmd_run(*, unit: Unit, do_exec: bool) -> None:
    if not do_exec:
        launchctl_kickstart(unit=unit)
        return

    states = []
    for s in launchd_state(with_body=False):
        if s.unit_file.stem == unit:
            states.append(s)
    [state] = states
    cmdline = state.cmdline
    assert cmdline is not None, unit

    ## cut off launchd wrapper
    sep_i = cmdline.index('--')
    cmdline = cmdline[sep_i + 1 :]
    ##

    cmds = ' '.join(map(shlex.quote, cmdline))
    logger.info(f'running: {cmds}')
    os.execvp(
        cmdline[0],
        list(cmdline),
    )


def get_entries_for_monitor(managed: State, *, params: MonitorParams) -> list[MonitorEntry]:
    # for now kinda copy pasted from systemd

    entries: list[MonitorEntry] = []
    for s in managed:
        assert isinstance(s, LaunchdUnitState), s

        unit_file = s.unit_file
        name = unit_file.name.removesuffix('.plist')

        is_seconds = re.fullmatch(r'every (\d+) seconds', s.schedule or '')
        if is_seconds is not None:
            delta = timedelta(seconds=int(is_seconds.group(1)))
            # meh, but works for now
            ss = f'every {delta}'
        else:
            ss = str(s.schedule)

        schedule = ss
        command = None
        if params.with_command:
            cmdline = s.cmdline
            assert cmdline is not None, name  # not None for launchd units
            command = ' '.join(map(shlex.quote, cmdline))
            command = remove_launchd_wrapper(command)

        status_ok = s.last_exit_code == '0'
        status = 'success' if status_ok else f'exitcode {s.last_exit_code}'

        pid = s.pid

        # launchd has no supported API for retrieving the next scheduled firing.
        # launchctl's unstable printed output does not expose one either.
        entries.append(
            MonitorEntry(
                unit=name,
                status=status,
                left='n/a',
                next='n/a',
                schedule=schedule,
                command=command,
                pid=pid,
                status_ok=status_ok,
            )
        )
    return entries
