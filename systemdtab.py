#!/usr/bin/env python3
from typing import NamedTuple, Union


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

When = str

# TODO how to come up with good implicit job name?
def timer(name: str, when: When) -> str:
    return f'''
# managed by systemdtab
[Unit]
Description=Timer for {name}

[Timer]
OnCalendar={when}
'''

# https://bugs.python.org/issue31528 eh, probably can't use configparser.. plaintext is good enough though.


# TODO later, implement logic for cleaning up old jobs

from pathlib import Path
PathIsh = Union[str, Path]

Command = Union[PathIsh, List[PathIsh]]

# TODO think about arg names?
# TODO not sure if should give it default often?
# TODO when first? so it's more compat to crontab..
def job(cmd: Command, *, when: When):
    pass

def test():
    # TODO 'fake' systemd dir?
    job(
        cmd=, # TODO allow taking lists and strings?

    )
    pass


def main():
    # TODO not sure if should use main?
    pass


if __name__ == '__main__':
    main()
