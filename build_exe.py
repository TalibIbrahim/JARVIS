"""
Build J.A.R.V.I.S. for Windows.

jarvis.spec is the single source of truth for the PyInstaller build; this script just runs it.

    python build_exe.py                 # dist/JARVIS/JARVIS.exe (one-folder build)
    python build_exe.py --clean         # same, after wiping PyInstaller's cache
    python build_exe.py --installer     # build, then compile installer/jarvis.iss with Inno Setup 6 (ISCC.exe)
    python build_exe.py --installer-only  # skip PyInstaller; just compile the installer from the existing dist/JARVIS
"""
import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(ROOT, "jarvis.spec")
ISS = os.path.join(ROOT, "installer", "jarvis.iss")
DIST_DIR = os.path.join(ROOT, "dist", "JARVIS")
DIST_EXE = os.path.join(DIST_DIR, "JARVIS.exe")


def _app_version():
    spec = importlib.util.spec_from_file_location("jarvis_version_info", os.path.join(ROOT, "installer", "version_info.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.app_version(ROOT)


def _dir_size_mb(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total / (1024 * 1024)


def find_iscc():
    """Path to Inno Setup 6's ISCC.exe, or None."""
    env = os.environ.get("ISCC")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("iscc") or shutil.which("ISCC.exe")
    if found:
        return found
    candidates = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            sub = os.path.join("Programs", "Inno Setup 6") if var == "LOCALAPPDATA" else "Inno Setup 6"
            candidates.append(os.path.join(base, sub, "ISCC.exe"))
    try:
        import winreg
        for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
                          (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    loc = winreg.QueryValueEx(k, "InstallLocation")[0]
                    candidates.append(os.path.join(loc, "ISCC.exe"))
            except OSError:
                pass
    except ImportError:
        pass
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def build_exe(clean=False):
    print("[build] Packaging J.A.R.V.I.S. (one-folder) from jarvis.spec ...")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    cmd.append(SPEC)
    print("[build] Command:", " ".join(cmd))
    t0 = time.perf_counter()
    res = subprocess.run(cmd, cwd=ROOT)
    if res.returncode != 0:
        print(f"\n[build] FAILED with return code {res.returncode}")
        sys.exit(res.returncode)
    print(f"\n[build] SUCCESS in {time.perf_counter() - t0:.0f}s: {DIST_EXE}")
    print(f"[build] Folder size: {_dir_size_mb(DIST_DIR):.0f} MB  (ship the whole dist/JARVIS folder, or the installer)")


def build_installer():
    if not os.path.isfile(DIST_EXE):
        print(f"[installer] {DIST_EXE} not found; build the app first.")
        sys.exit(1)
    iscc = find_iscc()
    if not iscc:
        print("[installer] Inno Setup 6 (ISCC.exe) not found. Install it from https://jrsoftware.org/isinfo.php,")
        print("            or set the ISCC environment variable to the full path of ISCC.exe.")
        sys.exit(2)
    version = _app_version()
    cmd = [iscc, f"/DAppVersion={version}", ISS]
    print("[installer] Command:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=os.path.dirname(ISS))
    if res.returncode != 0:
        print(f"\n[installer] FAILED with return code {res.returncode}")
        sys.exit(res.returncode)
    out = os.path.join(ROOT, "dist", "installer", f"JARVIS-Setup-{version}.exe")
    print(f"\n[installer] SUCCESS: {out}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Build J.A.R.V.I.S. for Windows.")
    p.add_argument("--clean", action="store_true", help="clear PyInstaller's cache before building")
    p.add_argument("--installer", action="store_true", help="also compile the Inno Setup installer after the build")
    p.add_argument("--installer-only", action="store_true", help="only compile the installer from the existing dist/JARVIS")
    args = p.parse_args(argv)
    if not args.installer_only:
        build_exe(clean=args.clean)
    if args.installer or args.installer_only:
        build_installer()


if __name__ == "__main__":
    main()
