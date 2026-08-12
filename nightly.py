"""nightly.py — nightly watchlist runner.

The actual watchlist-building logic lives in scan.py::run_nightly().
This small entry point keeps the GitHub Actions workflow explicit and
avoids maintaining a second copy of the nightly pipeline.
"""

from scan import run_nightly


if __name__ == "__main__":
    run_nightly()
