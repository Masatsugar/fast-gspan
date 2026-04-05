"""Custom hatch build hook that compiles gBolt during wheel creation.

Handles:
- Compiling gBolt C++ source via cmake + make
- Stripping the binary for size reduction
- Bundling shared libraries (libomp on macOS) for self-contained wheels
- Setting platform-specific wheel tags
"""

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


VENDOR_GBOLT = Path(__file__).parent / "fast_gspan" / "vendor" / "gbolt"
BINARY_NAME = "gbolt"
LIB_DIR_NAME = "lib"  # subdirectory for bundled shared libraries


def _detect_wheel_tag() -> str:
    """Return a platform tag suitable for the current build host."""
    machine = platform.machine().lower()
    system = platform.system()

    if system == "Linux":
        arch = machine  # x86_64, aarch64, etc.
        return f"py3-none-manylinux_2_17_{arch}"
    elif system == "Darwin":
        if machine == "arm64":
            return "py3-none-macosx_11_0_arm64"
        else:
            return "py3-none-macosx_10_14_x86_64"
    else:
        return f"py3-none-{system.lower()}_{machine}"


def _bundle_macos_dylibs(binary: Path, build_dir: Path, force_include: dict):
    """Find and bundle dynamic libraries that the gBolt binary links to on macOS.

    Uses otool to discover non-system dylibs (e.g. libomp.dylib from Homebrew),
    copies them into a lib/ directory next to the binary, and rewrites the binary's
    load commands so it finds them at @loader_path/lib/.
    """
    otool = shutil.which("otool")
    install_name_tool = shutil.which("install_name_tool")
    if not otool or not install_name_tool:
        print("Warning: otool/install_name_tool not found; skipping dylib bundling",
              file=sys.stderr)
        return

    result = subprocess.run(
        [otool, "-L", str(binary)], capture_output=True, text=True,
    )
    if result.returncode != 0:
        return

    lib_dir = build_dir / LIB_DIR_NAME
    lib_dir.mkdir(exist_ok=True)

    # System prefixes that should NOT be bundled
    system_prefixes = ("/usr/lib/", "/System/")

    for line in result.stdout.strip().splitlines()[1:]:  # skip first line (binary name)
        line = line.strip()
        match = re.match(r"(.+\.dylib)\s+\(", line)
        if not match:
            continue
        dylib_path = match.group(1).strip()

        # Skip system libraries
        if any(dylib_path.startswith(p) for p in system_prefixes):
            continue
        # Skip @rpath/@loader_path references (already relative)
        if dylib_path.startswith("@"):
            continue

        src = Path(dylib_path)
        if not src.exists():
            print(f"Warning: dylib not found: {dylib_path}", file=sys.stderr)
            continue

        dst = lib_dir / src.name
        shutil.copy2(str(src), str(dst))
        print(f"Bundling {src.name}")

        # Rewrite the binary's load command: absolute path -> @loader_path/lib/<name>
        new_path = f"@loader_path/{LIB_DIR_NAME}/{src.name}"
        subprocess.run(
            [install_name_tool, "-change", dylib_path, new_path, str(binary)],
            check=True,
        )

        # Also set the id of the copied dylib
        subprocess.run(
            [install_name_tool, "-id", f"@loader_path/{src.name}", str(dst)],
            check=False,
        )

        # Include the dylib in the wheel
        rel_dest = os.path.join(
            "fast_gspan", "vendor", "gbolt", "build", LIB_DIR_NAME, src.name,
        )
        force_include[str(dst)] = rel_dest


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

        # 4. On macOS, bundle non-system dylibs (e.g. libomp)
        if platform.system() == "Darwin":
            _bundle_macos_dylibs(binary, build_dir, build_data["force_include"])

        # 5. Include the binary in the wheel
        rel_dest = os.path.join(
            "fast_gspan", "vendor", "gbolt", "build", BINARY_NAME,
        )
        build_data["force_include"][str(binary)] = rel_dest

        # 6. Set platform-specific wheel tag
        build_data["tag"] = _detect_wheel_tag()
