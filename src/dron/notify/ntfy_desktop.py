#!/usr/bin/env python3
from .common import get_parser, IS_SYSTEMD
from .ntfy_common import run_ntfy


BACKEND = 'linux' if IS_SYSTEMD else 'darwin'


def main() -> None:
    p = get_parser()
    args = p.parse_args()

    run_ntfy(job=args.job, backend=BACKEND)


if __name__ == '__main__':
    main()
