from __future__ import annotations

import importlib.util
import os
import shlex
import sys
from collections import OrderedDict
from difflib import unified_diff
from itertools import tee
from pathlib import Path
from subprocess import check_call, run
from tempfile import TemporaryDirectory
from typing import Iterable, Iterator, NamedTuple, Union

import click

from . import launchd, systemd
from .api import Job, UnitName
from .common import (
    ALWAYS,
    IS_SYSTEMD,
    Body,
    State,
    Unit,
    UnitFile,
    UnitState,
    logger,
    unwrap,
)
from .systemd import _systemctl

# todo appdirs?
DRON_DIR = Path('~/.config/dron').expanduser()
DRON_UNITS_DIR = DRON_DIR / 'units'
DRON_UNITS_DIR.mkdir(parents=True, exist_ok=True)


DRONTAB = DRON_DIR / 'drontab.py'


def verify_units(pre_units: list[tuple[UnitName, Body]]) -> None:
    # need an inline import here in case we modify this variable from cli/tests
    from .common import VERIFY_UNITS

    if not VERIFY_UNITS:
        return

    if len(pre_units) == 0:
        # otherwise systemd analayser would complain if we pass zero units
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

    logger.info(f'writing unit file: {unit_file}')
    verify_unit(unit_name=unit_file.name, body=body)
    unit_file.write_text(body)


def _daemon_reload() -> None:
    if IS_SYSTEMD:
        check_call(_systemctl('daemon-reload'))
    else:
        # no-op under launchd
        pass


def managed_units(*, with_body: bool) -> State:
    if IS_SYSTEMD:
        yield from systemd.systemd_state(with_body=with_body)
    else:
        yield from launchd.launchd_state(with_body=with_body)


def make_state(jobs: Iterable[Job]) -> State:
    pre_units = []
    names: set[Unit] = set()
    for j in jobs:
        uname = j.unit_name

        assert uname not in names, j
        names.add(uname)

        if IS_SYSTEMD:
            s = systemd.service(unit_name=uname, command=j.command, on_failure=j.on_failure, **j.kwargs)
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
            p = launchd.plist(unit_name=uname, command=j.command, on_failure=j.on_failure, when=j.when)
            pre_units.append((uname + '.plist', p))

    verify_units(pre_units)

    for unit_file, body in pre_units:
        yield UnitState(
            unit_file=DRON_UNITS_DIR / unit_file,
            body=body,
            cmdline=None,  # ugh, a bit crap, but from this code path cmdline doesn't matter
        )


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
        msg = "Trying to delete all managed jobs"
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
    logger.info(f'no change: {len(nochange)}')
    logger.info(f'disabling: {len(deletes)}')
    logger.info(f'updating : {len(updates)}')
    logger.info(f'adding   : {len(adds)}')

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
        unit = u.unit
        unit_file = u.unit_file
        logger.info(f'updating {unit}')
        for d in diff:
            sys.stderr.write(d)
        write_unit(unit=u.unit, body=u.new_body)
        if IS_SYSTEMD:
            if unit.endswith('.service') and is_always_running(unit_file):
                # persistent unit needs a restart to pick up change
                _daemon_reload()
                check_call(_systemctl('restart', unit))
        else:
            launchd.launchctl_reload(unit=Path(unit).stem, unit_file=unit_file)

        if unit.endswith('.timer'):
            _daemon_reload()
            # NOTE: need to be careful -- seems that job might trigger straightaway if it's on interval schedule
            # so if we change something unrelated (e.g. whitespace), it will start all jobs at the same time??
            check_call(_systemctl('restart', u.unit))

    for a in adds:
        logger.info(f'adding {a.unit_file}')
        # TODO when we add, assert that previous unit wasn't managed? otherwise we overwrite something
        write_unit(unit=a.unit, body=a.body)

    # need to load units before starting the timers..
    _daemon_reload()

    for a in adds:
        unit_file = a.unit_file
        unit = unit_file.name
        logger.info(f'enabling {unit}')
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


Error = str
# TODO perhaps, return Plan or error instead?

# eh, implicit convention that only one state will be emitted. oh well
def lint(tabfile: Path) -> Iterator[Exception | State]:
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
        scmd = ' '.join(map(shlex.quote, l))
        logger.info(f'Running: {scmd}')
        with TemporaryDirectory() as td:
            env = {**os.environ}
            env = extra_path('MYPYPATH'  , dtab_dir, env)

            r = run(l, cwd=str(ldir), env=env, check=False)
        if r.returncode == 0:
            logger.info('OK')
            continue
        else:
            logger.error(f'FAIL: code: {r.returncode}')
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


def test_do_lint(tmp_path: Path) -> None:
    import pytest


    def ok(body: str) -> None:
        tpath = Path(tmp_path) / 'drontab.py'
        tpath.write_text(body)
        do_lint(tabfile=tpath)

    def fails(body: str) -> None:
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
from dron.api import job
def jobs():
    yield job(
        'hourly',
        ['/bin/echo', '123'],
        unit_name='unit_test',
    )
''')

    from .systemd import _is_missing_systemd

    if not _is_missing_systemd():
        from .cli import _drontab_example

        # this test doesn't work without systemd yet, because launchd adapter doesn't support unquoted commands, at least yet..
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
    pp = str(ppath)
    sys.path.insert(0, pp)
    try:
        spec = importlib.util.spec_from_file_location(tabfile.name, tabfile)
        assert spec is not None, tabfile
        loader = spec.loader
        assert loader is not None, (tabfile, spec)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    finally:
        sys.path.remove(pp)  # extremely meh..

    jobs = module.jobs
    emitted: dict[str, Job] = {}
    for job in jobs():
        assert isinstance(job, Job), job  # just in case for dumb typos
        assert job.unit_name not in emitted, (job, emitted[job.unit_name])
        yield job
        emitted[job.unit_name] = job


def apply(tabfile: Path) -> None:
    state = do_lint(tabfile)
    manage(state=state)


get_entries_for_monitor = (
    systemd.get_entries_for_monitor
    if IS_SYSTEMD else
    launchd.get_entries_for_monitor
)


def main() -> None:
    from . import cli

    cli.main()


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
