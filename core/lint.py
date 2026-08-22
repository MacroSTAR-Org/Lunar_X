"""Run the project's static checks."""

from __future__ import annotations

import subprocess

CHECKS: tuple[tuple[str, ...], ...] = (
    ("ruff", "check", "."),
    ("basedpyright",),
)


def main() -> int:
    """Run Ruff and BasedPyright, returning a failure if either fails."""
    exit_codes = []

    for command in CHECKS:
        print(f"\n$ {' '.join(command)}", flush=True)
        result = subprocess.run(command, check=False)
        exit_codes.append(result.returncode)

    return max(exit_codes, default=0)


if __name__ == "__main__":
    raise SystemExit(main())
