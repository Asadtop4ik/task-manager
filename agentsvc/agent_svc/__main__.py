"""`python3 -m agent_svc run|self-check`."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
