"""
Build script for Little Helper
Creates executable using PyInstaller
"""

import os
import sys
import shutil
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(SCRIPT_DIR, "dist")
BUILD_DIR = os.path.join(SCRIPT_DIR, "build")
INNO_SETUP = r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"


def clean():
    """Remove build artifacts."""
    for path in [DIST_DIR, BUILD_DIR]:
        if os.path.exists(path):
            shutil.rmtree(path)
            print(f"Removed: {path}")


def build():
    """Build executable with PyInstaller using LittleHelper.spec."""
    spec_file = os.path.join(SCRIPT_DIR, "LittleHelper.spec")
    if not os.path.exists(spec_file):
        print(f"ERROR: {spec_file} not found!")
        return None

    cmd = [
        sys.executable, "-m", "PyInstaller",
        spec_file,
    ]

    print("Running PyInstaller...")
    print(" ".join(cmd))
    subprocess.run(cmd, cwd=SCRIPT_DIR, check=True)
    
    exe_path = os.path.join(DIST_DIR, "LittleHelper.exe")
    if os.path.exists(exe_path):
        print(f"\nBuild successful: {exe_path}")
        return exe_path
    else:
        print("Build failed!")
        return None


def main():
    print("=" * 50)
    print("Little Helper - Build Script")
    print("=" * 50)
    
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        clean()
        return
    
    clean()
    exe_path = build()
    
    if exe_path:
        build_installer()

    print("\n" + "=" * 50)
    print("All done!" if exe_path else "Build failed!")
    print("=" * 50)


def build_installer():
    """Run Inno Setup to create the installer."""
    iss_file = os.path.join(SCRIPT_DIR, "setup.iss")
    if not os.path.exists(iss_file):
        print("setup.iss not found, skipping installer.")
        return

    if not os.path.exists(INNO_SETUP):
        print(f"Inno Setup not found at: {INNO_SETUP}")
        print("Skipping installer creation.")
        return

    installer_dir = os.path.join(SCRIPT_DIR, "installer")
    os.makedirs(installer_dir, exist_ok=True)

    print("\nRunning Inno Setup...")
    subprocess.run([INNO_SETUP, iss_file], cwd=SCRIPT_DIR, check=True)

    installer = os.path.join(installer_dir, "LittleHelper-Setup.exe")
    if os.path.exists(installer):
        print(f"Installer: {installer}")


if __name__ == "__main__":
    main()
