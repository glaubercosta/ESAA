from __future__ import annotations

import sys


def greet(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        return "Hello, world!"
    return f"Hello, {cleaned}!"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    name = args[0] if args else ""
    print(greet(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
