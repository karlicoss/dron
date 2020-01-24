#!/usr/bin/env python3
import argparse
import getpass
import os
from pathlib import Path
import shutil
from subprocess import check_call, CalledProcessError, run, PIPE, check_output
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import NamedTuple, Union, Sequence, Optional, Iterator


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
    verify(unit_file='other.service', contents=custom) # precondition
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
# TODO allow to pass extra args
def service(*, unit_name: str, command: Command) -> str:
    # TODO FIXME think carefully about escaping command etc?
    res = f'''
# managed by systemdtab
[Unit]
Description=Service for {unit_name}
OnFailure=status-email@%n.service

[Service]
ExecStart=bash -c "{command}"
# StandardOutput=file:/L/tmp/alala.log
{MANAGED_MARKER}
'''
    # TODO not sure if should include username??
    return res


def verify(*, unit_file: str, contents: str):
    # ugh. pipe doesn't work??
    # systemd-analyze --user verify <(cat systemdtab-test.service)
    # Failed to prepare filename /proc/self/fd/11: Invalid argument
    with TemporaryDirectory() as tdir:
        sfile = Path(tdir) / unit_file
        sfile.write_text(contents)
        res = run(['systemd-analyze', '--user', 'verify', str(sfile)], stdout=PIPE, stderr=PIPE)
        res.check_returncode()
        out = res.stdout
        err = res.stderr
        assert out == b'', out
        lines = err.splitlines()
        lines = [l for l in lines if b"Unknown lvalue 'Systemdtab'" not in l] # meh
        assert len(lines) == 0, err


def test_verify():
    import pytest # type: ignore[import]
    def fails(contents):
        with pytest.raises(Exception):
            verify(unit_file='whatever.service', contents=contents)

    def ok(contents):
        verify(unit_file='ok.service', contents=contents)

    ok(contents='''
[Service]
ExecStart=echo 123
''')

    ok(contents=service(unit_name='alala', command='echo 123'))

    # garbage
    fails(contents='fewfewf')

    # no execstart
    fails(contents='''
[Service]
StandardOutput=journal
''')

    fails(contents='''
[Service]
ExecStart=yes
StandardOutput=baaad
''')


def write_unit(*, unit_file: str, contents: str) -> None:
    # TODO contextmanager?
    verify(unit_file=unit_file, contents=contents)
    # TODO eh?
    (DIR / unit_file).write_text(contents)


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
    write_unit(unit_file=f'status-email@.service', contents=X)
    # I guess makes sense to reload here; fairly atomic step
    reload()


def finalize():
    reload()


# TODO think about arg names?
# TODO not sure if should give it default often?
# TODO when first? so it's more compat to crontab..
def job(when: Optional[When], command: Command, *, unit_name: Optional[str]=None):
    """
    when: if None, then timer won't be created (still allows running job manually)

    """
    assert unit_name is not None
    # TODO generate unit name
    # TODO not sure about names.
    # I guess warn user about non-unique names and prompt to give a more specific name?
    s = service(unit_name=unit_name, command=command)
    write_unit(unit_file=unit_name + '.service', contents=s)

    if when is not None:
        t = timer(unit_name=unit_name, when=when)
        write_unit(unit_file=unit_name + '.timer', contents=t)
        check_call(scu('start', unit_name + '.timer'))
    # TODO otherwise just unit status or something?

    # TODO FIXME enable?
    # TODO verify everything before starting to update
    # TODO copy files with rollback? not sure how easy it is..



def managed_units() -> Iterator[str]:
    res = check_output(scu('list-unit-files', '--no-pager', '--no-legend')).decode('utf8')
    units = [x.split()[0] for x in res.splitlines()]
    for u in units:
        # meh. but couldn't find any better way to filter a subset of systemd properties...
        # e.g. sc show only displays 'known' properties.
        # could filter by description? but bit too restrictive?
        res = check_output(scu('cat', u)).decode('utf8')
        if is_managed(res):
            yield res



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
