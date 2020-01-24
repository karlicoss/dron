#!/usr/bin/env python3
from pathlib import Path
from subprocess import check_call
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
        check_call(['systemd-analyze', '--user', 'verify', str(sfile)])


def unit(*, unit_name: str, command: Command) -> str:
    # TODO allow to pass extra args
    res = f'''
# managed by systemdtab
# TODO description unnecessary?
[Service]
ExecStart={command}
'''
    return res

    # TODO FIXME

# https://bugs.python.org/issue31528 eh, probably can't use configparser.. plaintext is good enough though.


# TODO later, implement logic for cleaning up old jobs


# TODO FIXME mkdir in case it doesn't exist..


DIR = Path("~/.config/systemd/user").expanduser()

# TODO not sure if should do one by one or all at once?
# yeah, makes sense to do all at once...
# TODO warn about dirty state?



def scu(*args, **kwargs):
    check_call(['systemctl', '--user', *args], **kwargs) # TODO status???


# TODO think about arg names?
# TODO not sure if should give it default often?
# TODO when first? so it's more compat to crontab..
def job(when: When, command: Command, *, unit_name: Optional[str]=None):
    assert unit_name is not None
    # TODO generate unit name
    # TODO not sure about names.
    # I guess warn user about non-unique names and prompt to give a more specific name?
    u = unit(unit_name=unit_name, command=command)
    verify(unit_name=unit_name, contents=u)

    t = timer(unit_name=unit_name, when=when)
    # TODO would be nice to revert....
    # TODO assert that managed by systemdtab
    # TODO name it systemdsl?
    # TODO not sure what rollback should do w.r.t to
    # TODO perhaps, only reenable changed ones? ugh. makes it trickier...
    uservice = unit_name + '.service'
    utimer = unit_name + '.timer'
    (DIR / uservice).write_text(u)
    (DIR / utimer).write_text(t)
    # TODO FIXME enable?
    scu('start', utimer)
    scu('status', utimer)
    # TODO list-timers --all?





def test():
    # TODO 'fake' systemd dir?
    # job(
    #     # cmd=, # TODO allow taking lists and strings?

    # )
    pass


def main():
    # TODO not sure if should use main?
    pass


if __name__ == '__main__':
    main()


# TODO stuff I learnt:
# TODO  systemd-analyze --user unit-paths 
# TODO blame!
#  systemd-analyze verify -- check syntax
