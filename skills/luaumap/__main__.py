"""Entry point: python Research/tools/luaumap <command> ...  (stdlib only, Python 3.8+)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
