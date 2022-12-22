#!/usr/bin/env python3
from .common import get_parser
from .ntfy_common import run_ntfy


def main() -> None:
    p = get_parser()
    args = p.parse_args()
    run_ntfy(job=args.job, backend='linux')


if __name__ == '__main__':
    main()
