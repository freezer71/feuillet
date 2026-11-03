import sys


def main() -> int:
    from .ui import run
    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
