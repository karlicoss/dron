#!/usr/bin/env python3
import argparse
from collections import OrderedDict
from datetime import datetime, timedelta
from difflib import unified_diff
from itertools import tee, groupby
import json
import getpass
import os
import sys
from pathlib import Path
import shlex
import shutil
from subprocess import check_call, CalledProcessError, run, PIPE, check_output, Popen
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import NamedTuple, Union, Sequence, Optional, Iterator, Tuple, Iterable, List, Any, Dict, Set, cast
from functools import lru_cache


import click # type: ignore

try:
    from kython.klogging2 import LazyLogger # type: ignore
except ImportError:
    import logging
    logger = logging.getLogger('dron')
else:
    logger = LazyLogger('dron', level='info')


SYSTEMD_USER_DIR = Path("~/.config/systemd/user").expanduser()

# todo appdirs?
DRON_DIR = Path('~/.config/dron').expanduser()
DIR = DRON_DIR / 'units'

# TODO make factory functions insted and remove mkdir from global scope?
SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
DIR.mkdir(parents=True, exist_ok=True)


DRONTAB = DRON_DIR / 'drontab.py'

PathIsh = Union[str, Path]


# TODO can remove this? although might be useful for tests
VERIFY_UNITS = True
# TODO ugh. verify tries using already installed unit files so if they were bad, everything would fail
# I guess could do two stages, i.e. units first, then timers
# dunno, a bit less atomic though...

fixture: Any
if 'PYTEST' in os.environ: # set by lint script
    import pytest # type: ignore
    fixture = pytest.fixture
else:
    fixture = lambda f: f # no-op otherwise to prevent pytest import


def has_systemd() -> bool:
    if 'GITHUB_ACTION' in os.environ:
        return False
    return True


def skip_if_no_systemd() -> None:
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


Unit = str
Body = str
UnitFile = Path


def scu(*args):
    return ['systemctl', '--user', *args]


def scu_enable(unit_file: UnitFile, *args):
    return scu('enable', unit_file, *args)


def scu_start(unit: Unit, *args):
    return scu('start', unit, *args)


def reload():
    check_call(scu('daemon-reload'))


MANAGED_MARKER = '<MANAGED BY DRON>'
def is_managed(body: str):
    return MANAGED_MARKER in body


def managed_header() -> str:
    return f'''
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
    verify(unit='other.service', body=custom) # precondition
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
{managed_header()}
[Unit]
Description=Timer for {unit_name} {MANAGED_MARKER}

[Timer]
{specs}

[Install]
WantedBy=timers.target
'''.lstrip()


# if it's an str, assume it's already escaped
# otherwise we are responsible for escaping..
Command = Union[PathIsh, Sequence[PathIsh]]

Escaped = str
def escape(command: Command) -> Escaped:
    if isinstance(command, Escaped):
        return command
    elif isinstance(command, Path):
        return escape([command])
    else:
        return ' '.join(shlex.quote(str(part)) for part in command)


def wrap(script: PathIsh, command: Command) -> Escaped:
    return shlex.quote(str(script)) + ' ' + escape(command)


def test_wrap():
    assert wrap('/bin/bash', ['-c', 'echo whatever']) == "/bin/bash -c 'echo whatever'"
    bin = Path('/bin/bash')
    assert wrap(bin, "-c 'echo whatever'") == "/bin/bash -c 'echo whatever'"
    assert wrap(bin, ['echo', bin]) == "/bin/bash echo /bin/bash"
    assert wrap('cat', bin) == "cat /bin/bash"


# TODO allow to pass extra args
def service(*, unit_name: str, command: Command, **kwargs: str) -> str:
    cmd = escape(command)
    # TODO not sure if something else needs to be escaped for ExecStart??

    # TODO ugh. how to allow injecting arbitrary stuff, not only in [Service] section?

    extras = '\n'.join(f'{k}={v}' for k, v in kwargs.items())

    res = f'''
{managed_header()}
[Unit]
Description=Service for {unit_name} {MANAGED_MARKER}
OnFailure=status-email@%n.service

[Service]
ExecStart={cmd}
{extras}
'''.lstrip()
    # TODO not sure if should include username??
    return res


def verify(*, unit: PathIsh, body: str):
    if not VERIFY_UNITS:
        return

    unit_name = Path(unit).name

    # TODO can validate timestamps too? and security? and calendars!


    # ugh. pipe doesn't work??
    # e.g. 'systemd-analyze --user verify <(cat systemdtab-test.service)' results in:
    # Failed to prepare filename /proc/self/fd/11: Invalid argument
    with TemporaryDirectory() as tdir:
        sfile = Path(tdir) / unit_name
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

        msg = f'failed checking {unit_name}, exit code {res.returncode}'
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
            verify(unit='whatever.service', body=body)

    def ok(body):
        verify(unit='ok.service', body=body)

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


def write_unit(*, unit: Unit, body: Body, prefix: Path=DIR) -> None:
    unit_file = prefix / unit

    logger.info('writing unit file: %s', unit_file)
    # TODO contextmanager?
    # I guess doesn't hurt doing it twice?
    verify(unit=unit_file, body=body)
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
    write_unit(unit=f'status-email@.service', body=X, prefix=SYSTEMD_USER_DIR)
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


State = Iterable[Tuple[UnitFile, Body]]


def _sd(s: str) -> str:
    return f'org.freedesktop.systemd1{s}'

class BusManager:
    def __init__(self) -> None:
        from dbus import SessionBus, Interface # type: ignore[import]
        self.Interface = Interface # meh

        self.bus = SessionBus()  # note: SystemBus is for system-wide services
        systemd = self.bus.get_object(_sd(''), '/org/freedesktop/systemd1')
        self.manager = Interface(systemd, dbus_interface=_sd('.Manager'))

    def properties(self, u: Unit):
        service_unit = self.manager.GetUnit(u)
        service_proxy = self.bus.get_object(_sd(''), str(service_unit))
        return self.Interface(service_proxy, dbus_interface='org.freedesktop.DBus.Properties')

    @staticmethod # meh
    def prop(obj, schema: str, name: str):
        return obj.Get(_sd(schema), name)


# TODO shit. updates across the boundairies of base directory are going to be trickier?...
def managed_units(*, with_body: bool=True) -> State:
    bus = BusManager()
    states = bus.manager.ListUnits() # ok nice, it's basically instant

    for state in states:
        name  = state[0]
        descr = state[1]
        if not is_managed(descr):
            continue

        # todo annoying, this call still takes some time... but whatever ok
        props = bus.properties(name)
        # stale = int(bus.prop(props, '.Unit', 'NeedDaemonReload')) == 1
        unit_file = Path(str(bus.prop(props, '.Unit', 'FragmentPath')))
        body = unit_file.read_text() if with_body else None
        body = cast(str, body) # FIXME later.. for now None is only used in monitor anyway
        yield unit_file, body


def test_managed_units() -> None:
    skip_if_no_systemd()

    # shouldn't fail at least
    list(managed_units())

    # TODO ugh. doesn't work on circleci, fails with
    # dbus.exceptions.DBusException: org.freedesktop.DBus.Error.BadAddress: Address does not contain a colon
    # todo maybe don't need it anymore with 20.04 circleci?
    if 'CI' not in os.environ:
        cmd_monitor(MonParams(with_success_rate=True, with_command=True))


def make_state(jobs: Iterable[Job]) -> State:
    def check(unit_name: Unit, body: Body):
        verify(unit=unit_name, body=body)
        # TODO meh. think about it later...
        unit_file = DIR / unit_name
        return (unit_file, body)

    names: Set[Unit] = set()

    for j in jobs:
        uname = j.unit_name

        assert uname not in names, j
        names.add(uname)

        s = service(unit_name=uname, command=j.command, **j.kwargs)
        yield check(uname + '.service', s)

        when = j.when
        if when is None:
            continue
        t = timer(unit_name=uname, when=when)
        yield check(uname + '.timer', t)

    # TODO otherwise just unit status or something?

    # TODO FIXME enable?
    # TODO verify everything before starting to update
    # TODO copy files with rollback? not sure how easy it is..



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
    currentd = OrderedDict(current)
    pendingd = OrderedDict(pending)

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
        check_call(scu('stop'   , a.unit))
        check_call(scu('disable', a.unit))
    for a in deletes:
        (DIR / a.unit).unlink() # TODO eh. not sure what do we do with user modifications?

    # TODO not sure how to support 'dirty' units detection...
    for a in updates:
        unit = a.unit
        diff = list(unified_diff(a.old_body.splitlines(keepends=True), a.new_body.splitlines(keepends=True)))
        if len(diff) == 0:
            continue
        logger.info('updating %s', unit)
        for d in diff:
            sys.stderr.write(d)
        write_unit(unit=a.unit, body=a.new_body)

        if unit.endswith('.timer'):
            # TODO do we need to enable again??
            check_call(scu('restart', a.unit))
        # TODO some option to treat all updates as deletes then adds might be good...

    # TODO more logging?

    for a in adds:
        logger.info('adding %s', a.unit_file)
        # TODO when we add, assert that previous unit wasn't managed? otherwise we overwrite something
        write_unit(unit=a.unit, body=a.body)

    # TODO need to enable services??

    # need to load units before starting the timers..
    reload()
   
    for a in adds:
        unit_file = a.unit_file
        unit = unit_file.name
        logger.info('enabling %s', unit)
        if unit.endswith('.service'):
            # quiet here because it warns that "The unit files have no installation config"
            check_call(scu_enable(unit_file, '--quiet'))
        elif unit.endswith('.timer'):
            check_call(scu_enable(unit_file, '--now'))
        else:
            raise AssertionError(a)

    # TODO not sure if this reload is even necessary??
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
    # todo how to allow these to be defined in tab file?
    linters = [
        # TODO hmm. -m is not friendly with pipx/virtualenv?
        ['mypy', '--no-incremental', '--check-untyped', str(tabfile)],
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
    globs: Dict[str, Any] = {}

    # TODO also need to modify pythonpath here??? ugh!

    pp = str(ppath)
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



from datetime import tzinfo

class Monitor:
    def __init__(self):
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

    @property # type: ignore[misc]
    @lru_cache
    def local_tz(self):
        # TODO warning if tzlocal isn't installed?
        try:
            from tzlocal import get_localzone
            return get_localzone()
        except:
            return self.utc


class MonParams(NamedTuple):
    with_success_rate: bool
    with_command: bool


def _cmd_monitor(managed: State, *, params: MonParams):
    logger.debug('starting monitor...')
    # TODO reorder timers and services so timers go before?
    sd = lambda s: f'org.freedesktop.systemd1{s}'

    mon = Monitor()

    UTCNOW = datetime.now(tz=mon.utc)

    # todo not sure what's difference from colorama?
    import termcolor
    import tabulate

    bus = BusManager()

    lines = []
    names = sorted(u.name for u, _ in managed)
    uname = lambda full: full.split('.')[0] # TODO not very relibable..
    for k, gr in groupby(names, key=uname):
        [service, timer] = gr
        ok = True
        running = False
        if True: # just preserve old indentation..
            cmd = 'n/a'
            status = 'n/a'

            props = bus.properties(timer)
            cal   = bus.prop(props, '.Timer', 'TimersCalendar')
            last  = bus.prop(props, '.Timer', 'LastTriggerUSec')
            next_ = bus.prop(props, '.Timer', 'NextElapseUSecRealtime')

            spec = cal[0][1] # TODO is there a more reliable way to retrieve it??
            # TODO not sure if last is really that useful..

            last_dt = mon.from_usec(last)
            next_dt = mon.from_usec(next_)
            # meh
            # TODO don't think this detects ad-hoc runs
            if next_dt == datetime.max:
                running = True
            if running:
                nexts = termcolor.colored('running now', 'yellow') + '        '
            else:
                # todo print tz in the header?
                # tood ugh. mypy can't handle lru_cache wrapper?
                nexts = next_dt.astimezone(mon.local_tz).replace(tzinfo=None, microsecond=0).isoformat() # type: ignore[arg-type]

            if next_dt == datetime.max:
                left_delta = timedelta(0)
            else:
                left_delta   = next_dt - UTCNOW

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
        # TODO split in two cols?
        # TODO instead of hacking microsecond, use 'NOW' or something?
        schedule = f'next: {nexts}; schedule: {spec}'

        if True: # just preserve indentaion..
            props = bus.properties(service)
            # TODO some summary too? e.g. how often in failed
            # TODO make defensive?
            exec_start = bus.prop(props, '.Service', 'ExecStart')
            result     = bus.prop(props, '.Service', 'Result')
            command =  ' '.join(map(shlex.quote, exec_start[0][1]))

            if params.with_success_rate:
                rate = _unit_success_rate(service)
                rates = f' {rate:.2f}'
            else:
                rates = ''

            if result == 'success':
                color = 'green'
            else:
                color = 'red'
                ok = False

        status = f'{result:<9} {ago:<8}{rates}'
        status = termcolor.colored(status, color)

        xx = [schedule]
        if params.with_command:
            xx.append(command)

        lines.append((ok, running, [k, status, left, '\n'.join(xx)]))
    # todo maybe default ordering could be by running time ... dunno
    lines_ = [l for _, _, l in sorted(lines, key=lambda x: (x[0], not x[1]))]
    # naming is consistent with systemctl --list-timers
    # meh
    tabulate.PRESERVE_WHITESPACE = True
    print(tabulate.tabulate(lines_, headers=['UNIT', 'STATUS/AGO', 'LEFT', 'COMMAND/SCHEDULE']))
    # TODO also 'running now'?


# TODO think if it's worth integrating with timers?
def cmd_monitor(params: MonParams) -> None:
    managed = list(managed_units(with_body=False)) # body slows down this call quite a bit
    if len(managed) == 0:
        print('No managed units!', file=sys.stderr)
    # TODO test it ?
    _cmd_monitor(managed, params=params)


def cmd_timers() -> None:
    os.execvp('watch', ['watch', '-n', '0.5', ' '.join(scu('list-timers', '--all'))])


Json = Dict[str, Any]
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


def cmd_past(unit: Unit):
    mon = Monitor()
    for j in _unit_logs(unit):
        ts = mon.from_usec(j['__REALTIME_TIMESTAMP'])
        msg = j['MESSAGE']
        print(ts.isoformat(), msg)


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

    p.add_argument('--marker', required=False, help=f'Use custom marker instead of default ({MANAGED_MARKER}). Mostly useful for developing/testing.')

    sp = p.add_subparsers(dest='mode')
    mp = sp.add_parser('monitor', help='Monitor services/timers managed by dron')
    mp.add_argument('-n'        ,type=int, default=1, help='-n parameter for watch')
    mp.add_argument('--once'   , action='store_true', help='only call once')
    mp.add_argument('--rate'   , action='store_true', help='Display success rate (unstable and potentially slow)')
    mp.add_argument('--command', action='store_true', help='Display command')
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


    marker = args.marker
    if marker is not None:
        global MANAGED_MARKER
        MANAGED_MARKER = marker


    mode = args.mode

    def tabfile_or_default():
        tabfile = args.tabfile
        if tabfile is None:
            tabfile = DRONTAB
        return tabfile

    if mode == 'monitor':
        # TODO hacky...
        once = args.once
        if not once:
            argv = sys.argv + ['--once']
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
