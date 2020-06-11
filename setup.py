from setuptools import setup, find_packages # type: ignore

def main():
    setup(
        name='dron',
        zip_safe=False,
        install_requires=[],
        entry_points={'console_scripts': ['dron = dron:main']},
    )


if __name__ == '__main__':
    main()
