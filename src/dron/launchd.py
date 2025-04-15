from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import sys
import textwrap
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from subprocess import PIPE, Popen, check_call, check_output
from tempfile import TemporaryDirectory
from typing import Any

from .api import (
    OnCalendar,
    OnFailureAction,
    When,
)
from .common import (
    ALWAYS,
    MANAGED_MARKER,
    Command,
    LaunchdUnitState,
    MonitorEntry,
    MonitorParams,
    State,
    Unit,
    UnitFile,
    logger,
    unwrap,
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
    # fmt: off
    return [
        sys.executable,
        '-m',
        'dron.launchd_wrapper',
        *itertools.chain.from_iterable(('--notify', n) for n in on_failure),
        '--job', job,
        '--',
    ]
    # fmt: on


def remove_launchd_wrapper(cmd: str) -> str:
    if ' dron.launchd_wrapper ' not in cmd:
        return cmd
    # uhh... not super reliable, but this is only used for monitor so hopefully fine
    [_, cmd] = cmd.split(' -- ', maxsplit=1)
    return cmd


def plist(
    *,
    unit_name: str,
    command: Command,
    on_failure: Sequence[OnFailureAction],
    when: When | None = None,
) -> str:
    # TODO hmm, kinda mirrors 'escape' method, not sure
    cmd: Sequence[str]
    if isinstance(command, (list, tuple)):
        cmd = tuple(map(str, command))
    elif isinstance(command, Path):
        cmd = [str(command)]
    elif isinstance(command, str) and ' ' not in command:
        cmd = [command]
    else:
        # unquoting and splitting is way trickier than quoting and joining...
        # not sure how to implement it p
        # maybe we just want bash -c in this case, dunno how to implement properly
        raise RuntimeError(command)
    del command

    mschedule = ''
    if when is None:
        # support later
        raise RuntimeError(unit_name)

    if when == ALWAYS:
        mschedule = '<key>KeepAlive</key>\n<true/>'
    else:
        assert isinstance(when, OnCalendar), when
        # https://www.freedesktop.org/software/systemd/man/systemd.time.html#
        # fmt: off
        seconds = {
            'minutely': 60,
            'hourly'  : 60 * 60,
            'daily'   : 60 * 60 * 24,
        }.get(when)
        # fmt: on
        if seconds is None:
            # ok, try systemd-like spec..
            # fmt: off
            specs = [
                (re.escape('*:0/')   + r'(\d+)', 60),
                (re.escape('*:*:0/') + r'(\d+)', 1),
            ]
            # fmt: on
            for rgx, mult in specs:
                m = re.fullmatch(rgx, when)
                if m is not None:
                    num = m.group(1)
                    seconds = int(num) * mult
                    break
        if seconds is None:
            # try to parse as hh:mm at least
            m = re.fullmatch(r'(\d\d):(\d\d)', when)
            assert m is not None, when
            hh = m.group(1)
            mm = m.group(2)
            mschedule = '\n'.join(
                [
                    '<key>StartCalendarInterval</key>',
                    '<dict>',
                    '<key>Hour</key>',
                    f'<integer>{int(hh)}</integer>',
                    '<key>Minute</key>',
                    f'<integer>{int(mm)}</integer>',
                    '</dict>',
                ]
            )
        else:
            mschedule = '\n'.join(('<key>StartInterval</key>', f'<integer>{seconds}</integer>'))

    assert mschedule != '', unit_name

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
    program_argvs = '\n'.join(f'<string>{c}</string>' for c in program_argv)

    # TODO add log file, although mailer is already capturing stdout
    # TODO hmm maybe use the same log file for all dron jobs? would make it easier to rotate?
    res = f'''
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>

    <key>Label</key>
    <string>{DRON_PREFIX}{unit_name}</string>
    <key>ProgramArguments</key>
    <array>
{textwrap.indent(program_argvs, " " * 8)}
    </array>

    <key>RunAtLoad</key>
    <true/>

{textwrap.indent(mschedule, " " * 8)}

    <key>Comment</key>
    <string>{MANAGED_MARKER}</string>
</dict>
</plist>
'''.lstrip()
    return res


from .common import LaunchdUnitState


def launchd_state(*, with_body: bool) -> Iterator[LaunchdUnitState]:
    # sadly doesn't look like it has json interface??
    dump = check_output(['launchctl', 'dumpstate']).decode('utf8')

    name: str | None = None
    extras: dict[str, Any] = {}
    arguments: list[str] | None = None
    all_props: str | None = None
    fields = [
        'path',
        'last exit code',
        'pid',
        'run interval',
    ]
    for line in dump.splitlines():
        if name is None:
            # start of job description group
            name = line.removesuffix(' = {')
            all_props = ''
            continue
        elif line == '}':
            # end of job description group
            path: str | None = extras.get('path')
            if path is not None and 'dron' in path:
                # otherwsie likely some sort of system unit
                unit_file = Path(path)
                body = unit_file.read_text() if with_body else None

                # TODO extract 'state'??

                periodic_schedule = extras.get('run interval')
                calendal_schedule = 'com.apple.launchd.calendarinterval' in unwrap(all_props)

                schedule: str | None = None
                if periodic_schedule is not None:
                    schedule = 'every ' + periodic_schedule
                elif calendal_schedule:
                    # TODO parse properly
                    schedule = 'calendar'
                else:
                    # NOTE: seems like keepalive attribute isn't present in launcd dumpstate output
                    schedule = 'always'

                yield LaunchdUnitState(
                    unit_file=Path(path),
                    body=body,
                    cmdline=tuple(extras['arguments']),
                    # might not be present when we killed process manually?
                    last_exit_code=extras.get('last exit code'),
                    # pid might not be present (presumably when it's not running)
                    pid=extras.get('pid'),
                    schedule=schedule,
                )
            name = None
            all_props = None
            extras = {}
            continue

        all_props = unwrap(all_props) + line + '\n'

        if arguments is not None:
            if line == '\t}':
                extras['arguments'] = arguments
                arguments = None
            else:
                arg = line.removeprefix('\t\t')
                arguments.append(arg)
        else:
            xx = line.removeprefix('\t')
            for f in fields:
                zz = f'{f} = '
                if xx.startswith(zz):
                    extras[f] = xx.removeprefix(zz)
                    break
            # special handling..
            if xx.startswith('arguments = '):
                arguments = []


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
