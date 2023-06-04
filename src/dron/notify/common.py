#!/usr/bin/env python3
import argparse
import platform


IS_SYSTEMD = platform.system() != 'Darwin'  # if not systemd it's launchd


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument('--job', required=True)
    p.add_argument('--stdin', action='store_true')
    return p
