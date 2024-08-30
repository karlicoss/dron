from .common import IS_SYSTEMD, get_parser
from .ntfy_common import run_ntfy

BACKEND = 'linux' if IS_SYSTEMD else 'darwin'


def main() -> None:
    p = get_parser()
    args = p.parse_args()

    run_ntfy(job=args.job, backend=BACKEND)


if __name__ == '__main__':
    main()
