from datetime import timedelta
import json
from pathlib import Path
import re
import shlex
from subprocess import check_output, Popen, PIPE, check_call
from tempfile import TemporaryDirectory
import textwrap
from typing import Sequence, Optional, Iterator, Any


from .common import (
    PathIsh,
    Unit, Body, UnitFile,
    Command,
    When, OnCalendar,
    logger,
    MonParams,
    State,
    LaunchdUnitState,
)


# TODO custom launchd domain?? maybe instead could do dron/ or something?
_LAUNCHD_DOMAIN = 'gui/501'


_MANAGED_MARKER = 'MANAGED BY DRON'

# in principle not necessary...
# but makes it much easier to filter out logs & lobs from launchctl dump
DRON_PREFIX = 'dron.'


def _launchctl(*args):
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
    # load is super defensive, returns code 1 on errors
    check_call(_launchctl('bootstrap', _LAUNCHD_DOMAIN, unit_file))
    _launch_agent(unit_file.name).symlink_to(unit_file)


def launchctl_unload(*, unit: Unit) -> None:
    # bootout is more verbose than unload
    # in addition unload is super defensive, returns code 0 on errors
    check_call(_launchctl('bootout', fqn(unit)))
    _launch_agent(unit + '.plist').unlink()


def launchctl_reload(*, unit: Unit, unit_file: UnitFile) -> None:
    # don't think there is a better way?
    launchctl_unload(unit=unit)
    launchctl_load(unit_file=unit_file)


def plist(*, unit_name: str, command: Command, when: Optional[When]=None) -> str:
    # TODO hmm, kinda mirrors 'escape' method, not sure
    cmd: Sequence[str]
    if isinstance(command, Sequence):
        cmd = tuple(map(str, command))
    elif isinstance(command, Path):
        cmd = [str(command)]
    else:
        # unquoting and splitting is way trickier than quoting and joining...
        # not sure how to implement it p
        # maybe we just want bash -c in this case, dunno how to implement properly
        assert False, command
    del command

    mschedule = ''
    if when is not None:
        assert isinstance(when, OnCalendar), when
        # https://www.freedesktop.org/software/systemd/man/systemd.time.html#
        seconds = {
            'minutely': 60,
            'hourly'  : 60 * 60,
            'daily'   : 60 * 60 * 24,
        }.get(when)
        if seconds is None:
            # ok, try systemd-like spec..
            specs = [
                (re.escape('*:0/')   + r'(\d+)', 60),
                (re.escape('*:*:0/') + r'(\d+)', 1 ),
            ]
            for rgx, mult in specs:
                m = re.fullmatch(rgx, when)
                if m is not None:
                    num = m.group(1)
                    seconds = int(num) * mult
                    break
        assert seconds is not None, when
        mschedule = '\n'.join(('<key>StartInterval</key>', f'<integer>{seconds}</integer>'))


    command_args = '\n'.join(f'<string>{c}</string>' for c in cmd)

    # FIXME shit. going to need a wrapper script to email on failure??
    # FIXME add log file
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
{textwrap.indent(command_args, " " * 8)}
    </array>

    <key>RunAtLoad</key>
    <true/>

{textwrap.indent(mschedule, " " * 8)}

    <key>Comment</key>
    <string>{_MANAGED_MARKER}</string>
</dict>
</plist>
'''.lstrip()
    return res


from .common import LaunchdUnitState
def launchd_state(with_body: bool) -> Iterator[LaunchdUnitState]:
    # sadly doesn't look like it has json interface??
    dump = check_output(['launchctl', 'dumpstate']).decode('utf8')

    name = None
    extras: dict[str, Any] = {}
    fields = [
        'path',
        'last exit code',
        'pid',
        'run interval',
    ]
    arguments = None
    for line in dump.splitlines():
        if name is None:
            name = line.removesuffix(' = {')
            continue
        elif line == '}':
            path = extras.get('path')
            if path is not None and 'dron' in path:
                # otherwsie likely some sort of system unit
                unit_file = Path(path)
                body = unit_file.read_text() if with_body else None
                yield LaunchdUnitState(
                    unit_file=Path(path),
                    body=body,
                    cmdline=tuple(extras['arguments']),
                    last_exit_code=extras['last exit code'],
                    # pid might not be present (presumably when it's not running)
                    pid=extras.get('pid'),
                    schedule=extras.get('run interval'),
                )
            name = None
            extras = {}
        elif arguments is not None:
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


def verify_unit(*, unit: PathIsh, body: str) -> None:
    with TemporaryDirectory() as tdir:
        tfile = Path(tdir) / Path(unit).name
        tfile.write_text(body)
        check_call([
            'plutil', '-lint',
            '-s',  # silent on success
            tfile,
        ])


def cmd_past(unit: Unit) -> None:
    sub = fqn('dron.' + unit)
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
    with Popen(cmd, stdout=PIPE, encoding='utf8') as p:
        out = p.stdout; assert out is not None
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
            ts  = j['timestamp']
            print(ts, sub, msg)


def _cmd_monitor(managed: State, *, params: MonParams) -> None:
    # for now kinda copy pasted from systemd
    logger.debug('starting monitor...')
    lines_: list[Sequence[str]] = []
    for s in managed:
        assert isinstance(s, LaunchdUnitState), s

        unit_file = s.unit_file
        name = unit_file.name.removesuffix('.plist')
        ok = True  # TODO?
        running = False  # TODO?

        is_seconds = re.fullmatch(r'(\d+) seconds', s.schedule)
        if is_seconds is not None:
            delta = timedelta(seconds=int(is_seconds.group(1)))
            # meh, but works for now
            ss = str(delta)
        else:
            ss = s.schedule

        schedule = f'every {ss}'
        mcommand = []
        if params.with_command:
            cmdline = ' '.join(map(shlex.quote, s.cmdline))
            mcommand = [cmdline]

        status = f'EXIT CODE {s.last_exit_code}'

        lines_.append((name, status, 'N/A', schedule, *mcommand))

    import tabulate
    tabulate.PRESERVE_WHITESPACE = True
    headers = ['UNIT', 'STATUS/AGO', 'LEFT', 'SCHEDULE']
    if params.with_command:
        headers += ['COMMAND']
    print(tabulate.tabulate(lines_, headers=headers))
