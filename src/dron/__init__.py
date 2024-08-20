# NOTE: backwards compatibility, now relying on dron.__main__
# should probably remove this later
from .dron import main

if __name__ == '__main__':
    main()
