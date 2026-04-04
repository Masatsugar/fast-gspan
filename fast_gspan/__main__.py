"""CLI entry point: python -m fast_gspan build"""

import shutil
import subprocess
import sys
from pathlib import Path

VENDOR_GBOLT_DIR = Path(__file__).parent / "vendor" / "gbolt"


def _check_tool(name: str, cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"  {name}: not found")
        return False


def build():
    """Build the vendored gBolt C++ source."""
    print("Building gBolt from vendored source...")

    if not VENDOR_GBOLT_DIR.exists():
        print(f"Error: vendor source not found at {VENDOR_GBOLT_DIR}")
        sys.exit(1)

    # Check prerequisites
    ok = True
    for name, cmd in [
        ("cmake", ["cmake", "--version"]),
        ("make", ["make", "--version"]),
    ]:
        if not _check_tool(name, cmd):
            ok = False
    if not ok:
        print("\nPlease install the missing build tools.")
        sys.exit(1)

    build_dir = VENDOR_GBOLT_DIR / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir()

    print("  Configuring (cmake) ...")
    result = subprocess.run(
        ["cmake", ".."],
        cwd=build_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"cmake failed:\n{result.stderr}")
        sys.exit(1)

    print("  Compiling (make) ...")
    result = subprocess.run(
        ["make", "-j4", "-k"],
        cwd=build_dir,
        capture_output=True,
        text=True,
    )

    exe = build_dir / "gbolt"
    if exe.exists():
        print(f"  gBolt built successfully: {exe}")
    else:
        print(f"  Build failed:\n{result.stderr}")
        sys.exit(1)


def clean():
    """Remove the gBolt build directory."""
    build_dir = VENDOR_GBOLT_DIR / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
        print("Build directory removed.")
    else:
        print("Nothing to clean.")


def usage():
    print("Usage: python -m fast_gspan <command>")
    print()
    print("Commands:")
    print("  build   Build the gBolt C++ backend")
    print("  clean   Remove the gBolt build directory")


def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "build":
        build()
    elif cmd == "clean":
        clean()
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
