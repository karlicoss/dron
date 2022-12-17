def _systemctl(*args):
    return ['systemctl', '--user', *args]


# used to use this, keeping for now just for the refernce
# def old_systemd_emailer() -> None:
#     user = getpass.getuser()
#     X = textwrap.dedent(f'''
#     [Unit]
#     Description=status email for %i to {user}
#
#     [Service]
#     Type=oneshot
#     ExecStart={SYSTEMD_EMAIL} --to {user} --unit %i --journalctl-args "-o cat"
#     # TODO why these were suggested??
#     # User=nobody
#     # Group=systemd-journal
#     ''')
#
#     write_unit(unit=f'status-email@.service', body=X, prefix=SYSTEMD_USER_DIR)
#     # I guess makes sense to reload here; fairly atomic step
#     _daemon_reload()
