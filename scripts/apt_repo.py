#!/usr/bin/env python3
"""Builds the Framewise APT repository (a "flat" one) in a folder.

    scripts/apt_repo.py SITE_DIR/apt KEY_ID [--keep N]

The folder holds the .deb files to publish. This writes, next to them:
    Packages, Packages.gz   what's available (from each .deb's control fields)
    Release                 checksums of those lists
    InRelease, Release.gpg  the same, signed with KEY_ID (gpg asks for its password)
and keeps only the newest N versions (default 3).

Computers with Framewise installed read it through
/etc/apt/sources.list.d/framewise.sources ("Suites: ./").
"""

import argparse
import gzip
import hashlib
import subprocess
import sys
from email.utils import formatdate
from pathlib import Path

HASHES = (("MD5Sum", "md5"), ("SHA1", "sha1"), ("SHA256", "sha256"), ("SHA512", "sha512"))


def version_key(deb):
    # framewise_1.0.10_all.deb -> (1, 0, 10)
    version = deb.name.split("_")[1]
    return tuple(int(p) if p.isdigit() else 0 for p in version.replace("-", ".").split("."))


def control(deb):
    out = subprocess.run(["dpkg-deb", "-f", str(deb)], capture_output=True, text=True, check=True).stdout
    return out.rstrip("\n")


def stanza(deb):
    data = deb.read_bytes()
    lines = [control(deb), f"Filename: ./{deb.name}", f"Size: {len(data)}"]
    for field, algo in HASHES:
        lines.append(f"{field}: {hashlib.new(algo, data).hexdigest()}")
    return "\n".join(lines)


def release(folder, files):
    lines = ["Origin: Framewise", "Label: Framewise", "Suite: stable", "Codename: stable",
             f"Date: {formatdate(usegmt=True)}", "Architectures: all amd64 arm64 i386 armhf riscv64",
             "Description: Framewise updates (https://github.com/LnPaulin/framewise)"]
    for field, algo in HASHES:
        lines.append(f"{field}:")
        for name in files:
            data = (folder / name).read_bytes()
            lines.append(f" {hashlib.new(algo, data).hexdigest()} {len(data):>8} {name}")
    return "\n".join(lines) + "\n"


def sign(folder, key):
    for args, out in ((["--clearsign"], "InRelease"), (["--armor", "--detach-sign"], "Release.gpg")):
        target = folder / out
        target.unlink(missing_ok=True)
        subprocess.run(["gpg", "--yes", "--local-user", key, "--digest-algo", "SHA512", *args,
                        "--output", str(target), str(folder / "Release")], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("key")
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args()

    debs = sorted(args.folder.glob("framewise_*_all.deb"), key=version_key)
    if not debs:
        sys.exit(f"No framewise_*_all.deb in {args.folder}")
    for old in debs[:-args.keep]:
        old.unlink()
    debs = debs[-args.keep:]

    packages = "\n\n".join(stanza(d) for d in reversed(debs)) + "\n"
    (args.folder / "Packages").write_text(packages)
    with gzip.GzipFile(args.folder / "Packages.gz", "wb", mtime=0) as f:
        f.write(packages.encode())
    (args.folder / "Release").write_text(release(args.folder, ["Packages", "Packages.gz"]))
    sign(args.folder, args.key)
    print(f"Repository ready in {args.folder}: " + ", ".join(d.name for d in reversed(debs)))


if __name__ == "__main__":
    main()
