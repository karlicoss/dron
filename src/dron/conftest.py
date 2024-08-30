import pytest


@pytest.fixture(scope='session', autouse=True)
def disable_verify_units_if_no_systemd():
    '''
    If we can't use systemd, we need to suppress systemd-specific linting
    '''
    from . import common
    from .systemd import is_missing_systemd

    reason = is_missing_systemd()
    if reason is not None:
        common.VERIFY_UNITS = False
    try:
        yield
    finally:
        common.VERIFY_UNITS = True
