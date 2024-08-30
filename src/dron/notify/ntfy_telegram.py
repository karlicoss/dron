from .common import get_parser
from .ntfy_common import run_ntfy


def main() -> None:
    p = get_parser()
    args = p.parse_args()
    run_ntfy(job=args.job, backend='telegram')


if __name__ == '__main__':
    main()
