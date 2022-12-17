# see https://github.com/karlicoss/pymplate for up-to-date reference


from setuptools import setup, find_namespace_packages # type: ignore


def main() -> None:
    name = 'dron'
    setup(
        name=name,

        # otherwise mypy won't work
        # https://mypy.readthedocs.io/en/stable/installed_packages.html#making-pep-561-compatible-packages
        zip_safe=False,

        packages=[name],
        package_data={name: ['py.typed']},

        install_requires=[
            'click',       # CLI interactions
            'tabulate'   , # for monitor
            'termcolor'  , # for monitor

            'mypy'       , # for checking units

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
        },

        entry_points={'console_scripts': ['dron = dron:main', 'systemd_email = dron.systemd_email:main']},

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
