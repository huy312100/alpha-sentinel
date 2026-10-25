"""``python -m bot`` - run the AlphaSentinel scanner bot."""

from __future__ import annotations

import asyncio
import sys

from bot.main import main

if __name__ == "__main__":  # pragma: no cover - module entry point
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130) from None
