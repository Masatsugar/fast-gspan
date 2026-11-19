"""Bundle the Rust executable in a Python-independent, platform-specific wheel."""

import os
import platform
import runpy
import sysconfig
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # The isolated build environment does not have pandas or NetworkX.
        # Load the builder without importing the public fast_gspan package.
        root = Path(self.root)
        builder = runpy.run_path(str(root / "fast_gspan" / "_build.py"))
        binary = builder["build_binary"]()
        build_data["force_include"][str(binary)] = "fast_gspan/vendor/gbolt/build/gbolt"
        build_data["pure_python"] = False

        if platform.system() == "Darwin":
            target = builder["macos_deployment_target"]().replace(".", "_")
            tag = f"macosx_{target}_{platform.machine()}"
        else:
            # Official Linux wheels are built inside a manylinux container.
            tag = os.environ.get("AUDITWHEEL_PLAT") or sysconfig.get_platform()
            tag = tag.replace("-", "_").replace(".", "_")
        build_data["tag"] = f"py3-none-{tag}"
