#!/usr/bin/env python3
import argparse
from collections import OrderedDict
from difflib import unified_diff
from itertools import tee
import os
import sys
from pathlib import Path
import shlex
import shutil
from subprocess import check_call, run
from tempfile import TemporaryDirectory
from typing import NamedTuple, Union, Optional, Iterator, Iterable, Any, Set


import click


from .api import *


from .common import (
    IS_SYSTEMD,
    logger,
    unwrap,
    MANAGED_MARKER,
    PathIsh,
    Unit, Body, UnitFile,
    VERIFY_UNITS,
)
from . import launchd
from . import systemd
from .systemd import _systemctl
from .launchd import _launchctl


# todo appdirs?
DRON_DIR = Path('~/.config/dron').expanduser()
DRON_UNITS_DIR = DRON_DIR / 'units'
DRON_UNITS_DIR.mkdir(parents=True, exist_ok=True)


DRONTAB = DRON_DIR / 'drontab.py'


UnitName = str
def verify_units(pre_units: list[tuple[UnitName, Body]]) -> None:
    if not VERIFY_UNITS:
        return

    if not IS_SYSTEMD:
        for unit_name, body in pre_units:
            launchd.verify_unit(unit_name=unit_name, body=body)
    else:
        systemd.verify_units(pre_units=pre_units)


def verify_unit(*, unit_name: UnitName, body: Body) -> None:
    return verify_units([(unit_name, body)])


def write_unit(*, unit: Unit, body: Body, prefix: Path=DRON_UNITS_DIR) -> None:
    unit_file = prefix / unit

    logger.info('writing unit file: %s', unit_file)
    verify_unit(unit_name=unit_file.name, body=body)
    unit_file.write_text(body)


def _daemon_reload() -> None:
    if IS_SYSTEMD:
        check_call(_systemctl('daemon-reload'))
    else:
        # no-op under launchd
        pass


from .common import UnitState, State


def managed_units(*, with_body: bool) -> State:
    if IS_SYSTEMD:
        yield from systemd.systemd_state(with_body=with_body)
    else:
        yield from launchd.launchd_state(with_body=with_body)


from .common import ALWAYS
def make_state(jobs: Iterable[Job]) -> State:
    pre_units = []
    names: Set[Unit] = set()
    for j in jobs:
        uname = j.unit_name

        assert uname not in names, j
        names.add(uname)

        if IS_SYSTEMD:
            s = systemd.service(unit_name=uname, command=j.command, extra_email=j.extra_email, **j.kwargs)
            pre_units.append((uname + '.service', s))

            when = j.when
            if when is None:
                # manual job?
                continue
            if when == ALWAYS:
                continue
            t = systemd.timer(unit_name=uname, when=when)
            pre_units.append((uname + '.timer', t))
        else:
            p = launchd.plist(unit_name=uname, command=j.command, when=j.when)
            pre_units.append((uname + '.plist', p))

    verify_units(pre_units)

    for unit_file, body in pre_units:
        yield UnitState(unit_file=DRON_UNITS_DIR / unit_file, body=body)


# TODO bleh. too verbose..
class Update(NamedTuple):
    unit_file: UnitFile
    old_body: Body
    new_body: Body

    @property
    def unit(self) -> str:
        return self.unit_file.name


class Delete(NamedTuple):
    unit_file: UnitFile

    @property
    def unit(self) -> str:
        return self.unit_file.name


class Add(NamedTuple):
    unit_file: UnitFile
    body: Body

    @property
    def unit(self) -> str:
        return self.unit_file.name


Action = Union[Update, Delete, Add]
Plan = Iterable[Action]

# TODO ugh. not sure how to verify them?

def compute_plan(*, current: State, pending: State) -> Plan:
    # eh, I feel like i'm reinventing something already existing here...
    currentd = OrderedDict((x.unit_file, unwrap(x.body)) for x in current)
    pendingd = OrderedDict((x.unit_file, unwrap(x.body)) for x in pending)

    units = [c for c in currentd if c not in pendingd] + list(pendingd.keys())
    for u in units:
        unit = u.name # TODO ??

        in_cur = u in currentd
        in_pen = u in pendingd
        if in_cur:
            if in_pen:
                # TODO not even sure I should emit it if bodies match??
                yield Update(unit_file=u, old_body=currentd[u], new_body=pendingd[u])
            else:
                yield Delete(unit_file=u)
        else:
            if in_pen:
                yield Add(unit_file=u, body=pendingd[u])
            else:
                raise AssertionError("Can't happen")


# TODO it's not apply, more like 'compute' and also plan is more like a diff between states?
def apply_state(pending: State) -> None:
    current = list(managed_units(with_body=True))

    pending_units = {s.unit_file.name for s in pending}
    def is_always_running(unit_path: Path) -> bool:
        name = unit_path.stem
        has_timer = f'{name}.timer' in pending_units
        # TODO meh. not ideal
        return not has_timer

    plan = list(compute_plan(current=current, pending=pending))

    deletes: list[Delete] = []
    adds: list[Add] = []
    _updates: list[Update] = []

    for a in plan:
        if isinstance(a, Delete):
            deletes.append(a)
        elif isinstance(a, Add):
            adds.append(a)
        elif isinstance(a, Update):
            _updates.append(a)
        else:
            raise AssertionError("Can't happen", a)

    if len(deletes) == len(current) and len(deletes) > 0:
        msg = f"Trying to delete all managed jobs"
        if click.confirm(f'{msg}. Are you sure?', default=False):
            pass
        else:
            raise RuntimeError(msg)

    Diff = list[str]
    nochange: list[Update] = []
    updates: list[tuple[Update, Diff]] = []

    for u in _updates:
        unit = a.unit
        diff: Diff = list(unified_diff(
            u.old_body.splitlines(keepends=True),
            u.new_body.splitlines(keepends=True),
        ))
        if len(diff) == 0:
            nochange.append(u)
        else:
            updates.append((u, diff))

    # TODO list unit names here?
    logger.info('no change: %d', len(nochange))
    logger.info('disabling: %d', len(deletes))
    logger.info('updating : %d', len(updates))
    logger.info('adding   : %d', len(adds))

    for a in deletes:
        if IS_SYSTEMD:
            # TODO stop timer first?
            check_call(_systemctl('stop'   , a.unit))
            check_call(_systemctl('disable', a.unit))
        else:
            launchd.launchctl_unload(unit=Path(a.unit).stem)
    for a in deletes:
        (DRON_UNITS_DIR / a.unit).unlink()


    for (u, diff) in updates:
        logger.info('updating %s', unit)
        for d in diff:
            sys.stderr.write(d)
        write_unit(unit=u.unit, body=u.new_body)
        if IS_SYSTEMD:
            if unit.endswith('.service') and is_always_running(u.unit_file):
                # persistent unit needs a restart to pick up change
                _daemon_reload()
                check_call(_systemctl('restart', u.unit))
        else:
            launchd.launchctl_reload(unit=Path(u.unit).stem, unit_file=u.unit_file)

        if unit.endswith('.timer'):
            # TODO do we need to enable again??
            _daemon_reload()
            check_call(_systemctl('restart', u.unit))
        # TODO some option to treat all updates as deletes then adds might be good...

    # TODO more logging?

    for a in adds:
        logger.info('adding %s', a.unit_file)
        # TODO when we add, assert that previous unit wasn't managed? otherwise we overwrite something
        write_unit(unit=a.unit, body=a.body)

    # need to load units before starting the timers..
    _daemon_reload()
   
    for a in adds:
        unit_file = a.unit_file
        unit = unit_file.name
        logger.info('enabling %s', unit)
        if unit.endswith('.service'):
            # quiet here because it warns that "The unit files have no installation config"
            # TODO maybe add [Install] section? dunno
            maybe_now = []
            if is_always_running(unit_file):
                maybe_now = ['--now']
            check_call(_systemctl('enable', unit_file, '--quiet', *maybe_now))
        elif unit.endswith('.timer'):
            check_call(_systemctl('enable', unit_file, '--now'))
        elif unit.endswith('.plist'):
            launchd.launchctl_load(unit_file=unit_file)
        else:
            raise AssertionError(a)

    # TODO not sure if this reload is even necessary??
    _daemon_reload()


def manage(state: State) -> None:
    apply_state(pending=state)


def cmd_edit() -> None:
    drontab = DRONTAB
    if not drontab.exists():
        if click.confirm(f"tabfile {drontab} doesn't exist. Create?", default=True):
            drontab.write_text('''
#!/usr/bin/env python3
from dron import job

def jobs():
    # yield job(
    #     'hourly',
    #     '/bin/echo 123',
    #     unit_name='test_unit'
    # )
    pass
'''.lstrip())
        else:
            raise RuntimeError()

    editor = os.environ.get('EDITOR')
    if editor is None:
        logger.warning('No EDITOR! Fallback to nano')
        editor = 'nano'

    with TemporaryDirectory() as tdir:
        tpath = Path(tdir) / 'drontab'
        shutil.copy2(drontab, tpath)

        orig_mtime = tpath.stat().st_mtime
        while True:
            res = run([editor, str(tpath)])
            res.check_returncode()

            new_mtime = tpath.stat().st_mtime
            if new_mtime == orig_mtime:
                logger.warning('No notification made')
                return

            ex: Optional[Exception] = None
            try:
                state = do_lint(tabfile=tpath)
            except Exception as e:
                logger.exception(e)
                ex = e
            else:
                try:
                    manage(state=state)
                except Exception as ee:
                    logger.exception(ee)
                    ex = ee
            if ex is not None:
                if click.confirm('Got errors. Try again?', default=True):
                    continue
                else:
                    raise ex
            else:
                drontab.write_text(tpath.read_text()) # handles symlinks correctly
                logger.info("Wrote changes to %s. Don't forget to commit!", drontab)
                break

        # TODO show git diff?
        # TODO perhaps allow to carry on regardless? not sure..
        # not sure how much we can do without modifying anything...


Error = str
# TODO perhaps, return Plan or error instead?

# eh, implicit convention that only one state will be emitted. oh well
def lint(tabfile: Path) -> Iterator[Union[Exception, State]]:
    linters = [
        [sys.executable, '-m', 'mypy', '--no-incremental', '--check-untyped', str(tabfile)],
    ]

    ldir = tabfile.parent
    # TODO not sure if should always lint in temporary dir to prevent turds?

    dron_dir = str(Path(__file__).resolve().absolute().parent)
    dtab_dir = drontab_dir()

    # meh.
    def extra_path(variable: str, path: str, env) -> dict[str, str]:
        vv = env.get(variable)
        pp = path + ('' if vv is None else ':' + vv)
        return {**env, variable: pp}

    errors = []
    for l in linters:
        logger.info('Running: %s', ' '.join(map(shlex.quote, l)))
        with TemporaryDirectory() as td:
            env = {**os.environ}
            env = extra_path('PYTHONPATH', dtab_dir, env)
            env = extra_path('MYPYPATH'  , dtab_dir, env)

            r = run(l, cwd=str(ldir), env=env)
        if r.returncode == 0:
            logger.info('OK')
            continue
        else:
            logger.error('FAIL: code: %d', r.returncode)
            errors.append('error')
    if len(errors) > 0:
        yield RuntimeError('Python linting failed!')
        return

    # TODO just add options to skip python lint? so it always goes through same code paths

    try:
        jobs = load_jobs(tabfile=tabfile, ppath=Path(dtab_dir))
    except Exception as e:
        # TODO could add better logging here? 'i.e. error while loading jobs'
        logger.exception(e)
        yield e
        return

    try:
        state = list(make_state(jobs))
    except Exception as e:
        logger.exception(e)
        yield e
        return

    yield state


def test_do_lint(tmp_path, handle_systemd):
    import pytest
    def ok(body: str):
        tpath = Path(tmp_path) / 'drontab'
        tpath.write_text(body)
        do_lint(tabfile=tpath)

    def fails(body: str):
        with pytest.raises(Exception):
            ok(body)

    fails(body='''
    None.whatever
    ''')

    # no jobs
    fails(body='''
    ''')

    ok(body='''
def jobs():
    yield from []
''')

    ok(body='''
from dron import job
def jobs():
    yield job(
        'hourly',
        '/bin/echo 123',
        unit_name='unit_test',
    )
''')

    example = _drontab_example()
    # ugh. some hackery to make it find the executable..
    echo = " '/bin/echo"
    example = example.replace(" 'linkchecker", echo).replace(" '/home/user/scripts/run-borg", echo).replace(" 'ping", " '/bin/ping")
    ok(body=example)


def do_lint(tabfile: Path) -> State:
    eit, vit = tee(lint(tabfile))
    errors = [r for r in eit if     isinstance(r, Exception)]
    values = [r for r in vit if not isinstance(r, Exception)]
    assert len(errors) == 0, errors
    [state] = values
    return state


def drontab_dir() -> str:
    # meeh
    return str(DRONTAB.resolve().absolute().parent)


def load_jobs(tabfile: Path, ppath: Path) -> Iterator[Job]:
    globs: dict[str, Any] = {}

    # TODO also need to modify pythonpath here??? ugh!

    pp = str(ppath)
    sys.path.insert(0, pp)
    try:
        exec(tabfile.read_text(), globs)
    finally:
        sys.path.remove(pp)  # extremely meh..

    jobs = globs['jobs']
    return jobs()


def apply(tabfile: Path) -> None:
    state = do_lint(tabfile)
    manage(state=state)


def cmd_lint(tabfile: Path) -> None:
    do_lint(tabfile)  # just ignore state
    logger.info('all good')


def cmd_apply(tabfile: Path) -> None:
    apply(tabfile)


from .common import MonParams


# TODO think if it's worth integrating with timers?
def cmd_monitor(params: MonParams) -> None:
    managed = list(managed_units(with_body=False)) # body slows down this call quite a bit
    if len(managed) == 0:
        print('No managed units!', file=sys.stderr)
    # TODO test it ?
    if IS_SYSTEMD:
        return systemd._cmd_monitor(managed, params=params)
    else:
        return launchd._cmd_monitor(managed, params=params)


def cmd_past(unit: Unit) -> None:
    if IS_SYSTEMD:
        return systemd.cmd_past(unit)
    else:
        return launchd.cmd_past(unit)


# TODO test it and also on Circle?
def _drontab_example():
    return '''
from dron import job

# at the moment you're expected to define jobs() function that yields jobs
# in the future I might add more mechanisms
def jobs():
    # simple job that doesn't do much
    yield job(
        'daily',
        '/home/user/scripts/run-borg /home/user',
        unit_name='borg-backup-home',
    )

    yield job(
        'daily',
        'linkchecker https://beepb00p.xyz',
        unit_name='linkchecker-beepb00p',
    )

    # drontab is simply python code!
    # so if you're annoyed by having to rememver Systemd syntax, you can use a helper function
    def every(*, mins: int) -> str:
        return f'*:0/{mins}'

    # make sure my website is alive, it will send local email on failure
    yield job(
        every(mins=10),
        'ping https://beepb00p.xyz',
        unit_name='ping-beepb00p',
    )
'''.lstrip()


def make_parser() -> argparse.ArgumentParser:
    from .common import VerifyOff
    def add_verify(p):
        # ugh. might be broken on bionic :(
        # specify in readme???
        # would be nice to use external checker..
        # https://github.com/systemd/systemd/issues/8072 
        # https://unix.stackexchange.com/questions/493187/systemd-under-ubuntu-18-04-1-fails-with-failed-to-create-user-slice-serv
        p.add_argument('--no-verify', action=VerifyOff, nargs=0, help='Skip systemctl verify step')

    p = argparse.ArgumentParser(prog='dron', description='''
dron -- simple frontend for Systemd, inspired by cron.

- *d* stands for 'Systemd'
- *ron* stands for 'cron'

dron is my attempt to overcome things that make working with Systemd tedious
'''.lstrip(),
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, width=100),  # type: ignore
    )
    # TODO ugh. when you type e.g. 'dron apply', help format is wrong..
    example = ''.join(': ' + l for l in _drontab_example().splitlines(keepends=True))
    # TODO begin_src python maybe?
    p.epilog = f'''
* What does it do?
In short, you type ~dron edit~ and edit your config file, similarly to ~crontab -e~:

{example}

After you save your changes and exit the editor, your drontab is checked for syntax and applied

- if checks have passed, your jobs are mapped onto Systemd units and started up
- if there are potential errors, you are prompted to fix them before retrying

* Why?
In short, because I want to benefit from the heavy lifting that Systemd does: timeouts, resource management, restart policies, powerful scheduling specs and logging,
while not having to manually manipulate numerous unit files and restart the daemon all over.

I elaborate on what led me to implement it and motivation [[https://beepb00p.xyz/scheduler.html#what_do_i_want][here]]. Also:

- why not just use [[https://beepb00p.xyz/scheduler.html#cron][cron]]?
- why not just use [[https://beepb00p.xyz/scheduler.html#systemd][systemd]]?
    '''

    p.add_argument('--marker', required=False, help=f'Use custom marker instead of default `{MANAGED_MARKER}`. Possibly useful for developing/testing.')

    sp = p.add_subparsers(dest='mode')
    mp = sp.add_parser('monitor', help='Monitor services/timers managed by dron')
    mp.add_argument('-n'        ,type=int, default=1, help='-n parameter for watch')
    mp.add_argument('--once'   , action='store_true', help='only call once')
    mp.add_argument('--rate'   , action='store_true', help='Display success rate (unstable and potentially slow)')
    mp.add_argument('--command', action='store_true', help='Display command')
    pp = sp.add_parser('past', help='List past job runs')
    pp.add_argument('unit', type=str) # TODO add shell completion?
    ep = sp.add_parser('edit', help="Edit  drontab (like 'crontab -e')")
    add_verify(ep)
    ap = sp.add_parser('apply', help="Apply drontab (like 'crontab' with no args)")
    ap.add_argument('tabfile', type=Path, nargs='?')
    add_verify(ap)
    # TODO --force?
    # TODO list?
    lp = sp.add_parser('lint', help="Check drontab (no 'crontab' alternative, sadly!)")
    add_verify(lp)
    lp.add_argument('tabfile', type=Path, nargs='?')
    up = sp.add_parser('uninstall', help="Uninstall all managed jobs")
    add_verify(up)

    return p


def main() -> None:
    p = make_parser()
    args = p.parse_args()


    marker = args.marker
    if marker is not None:
        global MANAGED_MARKER
        MANAGED_MARKER = marker

    mode = args.mode

    def tabfile_or_default() -> Path:
        tabfile = args.tabfile
        if tabfile is None:
            tabfile = DRONTAB
        return tabfile

    if mode == 'monitor':
        # TODO hacky...
        once = args.once
        if not once:
            argv = sys.argv + ['--once']
            # hmm for some reason on OSX termcolor doesn't work under watch??
            os.environ['FORCE_COLOR'] = 'true'
            os.execvp(
                'watch',
                [
                    'watch',
                    '--color',
                    '-n', str(args.n),
                    *map(shlex.quote, argv),
                ],
            )
        else:
            params = MonParams(
                with_success_rate=args.rate,
                with_command=args.command,
            )
            cmd_monitor(params)
    elif mode == 'past':
        cmd_past(unit=args.unit)
    elif mode == 'edit':
        cmd_edit()
    elif mode == 'lint':
        tabfile = tabfile_or_default()
        cmd_lint(tabfile)
    elif mode == 'apply':
        tabfile = tabfile_or_default()
        cmd_apply(tabfile)
    elif mode == 'uninstall':
        click.confirm('Going to remove all dron managed jobs. Continue?', default=True, abort=True)
        with TemporaryDirectory() as td:
            empty = Path(td) / 'empty'
            empty.write_text('''
def jobs():
    yield from []
''')
            cmd_apply(empty)
    else:
        logger.error('Unknown mode: %s', mode)
        p.print_usage(sys.stderr)
        sys.exit(1)
    # TODO need self install..
    # TODO add edit command; open drontab file in EDITOR; lint if necessary (link commands specified in the file)
    # after linting, carry on to applying


if __name__ == '__main__':
    main()


# TODO stuff I learnt:
# TODO  systemd-analyze --user unit-paths 
# TODO blame!
#  systemd-analyze verify -- check syntax

# TODO would be nice to revert... via contextmanager?
# TODO assert that managed by dron
# TODO not sure what rollback should do w.r.t to
# TODO perhaps, only reenable changed ones? ugh. makes it trickier...

# TODO wonder if I remove timers, do they drop counts?
# TODO FIXME ok, for now, it's fine, but with more sophisticated timers might be a bit annoying

# TODO use python's literate types?
# TODO


# TODO wow, that's quite annoying. so timer has to be separate file. oh well.

# TODO tui for confirming changes, show short diff?

# TODO actually for me, stuff like 'hourly' makes little sense; I usually space out in time..

# https://bugs.python.org/issue31528 eh, probably can't use configparser.. plaintext is good enough though.


# TODO later, implement logic for cleaning up old jobs


# TODO not sure if should do one by one or all at once?
# yeah, makes sense to do all at once...
# TODO warn about dirty state?


# TODO test with 'fake' systemd dir?

# TODO the assumption is that managed jobs are not changed manually, or changed in a way that doesn't break anything
# in general it's impossible to prevent anyway

# def update_unit(unit_file: Unit, old_body: Body, new_body: Body) -> Action:
#     if old_body == new_body:
#         pass # TODO no-op?
#     else:
#         raise RuntimeError(unit_file, old_body, new_body)
#     # TODO hmm FIXME!! yield is a nice way to make function lazy??


# TODO that perhaps? https://askubuntu.com/a/897317/427470
