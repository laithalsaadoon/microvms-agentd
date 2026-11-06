#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# seed.sh SEED PROJECT ORIGIN: replace PROJECT with a fresh git repo holding one seed commit
# (every file in the SEED directory: the code with its bug, the frozen tests and check.py, plus
# a README when SEED has none, and an ignored .env), and ORIGIN with a bare repo whose main is
# that commit. driver.py runs this outside the box before each phase. GIT_CONFIG_GLOBAL points
# at the tools' git config (identity, hooks off).
set -eu
SEED=$1 PROJECT=$2 ORIGIN=$3
rm -rf "$PROJECT" "$ORIGIN"
mkdir -p "$PROJECT"
cp -R "$SEED/." "$PROJECT/"
[ -f "$PROJECT/README.md" ] || printf '# calc\n\nA tiny calculator project.\n' > "$PROJECT/README.md"
printf '.env\n__pycache__/\n' > "$PROJECT/.gitignore"
printf 'DB_PASSWORD=not-a-real-secret-0123\n' > "$PROJECT/.env"
chmod 0600 "$PROJECT/.env"
git init -q -b main "$PROJECT"
git -C "$PROJECT" config core.hooksPath /dev/null
git -C "$PROJECT" add -A
git -C "$PROJECT" commit -q -m "seed: $(basename "$SEED")"
git init -q --bare -b main "$ORIGIN"
# origin is reached through the ext transport, which execs `git receive-pack <path>` with no
# shell. A plain path would make git run receive-pack through /bin/sh, which a Box tool's
# sandbox doesn't have. The agent can't change this URL: .git is git's alone, and `git config`
# and `git remote` have no permit.
git -C "$PROJECT" remote add origin "ext::git %s $ORIGIN"
git -C "$PROJECT" push -q -u origin main
