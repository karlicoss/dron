#!/usr/bin/env python3
import getpass
from pathlib import Path
import shutil
from subprocess import check_call, CalledProcessError, run, PIPE, check_output
from typing import NamedTuple, Union, Sequence, Optional


# TODO timer spec?
class Timer(NamedTuple):
    pass

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
When = str

# TODO how to come up with good implicit job name?
def timer(*, unit_name: str, when: When) -> str:
    return f'''
# managed by systemdtab
[Unit]
Description=Timer for {unit_name}

[Timer]
OnCalendar={when}
'''

PathIsh = Union[str, Path]

Command = Union[PathIsh, Sequence[PathIsh]]

from tempfile import NamedTemporaryFile, TemporaryDirectory

def verify(*, unit_name: str, contents: str):
    # TODO ugh pipe doesn't work??
# systemd-analyze --user verify <(cat systemdtab-test.service)       I  18:58:02  
# Failed to prepare filename /proc/self/fd/11: Invalid argument
    with TemporaryDirectory() as tdir:
        sfile = (Path(tdir) / unit_name).with_suffix('.service')
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
            verify(unit_name='whatever.service', contents=contents)

    def ok(contents):
        verify(unit_name='ok.service', contents=contents)

    ok(contents='''
[Service]
ExecStart=echo 123
''')

    ok(contents=unit(unit_name='alala', command='echo 123'))

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


def unit(*, unit_name: str, command: Command) -> str:
    # TODO allow to pass extra args
    # TODO FIXME think carefully about escaping etc?
    res = f'''
# managed by systemdtab
# TODO description unnecessary?
[Service]
ExecStart=bash -c "{command}"
# StandardOutput=file:/L/tmp/alala.log
Systemdtab=true

[Unit]
OnFailure=status-email@%n.service
Requires=systemdtab.target

'''
    # TODO not sure if should include username??
    return res
# TODO need to install systemdtab-email thing?
# TODO dunno, separate script might be nicer to test?

    # TODO FIXME

# https://bugs.python.org/issue31528 eh, probably can't use configparser.. plaintext is good enough though.


# TODO later, implement logic for cleaning up old jobs


# TODO FIXME mkdir in case it doesn't exist..


DIR = Path("~/.config/systemd/user").expanduser()

# TODO not sure if should do one by one or all at once?
# yeah, makes sense to do all at once...
# TODO warn about dirty state?



def scu(*args, method=check_call, **kwargs):
    return method(['systemctl', '--user', *args], **kwargs) # TODO status???


def write_unit(*, unit_name: str, contents: str) -> None:
    # TODO contextmanager?
    verify(unit_name=unit_name, contents=contents)
    # TODO eh?

    uservice = unit_name + '.service'
    (DIR / uservice).write_text(contents)


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
    write_unit(unit_name=f'status-email@', contents=X)
    # I guess makes sense to reaload here; fairly atomic step
    scu('daemon-reload')


def finalize():
    scu('daemon-reload')


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
    u = unit(unit_name=unit_name, command=command)
    write_unit(unit_name=unit_name, contents=u)

    if when is not None:
        t = timer(unit_name=unit_name, when=when)
        utimer = unit_name + '.timer'
        (DIR / utimer).write_text(t)
        scu('start', utimer)
    # TODO otherwise just unit status or something?

    # TODO FIXME enable?
    # TODO verify everything before starting to update
    # TODO copy files with rollback? not sure how easy it is..





def test():
    # TODO 'fake' systemd dir?
    # job(
    #     # cmd=, # TODO allow taking lists and strings?

    # )
    pass

import argparse

def main():
    # TODO not sure if should use main?
    # scu list-unit-files --no-pager --no-legend
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest='mode')
    m = sp.add_parser('managed')
    args = p.parse_args()

    mode = args.mode; assert mode is not None

    if mode == 'managed':
        res = scu('list-unit-files', '--no-pager', '--no-legend', method=check_output).decode('utf8')
        units = [x.split()[0] for x in res.splitlines()]
        for u in units:
            # meh. but couldn't find any better way to filter a subset of systemd properties...
            # e.g. sc show only displays 'known' properties.
            # could filter by description? but bit too restrictive?
            res = scu('cat', u, method=check_output).decode('utf8')
            if 'Systemdtab=true' in res:
                print(u)



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
