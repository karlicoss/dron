#!/usr/bin/env python3
import argparse


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument('--job', required=True)
    return p
