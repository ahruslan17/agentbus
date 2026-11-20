"""Apply SQL migrations. Safe to run repeatedly: every statement is idempotent."""

from __future__ import annotations

import os
import sys

from agentbus import store
from agentbus.config import DEFAULT_DATABASE_URL


def main() -> int:
    url = os.environ.get("AGENTBUS_DATABASE_URL", DEFAULT_DATABASE_URL)
    with store.connect(url) as conn:
        applied = store.init_schema(conn)
    print(f"schema applied: {', '.join(applied)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
