"""Builds the release: dist/Truck Dispatcher/ (exe + files) and dist/TruckDispatcher-vX.Y.Z.zip.

python tools/build.py          needs: pip install -r requirements.txt pyinstaller

PyInstaller is run in a temp folder on the system drive (it fails when the source and its temp folders
are on different drives), then the result is copied back into dist/.
"""
import hashlib, os, re, shutil, subprocess, sys, tempfile, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "Truck Dispatcher"
FILES = ["app.py", "dispatcher.py", "planner.py", "motion.py", "gdioverlay.py", "overlay.py", "gamecam.py",
         "locate.py", "gwmem.py"]


def version():
    with open(os.path.join(ROOT, "dispatcher.py"), encoding="utf-8") as f:
        return re.search(r'^VERSION = "([^"]+)"', f.read(), re.M).group(1)


def main():
    ver = version()
    work = tempfile.mkdtemp(prefix="tdbuild-")
    src = os.path.join(work, "src")
    os.makedirs(src)
    for f in FILES:
        shutil.copy2(os.path.join(ROOT, f), src)
    shutil.copytree(os.path.join(ROOT, "savelib"), os.path.join(src, "savelib"))
    shutil.copytree(os.path.join(ROOT, "media"), os.path.join(src, "media"))
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
           "--name", NAME, "--icon", os.path.join(src, "media", "icon.ico"),
           "--add-data", "%s;media" % os.path.join(src, "media", "icon.ico"),
           "--paths", os.path.join(src, "savelib"),
           "--hidden-import", "scipy._cyutility", "--collect-submodules", "scipy.sparse.csgraph",
           "--collect-submodules", "scipy._lib", "--collect-submodules", "scipy._external",
           "--exclude-module", "PIL", "--exclude-module", "matplotlib", "--exclude-module", "pkg_resources",
           "--distpath", os.path.join(work, "dist"), "--workpath", os.path.join(work, "build"),
           "--specpath", work, os.path.join(src, "app.py")]
    print(" ".join(cmd))
    subprocess.check_call(cmd, cwd=src)
    out = os.path.join(ROOT, "dist")
    app = os.path.join(out, NAME)
    if os.path.isdir(app):
        shutil.rmtree(app)
    os.makedirs(out, exist_ok=True)
    shutil.copytree(os.path.join(work, "dist", NAME), app)
    shutil.copy2(os.path.join(ROOT, "docs", "README.txt"), os.path.join(app, "README.txt"))
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(app, "LICENSE.txt"))
    zpath = os.path.join(out, "TruckDispatcher-v%s.zip" % ver)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, _, files in os.walk(app):
            for f in files:
                full = os.path.join(base, f)
                z.write(full, os.path.join(NAME, os.path.relpath(full, app)))
    shutil.rmtree(work, ignore_errors=True)
    h = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
    print("\nBuilt %s (%.1f MB)\nsha256 %s" % (zpath, os.path.getsize(zpath) / 1e6, h))


if __name__ == "__main__":
    main()
