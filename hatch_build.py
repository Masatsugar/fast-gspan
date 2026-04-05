"""Custom hatch build hook that compiles gBolt during wheel creation."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


VENDOR_GBOLT = Path(__file__).parent / "fast_gspan" / "vendor" / "gbolt"
BINARY_NAME = "gbolt"


def _detect_wheel_tag() -> str:
    """Return a platform tag suitable for the current build host."""
    machine = platform.machine().lower()
    system = platform.system()

    if system == "Linux":
        # Use manylinux2014 (glibc >= 2.17) as a safe baseline
        arch = machine  # x86_64, aarch64, etc.
        return f"py3-none-manylinux_2_17_{arch}"
    elif system == "Darwin":
        if machine == "arm64":
            return "py3-none-macosx_11_0_arm64"
        else:
            return "py3-none-macosx_10_14_x86_64"
    else:
        # Fallback – let hatchling decide
        return f"py3-none-{system.lower()}_{machine}"


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        # Only compile for wheel builds, not sdist
        if self.target_name != "wheel":
            return

        build_dir = VENDOR_GBOLT / "build"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir()

        # 1. cmake (Release build for smaller binary)
        cmake_cmd = ["cmake", "..", "-DCMAKE_BUILD_TYPE=Release"]
        result = subprocess.run(
            cmake_cmd, cwd=build_dir, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"cmake failed:\n{result.stderr}", file=sys.stderr)
            raise RuntimeError("cmake configuration failed")

        # 2. make
        result = subprocess.run(
            ["make", "-j4"], cwd=build_dir, capture_output=True, text=True,
        )
        binary = build_dir / BINARY_NAME
        if not binary.exists():
            print(f"make failed:\n{result.stderr}", file=sys.stderr)
            raise RuntimeError("gBolt compilation failed")

        # 3. strip to reduce size
        if shutil.which("strip"):
            subprocess.run(["strip", str(binary)], check=False)

        # 4. Include the binary in the wheel
        rel_dest = os.path.join(
            "fast_gspan", "vendor", "gbolt", "build", BINARY_NAME
        )
        build_data["force_include"][str(binary)] = rel_dest

        # 5. Set platform-specific wheel tag
        build_data["tag"] = _detect_wheel_tag()
