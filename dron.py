#!/usr/bin/env python3
import argparse
from collections import OrderedDict
from datetime import datetime, timedelta
from difflib import unified_diff
from itertools import tee
import json
import getpass
import os
import sys
from pathlib import Path
import shlex
import shutil
from subprocess import check_call, CalledProcessError, run, PIPE, check_output, Popen
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import NamedTuple, Union, Sequence, Optional, Iterator, Tuple, Iterable, List, Any, Dict


# TODO not sure about click..
import click # type: ignore

 # TODO
try:
    from kython.klogging2 import LazyLogger # type: ignore
except ImportError:
    import logging
    logger = logging.getLogger('dron')
else:
    # TODO need bit less verbose logging
    logger = LazyLogger('dron', level='debug')

DIR = Path("~/.config/systemd/user").expanduser()
# TODO FIXME mkdir in case it doesn't exist?


# TODO allow specifying the path somewhere?
DRONTAB = Path("~/.config/drontab").expanduser()


PathIsh = Union[str, Path]


VERIFY_UNITS = True
# TODO ugh. verify tries using already installed unit files so if they were bad, everything would fail
# I guess could do two stages, i.e. units first, then timers
# dunno, a bit less atomic though...


if 'PYTEST' in os.environ: # set by lint script
    import pytest # type: ignore
    fixture = pytest.fixture
else:
    fixture = lambda f: f # no-op otherwise to prevent pytest import


def has_systemd():
    if 'GITHUB_ACTION' in os.environ:
        return False
    return True


def skip_if_no_systemd():
    import pytest # type: ignore
    if not has_systemd():
        pytest.skip('No systemd')


# TODO eh, come up with a better name
@fixture
def handle_systemd():
    '''
    If we can't use systemd, we need to suppress systemd-specific linting
    '''
    global VERIFY_UNITS
    if not has_systemd():
        VERIFY_UNITS = False
    try:
        yield
    finally:
        VERIFY_UNITS = True


def scu(*args):
    return ['systemctl', '--user', *args]


def reload():
    check_call(scu('daemon-reload'))


MANAGED_MARKER = '<MANAGED BY DRON>'
def is_managed(body: str):
    # TODO not sure what's a good way of detecting that..
    legacy_marker = 'Systemdtab=true'
    # TODO remove Systemdtab=true later
    return MANAGED_MARKER in body or legacy_marker in body


MANAGED_HEADER = f'''
# {MANAGED_MARKER}
# If you do any manual changes, they will be overridden on the next dron run
'''.lstrip()


def test_managed(handle_systemd):
    skip_if_no_systemd()
    assert is_managed(timer(unit_name='whatever', when='daily'))

    custom = '''
[Service]
ExecStart=/bin/echo 123
'''
    verify(unit_file='other.service', body=custom) # precondition
    assert not is_managed(custom)


OnCalendar = str
TimerSpec = Dict[str, str] # meh
When = Union[OnCalendar, TimerSpec]
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
{MANAGED_HEADER}
[Unit]
Description=Timer for {unit_name}

[Timer]
{specs}

[Install]
WantedBy=timers.target
'''.lstrip()


Command = Union[PathIsh, Sequence[PathIsh]]

def ncmd(command: Command) -> List[str]:
    if isinstance(command, (str, Path)):
        return ncmd([command])
    else:
        return [str(c) for c in command]



# TODO allow to pass extra args
def service(*, unit_name: str, command: Command, **kwargs: str) -> str:
    # TODO FIXME think carefully about escaping command etc?
    nc = ncmd(command)
    # TODO not sure how to handle this properly...
    cmd = ' '.join(nc)

    # TODO ugh. how to allow injecting arbitrary stuff, not only in [Service] section?

    extras = '\n'.join(f'{k}={v}' for k, v in kwargs.items())

    res = f'''
{MANAGED_HEADER}
[Unit]
Description=Service for {unit_name}
OnFailure=status-email@%n.service

[Service]
ExecStart={cmd}
{extras}
'''.lstrip()
    # TODO not sure if should include username??
    return res


def verify(*, unit_file: str, body: str):
    if not VERIFY_UNITS:
        return

    # ugh. pipe doesn't work??
    # e.g. 'systemd-analyze --user verify <(cat systemdtab-test.service)' results in:
    # Failed to prepare filename /proc/self/fd/11: Invalid argument
    with TemporaryDirectory() as tdir:
        sfile = Path(tdir) / unit_file
        sfile.write_text(body)
        res = run(['systemd-analyze', '--user', 'verify', str(sfile)], stdout=PIPE, stderr=PIPE)
        # ugh. apparently even exit code 1 doesn't guarantee correct output??
        out = res.stdout
        err = res.stderr
        assert out == b'', out # not sure if that's possible..

        if err == b'':
            return

        # TODO UGH.
        # is not executable: No such file or directory

        msg = f'failed checking {unit_file}, exit code {res.returncode}'
        logger.error(msg)
        print(body, file=sys.stderr)
        print('systemd-analyze output:', file=sys.stderr)
        for line in err.decode('utf8').splitlines():
            print(line, file=sys.stderr)
            # TODO right, might need to install service first...
        raise RuntimeError(msg)


def test_verify(handle_systemd):
    skip_if_no_systemd()
    def fails(body: str):
        import pytest # type: ignore[import]
        with pytest.raises(Exception):
            verify(unit_file='whatever.service', body=body)

    def ok(body):
        verify(unit_file='ok.service', body=body)

    ok(body='''
[Service]
ExecStart=/bin/echo 123
''')

    ok(body=service(unit_name='alala', command='/bin/echo 123'))

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


def write_unit(*, unit_file: str, body: str) -> None:
    logger.debug('writing unit file: %s', unit_file)
    # TODO contextmanager?
    # I guess doesn't hurt doing it twice?
    verify(unit_file=unit_file, body=body)
    # TODO eh?
    (DIR / unit_file).write_text(body)


def prepare():
    # TODO automatically email to user? I guess make sense..
    user = getpass.getuser()
    # TODO atomic write?
    src = Path(__file__).absolute().resolve().parent / 'systemd-email'
    target = Path('~/.local/bin/systemd-email').expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    # TODO ln maybe?..

    # TODO set a very high nice value? not sure
    # TODO need to make sure logs are preserved?
    X = f'''
[Unit]
Description=status email for %i to {user}

[Service]
Type=oneshot
ExecStart={target} --to {user} --unit %i --journalctl-args "-o cat"
# TODO why these were suggested??
# User=nobody
# Group=systemd-journal
'''
    # TODO copy the file to local??
    write_unit(unit_file=f'status-email@.service', body=X)
    # I guess makes sense to reload here; fairly atomic step
    reload()


class Job(NamedTuple):
    when: Optional[When]
    command: Command
    unit_name: str
    kwargs: Dict[str, str]

# TODO think about arg names?
# TODO not sure if should give it default often?
# TODO when first? so it's more compat to crontab..
def job(when: Optional[When], command: Command, *, unit_name: Optional[str]=None, **kwargs) -> Job:
    """
    when: if None, then timer won't be created (still allows running job manually)

    """
    assert unit_name is not None
    # TODO later, autogenerate unit name
    # I guess warn user about non-unique names and prompt to give a more specific name?
    return Job(
        when=when,
        command=command,
        unit_name=unit_name,
        kwargs=kwargs,
    )


Unit = str
Body = str
State = Iterable[Tuple[Unit, Body]]


def managed_units() -> State:
    res = check_output(scu('list-unit-files', '--no-pager', '--no-legend')).decode('utf8')
    units = list(sorted(x.split()[0] for x in res.splitlines()))
    for u in units:
        # meh. but couldn't find any better way to filter a subset of systemd properties...
        # e.g. sc show only displays 'known' properties.
        # could filter by description? but bit too restrictive?

        res = check_output(scu('cat', u)).decode('utf8')
        # ugh. systemctl cat adds some annoying header...
        lines = res.splitlines(keepends=True)
        assert lines[0].startswith('# ')
        res = ''.join(lines[1:])

        if is_managed(res):
            yield u, res


def test_managed_units():
    skip_if_no_systemd()

    # shouldn't fail at least
    list(managed_units())

    # TODO ugh. doesn't work on circleci, fails with
    # dbus.exceptions.DBusException: org.freedesktop.DBus.Error.BadAddress: Address does not contain a colon
    if 'CI' not in os.environ:
        cmd_managed(long_=True)


def make_state(jobs: Iterable[Job]) -> State:
    def check(unit_file, body):
        verify(unit_file=unit_file, body=body)
        return (unit_file, body)

    for j in jobs:
        s = service(unit_name=j.unit_name, command=j.command, **j.kwargs)
        yield check(j.unit_name + '.service', s)

        when = j.when
        if when is None:
            continue
        t = timer(unit_name=j.unit_name, when=when)
        yield check(j.unit_name + '.timer', t)

    # TODO otherwise just unit status or something?

    # TODO FIXME enable?
    # TODO verify everything before starting to update
    # TODO copy files with rollback? not sure how easy it is..



# TODO bleh. too verbose..
class Update(NamedTuple):
    unit_file: Unit
    old_body: Body
    new_body: Body


class Delete(NamedTuple):
    unit_file: Unit


class Add(NamedTuple):
    unit_file: Unit
    body: Body


Action = Union[Update, Delete, Add]
Plan = Iterable[Action]

# TODO ugh. not sure how to verify them?

def compute_plan(*, current: State, pending: State) -> Plan:
    # eh, I feel like i'm reinventing something already existing here...
    currentd = OrderedDict(current)
    pendingd = OrderedDict(pending)

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
    current = list(managed_units())
    plan = list(compute_plan(current=current, pending=pending))

    deletes: List[Delete] = []
    updates: List[Update] = []
    adds: List[Add] = []

    for a in plan:
        if isinstance(a, Delete):
            deletes.append(a)
        elif isinstance(a, Update):
            updates.append(a)
        elif isinstance(a, Add):
            adds.append(a)
        else:
            raise AssertionError("Can't happen", a)

    if len(deletes) == len(current) and len(deletes) > 0:
        msg = f"Trying to delete all managed jobs"
        if click.confirm(f'{msg}. Are you sure?', default=False):
            pass
        else:
            raise RuntimeError(msg)

    logger.info('disabling: %d', len(deletes)) # TODO rename to disables?
    logger.info('updating : %d', len(updates)) # TODO list unit names?
    logger.info('adding   : %d', len(adds)) # TODO only list ones that actually changing?

    for a in deletes:
        # TODO stop timer first?
        check_call(scu('stop'   , a.unit_file))
        check_call(scu('disable', a.unit_file))
    for a in deletes:
        (DIR / a.unit_file).unlink() # TODO eh. not sure what do we do with user modifications?

    # TODO not sure how to support 'dirty' units detection...
    for a in updates:
        ufile = a.unit_file
        diff = list(unified_diff(a.old_body.splitlines(keepends=True), a.new_body.splitlines(keepends=True)))
        if len(diff) == 0:
            continue
        logger.info('updating %s', ufile)
        for d in diff:
            sys.stderr.write(d)
        write_unit(unit_file=ufile, body=a.new_body)

        if ufile.endswith('.timer'):
            # TODO do we need to enable again??
            check_call(scu('restart', a.unit_file))
        # TODO some option to treat all updates as deletes then adds might be good...

    # TODO more logging?

    for a in adds:
        ufile = a.unit_file
        logger.info('adding %s', ufile)
        # TODO when we add, assert that previous unit wasn't managed? otherwise we overwrite something
        write_unit(unit_file=ufile, body=a.body)
        if ufile.endswith('.timer'):
            logger.info('starting %s', ufile)
            # TODO use enable --now??
            check_call(scu('start', ufile)) # dunno if it's worth restarting?
            check_call(scu('enable', ufile))

    reload()


def manage(state: State) -> None:
    prepare()
    apply_state(pending=state)


def cmd_edit():
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
    # TODO how to allow these to be defined in tab file?

    linters = [
        ['python3', '-m', 'pylint', '-E', str(tabfile)],
        ['python3', '-m', 'mypy', '--no-incremental', '--check-untyped', str(tabfile)],
    ]

    ldir = tabfile.parent
    # TODO not sure if should always lint in temporary dir to prevent turds?

    dron_dir = str(Path(__file__).resolve().absolute().parent)
    dtab_dir = drontab_dir()

    # meh.
    def extra_path(variable: str, path: str, env) -> Dict[str, str]:
        vv = env.get(variable)
        pp = path + ('' if vv is None else ':' + vv)
        return {**env, variable: pp}

    errors = []
    for l in linters:
        logger.info('Running: %s', ' '.join(map(shlex.quote, l)))
        with TemporaryDirectory() as td:
            env = {**os.environ}
            env = extra_path('PYTHONPATH', dron_dir, env)
            env = extra_path('PYTHONPATH', dtab_dir, env)

            env = extra_path('MYPYPATH', dron_dir, env)
            env = extra_path('MYPYPATH', dtab_dir, env)

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
        jobs = load_jobs(tabfile=tabfile)
    except Exception as e:
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


def load_jobs(tabfile: Path) -> Iterator[Job]:
    globs: Dict[str, Any] = {}

    # TODO also need to modify pythonpath here??? ugh!

    pp = str(tabfile.resolve().absolute().parent)
    sys.path.insert(0, pp)
    try:
        exec(tabfile.read_text(), globs)
    finally:
        sys.path.remove(pp) # extremely meh..


    jobs = globs['jobs']
    return jobs()


def apply(tabfile: Path) -> None:
    state = do_lint(tabfile)
    manage(state=state)


def cmd_lint(tabfile: Path) -> None:
    do_lint(tabfile) # just ignore state


def cmd_apply(tabfile: Path) -> None:
    apply(tabfile)


def _cmd_managed_long(managed):
    # TODO reorder timers and services so timers go before?
    sd = lambda s: f'org.freedesktop.systemd1{s}'

    UTCNOW = datetime.utcnow()

    # TODO not sure what's difference from colorama?
    import termcolor

    import tabulate
    from dbus import SessionBus, Interface, DBusException # type: ignore[import]
    bus = SessionBus()  # TODO SystemBus for system??
    systemd = bus.get_object(sd(''), '/org/freedesktop/systemd1')
    manager = Interface(systemd, dbus_interface=sd('.Manager'))
    lines = []
    for u, _ in managed:
        service_unit = manager.GetUnit(u)
        service_proxy = bus.get_object(sd(''), str(service_unit))
        properties = Interface(service_proxy, dbus_interface='org.freedesktop.DBus.Properties')
        ok = True
        if u.endswith('.timer'):
            cmd = 'n/a'
            status = 'n/a'

            cal   = properties.Get(sd('.Timer'), 'TimersCalendar')
            last  = properties.Get(sd('.Timer'), 'LastTriggerUSec')
            next_ = properties.Get(sd('.Timer'), 'NextElapseUSecRealtime')

            spec = cal[0][1] # TODO is there a more reliable way to retrieve it??
            # TODO not sure if last is really that useful..

            last_dt = datetime.utcfromtimestamp(int(last)  / 10 ** 6)
            next_dt = datetime.utcfromtimestamp(int(next_) / 10 ** 6)

            # chop off microseconds
            left_delta = timedelta(seconds=(next_dt - UTCNOW).seconds)

            passed_delta = timedelta(seconds=(UTCNOW - last_dt).seconds)

            # TODO color?
            left   = f'{str(left_delta  ):<8} left'
            status = f'{str(passed_delta):<8} ago'
            cmd = f'next: {next_dt.isoformat()}; schedule: {spec}'
        else:
            # TODO some summary too? e.g. how often in failed
            # TODO make defensive?
            exec_start = properties.Get(sd('.Service'), 'ExecStart')
            result     = properties.Get(sd('.Service'), 'Result')
            cmd =  ' '.join(map(shlex.quote, exec_start[0][1]))

            status = str(result)
            if status == 'success':
                color = 'green'
            else:
                color = 'red'
                ok = True
            status = termcolor.colored(status, color)
            left = ''

        lines.append((ok, [u, status, left, cmd]))
    lines_ = [l for _, l in sorted(lines, key=lambda x: x[0])]
    # naming is consistent with systemctl --list-timers
    print(tabulate.tabulate(lines_, headers=['UNIT', 'STATUS/PASSED', 'LEFT', 'COMMAND/SCHEDULE']))


# TODO think if it's worth integrating with timers?
def cmd_managed(long_: bool):
    managed = list(managed_units())
    if len(managed) == 0:
        print('No managed units!', file=sys.stderr)
    # TODO test long_ mode?
    if long_:
        _cmd_managed_long(managed)
    else:
        for u, _ in managed:
            print(u)


def cmd_timers():
    os.execvp('watch', ['watch', '-n', '0.5', ' '.join(scu('list-timers', '--all'))])


def cmd_past(unit: str):
    # meh
    # TODO so do I need to parse logs to get failure stats? perhaps json would be more reliable
    cmd = f'journalctl --user -u {unit} -o json -t systemd --output-fields UNIT_RESULT,JOB_TYPE'
    # TODO not sure if the stats belong here..
    started = 0
    failed  = 0
    # TODO not sure how much time it takes to query all journals?
    with Popen(cmd.split(), stdout=PIPE) as po:
        stdout = po.stdout; assert stdout is not None
        for line in stdout:
            j = json.loads(line.decode('utf8'))
            # apparently, successful runs aren't getting logged? not sure why
            jt = j.get('JOB_TYPE')
            ur = j.get('UNIT_RESULT')
            # not sure about this..
            assert (jt is None) ^ (ur is None), j
            if jt is not None:
                started += 1
            else:
                failed += 1
            # if res is not set, it must have been 'unit started' message??
    print(started, failed)


class VerifyOff(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        global VERIFY_UNITS
        VERIFY_UNITS = False


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



def make_parser():
    def add_verify(p):
        # ugh. might be broken on bionic :(
        # specify in readme???
        # would be nice to use external checker..
        # https://github.com/systemd/systemd/issues/8072 
        # https://unix.stackexchange.com/questions/493187/systemd-under-ubuntu-18-04-1-fails-with-failed-to-create-user-slice-serv
        p.add_argument('--no-verify', action=VerifyOff, nargs=0, help='Skip systemctl verify step')

    p = argparse.ArgumentParser('''
dron -- simple frontend for Systemd, inspired by cron.

- *d* stands for 'Systemd'
- *ron* stands for 'cron'

dron is my attempt to overcome things that make working with Systemd tedious
'''.strip(),
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

    sp = p.add_subparsers(dest='mode')
    mp = sp.add_parser('managed', help='List units managed by dron')
    mp.add_argument('--long', '-l' , action='store_true', help='Longer listing format')
    mp.add_argument('--watch', '-w', action='store_true', help='Watch regularly')
    sp.add_parser('timers', help='List all timers') # TODO timers doesn't really belong here?
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


def main():
    p = make_parser()
    args = p.parse_args()

    mode = args.mode

    def tabfile_or_default():
        tabfile = args.tabfile
        if tabfile is None:
            click.confirm(f'Use default tabfile: {DRONTAB}?', default=True, abort=True)
            tabfile = DRONTAB
        return tabfile

    if mode == 'managed':
        # TODO hacky...
        watch = args.watch
        if watch:
            argv = [a for a in sys.argv if a not in {'-w', '--watch'}]
            os.execvp(
                'watch',
                [
                    'watch',
                    '--color',
                    '-n', '1', # TODO make configurable?
                    *argv,
                ],
            )
        else:
            cmd_managed(long_=args.long)
    elif mode == 'timers': # TODO rename to 'monitor'?
        cmd_timers()
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
