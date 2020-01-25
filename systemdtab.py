#!/usr/bin/env python3
import argparse
import getpass
import os
from pathlib import Path
import shutil
from subprocess import check_call, CalledProcessError, run, PIPE, check_output
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import NamedTuple, Union, Sequence, Optional, Iterator, Tuple, Iterable, List


from kython.klogging2 import LazyLogger # type: ignore


logger = LazyLogger('systemdtab', level='debug')

DIR = Path("~/.config/systemd/user").expanduser()
# TODO FIXME mkdir in case it doesn't exist..


PathIsh = Union[str, Path]


# def scu(*args, method=check_call, **kwargs):
#     return method(['systemctl', '--user', *args], **kwargs) # TODO status???

def scu(*args):
    return ['systemctl', '--user', *args]


def reload():
    check_call(scu('daemon-reload'))


MANAGED_MARKER = 'Systemdtab=true'
def is_managed(body: str):
    return MANAGED_MARKER in body


def test_managed():
    assert is_managed(timer(unit_name='whatever', when='daily'))

    custom = '''
[Service]
ExecStart=echo 123
'''
    verify(unit_file='other.service', body=custom) # precondition
    assert not is_managed(custom)


When = str
# TODO how to come up with good implicit job name?
def timer(*, unit_name: str, when: When) -> str:
    return f'''
# managed by systemdtab
[Unit]
Description=Timer for {unit_name}
{MANAGED_MARKER}

[Timer]
OnCalendar={when}
'''


Command = Union[PathIsh, Sequence[PathIsh]]

def ncmd(command: Command) -> List[str]:
    if isinstance(command, (str, Path)):
        return ncmd([command])
    else:
        return [str(c) for c in command]



# TODO allow to pass extra args
def service(*, unit_name: str, command: Command) -> str:
    # TODO FIXME think carefully about escaping command etc?
    nc = ncmd(command)
    # TODO not sure how to handle this properly...
    cmd = ' '.join(nc)

    res = f'''
# managed by systemdtab
[Unit]
Description=Service for {unit_name}
OnFailure=status-email@%n.service

[Service]
ExecStart={cmd}
# StandardOutput=file:/L/tmp/alala.log
{MANAGED_MARKER}
'''
    # TODO not sure if should include username??
    return res


def verify(*, unit_file: str, body: str):
    # ugh. pipe doesn't work??
    # systemd-analyze --user verify <(cat systemdtab-test.service)
    # Failed to prepare filename /proc/self/fd/11: Invalid argument
    with TemporaryDirectory() as tdir:
        sfile = Path(tdir) / unit_file
        sfile.write_text(body)
        res = run(['systemd-analyze', '--user', 'verify', str(sfile)], stdout=PIPE, stderr=PIPE)
        if res.returncode != 0:
            raise RuntimeError(res)
        out = res.stdout
        err = res.stderr
        assert out == b'', out
        lines = err.splitlines()
        lines = [l for l in lines if b"Unknown lvalue 'Systemdtab'" not in l] # meh
        assert len(lines) == 0, err


def test_verify():
    import pytest # type: ignore[import]
    def fails(body):
        with pytest.raises(Exception):
            verify(unit_file='whatever.service', body=body)

    def ok(body):
        verify(unit_file='ok.service', body=body)

    ok(body='''
[Service]
ExecStart=echo 123
''')

    ok(body=service(unit_name='alala', command='echo 123'))

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
    src = Path(__file__).absolute().parent / 'systemd-email'
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
ExecStart={target} {user} %i
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

# TODO think about arg names?
# TODO not sure if should give it default often?
# TODO when first? so it's more compat to crontab..
def job(when: Optional[When], command: Command, *, unit_name: Optional[str]=None) -> Job:
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
    )



def managed_units() -> Iterator[str]:
    res = check_output(scu('list-unit-files', '--no-pager', '--no-legend')).decode('utf8')
    units = [x.split()[0] for x in res.splitlines()]
    for u in units:
        # meh. but couldn't find any better way to filter a subset of systemd properties...
        # e.g. sc show only displays 'known' properties.
        # could filter by description? but bit too restrictive?
        res = check_output(scu('cat', u)).decode('utf8')
        if is_managed(res):
            yield u

Unit = str
Body = str
Plan = Iterable[Tuple[Unit, Body]]

def plan(jobs: Iterable[Job]) -> Plan:
    def check(unit_file, body):
        verify(unit_file=unit_file, body=body)
        return (unit_file, body)

    for j in jobs:
        s = service(unit_name=j.unit_name, command=j.command)
        yield check(j.unit_name + '.service', s)
        # write_unit(unit_file=j.unit_name + '.service', contents=s) T

        when = j.when
        if when is None:
            continue
        t = timer(unit_name=j.unit_name, when=when)
        yield check(j.unit_name + '.timer', t)

        # write_unit(unit_file=unit_name + '.timer', contents=t)
        # TODO not sure what should be started? timers only I suppose
        # check_call(scu('start', unit_name + '.timer'))
    # TODO otherwise just unit status or something?

    # TODO FIXME enable?
    # TODO verify everything before starting to update
    # TODO copy files with rollback? not sure how easy it is..


def apply_plan(pjobs: Plan) -> None:
    plist = list(pjobs)

    # TODO ugh. how to test this properly?
    current = list(sorted(managed_units()))
    pending = list(p[0] for p in plist)

    to_disable = [u for u in current if u not in pending]
    if len(to_disable) == len(current):
        raise RuntimeError(f"Something might be wrong: current {current}, pending {pending}")
    if len(to_disable) > 0:
        logger.info('Disabling: %s', to_disable)

    for u in to_disable:
        # TODO stop timer first?
        check_call(scu('stop', u))
    for u in to_disable:
        (DIR / u).unlink() # TODO eh. not sure what do we do with user modifications?


    # TODO FIXME logging...
    # TODO FIXME undo first?
    for unit_file, body in plist:
        write_unit(unit_file=unit_file, body=body)

    reload()


def manage(jobs: Iterable[Job]) -> None:
    pjobs = plan(jobs)
    # TOOD assert nonzero plan?

    apply_plan(pjobs)


def main():
    # TODO not sure if should use main?
    # scu list-unit-files --no-pager --no-legend
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest='mode')
    sp.add_parser('managed')
    sp.add_parser('timers')
    args = p.parse_args()

    mode = args.mode; assert mode is not None

    if mode == 'managed':
        for u in managed_units():
            print(u)
    elif mode == 'timers':
        os.execvp('watch', ['watch', '-n', '0.5', ' '.join(scu('list-timers'))])
    else:
        raise RuntimeError(mode)
    # TODO need self install..
    # TODO add edit command; open sdtab file in EDITOR; lint if necessary (link commands specified in the file)
    # after linting, carry on to applying


if __name__ == '__main__':
    main()


# TODO stuff I learnt:
# TODO  systemd-analyze --user unit-paths 
# TODO blame!
#  systemd-analyze verify -- check syntax


# TODO test via systemd??

# TODO would be nice to revert... via contextmanager?
# TODO assert that managed by systemdtab
# TODO name it systemdsl?
# sdcron? sdtab?
# TODO not sure what rollback should do w.r.t to
# TODO perhaps, only reenable changed ones? ugh. makes it trickier...

# TODO wonder if I remove timers, do they drop counts?
# TODO FIXME ok, for now, it's fine, but with more sophisticated timers might be a bit annoying

# TODO use python's literate types?
# TODO
#        The following special expressions may be used as shorthands for longer normalized forms:
#
#                minutely → *-*-* *:*:00
#                  hourly → *-*-* *:00:00
#                   daily → *-*-* 00:00:00
#                 monthly → *-*-01 00:00:00
#                  weekly → Mon *-*-* 00:00:00
#                  yearly → *-01-01 00:00:00
#               quarterly → *-01,04,07,10-01 00:00:00
#            semiannually → *-01,07-01 00:00:00


# TODO wow, that's quite annoying. so timer has to be separate file. oh well.


# TODO tui for confirming changes, show short diff?

# TODO actually for me, stuff like 'hourly' makes little sense; I usually space out in time..

# TODO need to install systemdtab-email thing?
# TODO dunno, separate script might be nicer to test?


# https://bugs.python.org/issue31528 eh, probably can't use configparser.. plaintext is good enough though.


# TODO later, implement logic for cleaning up old jobs


# TODO not sure if should do one by one or all at once?
# yeah, makes sense to do all at once...
# TODO warn about dirty state?


# TODO test with 'fake' systemd dir?


# TODO the assumption is that managed jobs are not changed manually, or changed in a way that doesn't break anything
# in general it's impossible to prevent anyway


# TODO change log formats for emails? not that I really need pids..
