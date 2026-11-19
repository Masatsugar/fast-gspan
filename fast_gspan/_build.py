"""Build the standalone Rust backend for a checkout or an installed package."""

import os
import platform
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

PACKAGE_DIR = Path(__file__).resolve().parent
BUILD_DIR = PACKAGE_DIR / "vendor" / "gbolt" / "build"


def macos_deployment_target() -> str:
    default = "11.0" if platform.machine() == "arm64" else "10.14"
    return os.environ.get("MACOSX_DEPLOYMENT_TARGET") or default


def build_binary() -> Path:
    crate = PACKAGE_DIR / "native"
    if not (crate / "Cargo.toml").is_file():
        crate = PACKAGE_DIR.parent / "native"

    env = os.environ.copy()
    if platform.system() == "Darwin":
        env["MACOSX_DEPLOYMENT_TARGET"] = macos_deployment_target()

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    # A failed or concurrent rebuild must leave a runnable executable behind.
    with TemporaryDirectory(dir=BUILD_DIR) as target:
        try:
            subprocess.run(
                [
                    "cargo", "build", "--release", "--locked",
                    "--manifest-path", str(crate / "Cargo.toml"),
                    "--target-dir", target,
                ],
                check=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Install Rust (rustc and cargo) to build gbolt.") from exc

        binary = BUILD_DIR / "gbolt"
        (Path(target) / "release" / "gbolt").replace(binary)
    return binary
