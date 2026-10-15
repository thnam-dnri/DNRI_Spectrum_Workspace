#!/usr/bin/env python3
"""Build the GUI with PyInstaller on its native Linux or macOS host."""
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    system = platform.system()
    if system not in ("Linux", "Darwin"):
        raise SystemExit("Build this source on Linux or macOS.")
    output = ROOT / "outputs/dsw"
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--onedir", "--name", "DSW", "--paths", str(ROOT / "src/dsw"),
               "--distpath", str(output / "dist"),
               "--workpath", str(output / "build"),
               "--specpath", str(output / "build")]
    if system == "Darwin":
        command += ["--windowed", "--osx-bundle-identifier", "vn.dnri.dsw"]
    command += [str(ROOT / "scripts/run_dsw.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    resources = output / ("dist" if system == "Darwin" else "dist/DSW")
    shutil.copytree(ROOT / "tests/dsw/fixtures/examples", resources / "examples",
                    dirs_exist_ok=True)
    shutil.copytree(ROOT / "docs", resources / "docs", dirs_exist_ok=True)
    artifact = output / ("dist/DSW.app" if system == "Darwin" else "dist/DSW/DSW")
    if not artifact.exists():
        raise RuntimeError(f"Expected build artifact is missing: {artifact}")
    print(f"Built native {system}/{platform.machine()} GUI: {artifact}")
    print("No FPGA programming or serial connection was performed.")


if __name__ == "__main__":
    main()
