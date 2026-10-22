#!/usr/bin/env bash
# Publishes a new Framewise version. One command for everything:
#
#   scripts/release.sh 1.0.3 notes.md
#
#   1. sets the version (app/framewise/__init__.py, README), runs the tests
#   2. builds dist/framewise_<version>_all.deb
#   3. builds the APT repository with it, signed with the Framewise key (gpg
#      asks for its password; nothing has been published yet if that fails)
#   4. commits "Framewise <version>", tags v<version>, pushes main and the tag
#   5. creates the GitHub release with the .deb and the notes
#   6. publishes the APT repository on GitHub Pages (gh-pages branch)
#
# Installed copies then get the update through Software Updater.
set -euo pipefail

VERSION="${1:?usage: scripts/release.sh VERSION NOTES.md}"
NOTES="${2:?usage: scripts/release.sh VERSION NOTES.md}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="F69B99958DF3DE449A33CABF2B8D94D192EADC4F"   # Framewise Archive Signing Key
SITE_URL="https://lnpaulin.github.io/framewise/apt/"
cd "$ROOT"

# ---- checks ----------------------------------------------------------------------------
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version must look like 1.2.3"; exit 1; }
[[ -f "$NOTES" ]] || { echo "No notes file: $NOTES"; exit 1; }
[[ "$(git branch --show-current)" == main ]] || { echo "Release from main"; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo "Commit your changes first"; exit 1; }
git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null && { echo "v$VERSION already exists"; exit 1; }
gpg --list-secret-keys "$KEY" >/dev/null 2>&1 || { echo "The signing key isn't on this computer"; exit 1; }

# ---- version, tests, package --------------------------------------------------------
sed -i -E "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" app/framewise/__init__.py
sed -i -E "s/framewise_[0-9.]+_all\.deb/framewise_${VERSION}_all.deb/g" README.md
(cd app && python3 -m unittest $(ls tests/test_*.py | sed 's#/#.#; s#\.py$##') 2>&1 | tail -1)
scripts/build-deb.sh
DEB="dist/framewise_${VERSION}_all.deb"

# ---- APT repository (GitHub Pages) ------------------------------------------------------
# The site is one commit, replaced each time (the .deb files would otherwise
# pile up in its history); the last few versions are kept.
SITE="$(mktemp -d)"
trap 'rm -rf "$SITE"' EXIT
mkdir -p "$SITE/apt"
if git ls-remote --exit-code --heads origin gh-pages >/dev/null 2>&1; then
    git fetch -q origin gh-pages
    git archive FETCH_HEAD apt | tar -x -C "$SITE"
fi
cp "$DEB" "$SITE/apt/"
if ! python3 scripts/apt_repo.py "$SITE/apt" "$KEY" --keep 3; then
    git checkout -- app/framewise/__init__.py README.md
    echo "Signing failed (password box closed?). Nothing was published; run this again."
    exit 1
fi
cp packaging/apt/framewise-archive-keyring.asc "$SITE/apt/framewise-archive-keyring.asc"
# For the one-line install: the same key and source files the .deb installs.
cp packaging/apt/framewise-archive-keyring.gpg "$SITE/apt/framewise.gpg"
cp packaging/deb/framewise.sources "$SITE/apt/framewise.sources"
cp packaging/site/index.html packaging/site/install.sh "$SITE/"
touch "$SITE/.nojekyll"

# ---- git and GitHub release --------------------------------------------------------
git add app/framewise/__init__.py README.md
git commit -q -m "Framewise $VERSION"
git tag -a "v$VERSION" -m "Framewise $VERSION"
git push -q origin main "v$VERSION"
gh release create "v$VERSION" "$DEB" --title "Framewise $VERSION" --notes-file "$NOTES"

# ---- publish the APT repository -------------------------------------------------------
(
    cd "$SITE"
    git init -q -b gh-pages
    git config user.name "$(git -C "$ROOT" config user.name)"
    git config user.email "$(git -C "$ROOT" config user.email)"
    git add -A
    git commit -q -m "APT repository: Framewise $VERSION"
    git push -q --force "$(git -C "$ROOT" remote get-url origin)" gh-pages
)
echo
echo "Framewise $VERSION is out:"
echo "  https://github.com/LnPaulin/framewise/releases/tag/v$VERSION"
echo "  $SITE_URL (Software Updater picks it up)"
