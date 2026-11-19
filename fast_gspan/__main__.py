"""Manage the bundled Rust backend with python -m fast_gspan."""

import shutil
import subprocess
import sys

from ._build import BUILD_DIR, build_binary


def main():
    if sys.argv[1:] == ["build"]:
        try:
            binary = build_binary()
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            sys.exit(str(exc))
        print(f"Built {binary}")
    elif sys.argv[1:] == ["clean"]:
        if BUILD_DIR.exists():
            shutil.rmtree(BUILD_DIR)
            print("Build directory removed.")
        else:
            print("Nothing to clean.")
    else:
        sys.exit("Usage: python -m fast_gspan {build|clean}")


if __name__ == "__main__":
    main()
