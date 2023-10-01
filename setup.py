# see https://github.com/karlicoss/pymplate for up-to-date reference


from setuptools import setup, find_namespace_packages  # type: ignore


def main() -> None:
    # works with both ordinary and namespace packages
    pkgs = find_namespace_packages('src')
    pkg = min(pkgs) # lexicographically smallest is the correct one usually?
    setup(
        name=pkg,
        use_scm_version={
            'version_scheme': 'python-simplified-semver',
            'local_scheme': 'dirty-tag',
        },
        setup_requires=['setuptools_scm'],

        # otherwise mypy won't work
        # https://mypy.readthedocs.io/en/stable/installed_packages.html#making-pep-561-compatible-packages
        zip_safe=False,

        packages=pkgs,
        package_dir={'': 'src'},
        # necessary so that package works with mypy
        package_data={pkg: ['py.typed']},

        install_requires=[
            'click'         ,  # CLI
            'prompt_toolkit',  # CLI
            'tabulate'      ,  # for monitor
            'termcolor'     ,  # for monitor

            'mypy'          ,  # for checking units
            'loguru'        ,  # nicer logging

            # vvv example of git repo dependency
            # 'repo @ git+https://github.com/karlicoss/repo.git',

            # vvv  example of local file dependency. yes, DUMMY is necessary for some reason
            # 'repo @ git+file://DUMMY/path/to/repo',
        ],
        extras_require={
            ':sys_platform != "darwin"': [
                'dbus-python',  # dbus interface to systemd
            ],
            'testing': ['pytest'],
            'linting': ['pytest', 'mypy', 'lxml'], # lxml for mypy coverage report
            # NOTE: need ntfy[telegram] for telegram notifications
            # in addition, after that need to force install python-telegram-bot<20.0, otherwise it breaks 
            # see https://github.com/rahiel/telegram-send/issues/115#issuecomment-1368728425
        },

        entry_points={'console_scripts': ['dron = dron:main']},

        # this needs to be set if you're planning to upload to pypi
        # url='',
        # author='',
        # author_email='',
        # description='',

        # Rest of the stuff -- classifiers, license, etc, I don't think it matters for pypi
        # it's just unnecessary duplication
    )


if __name__ == '__main__':
    main()

