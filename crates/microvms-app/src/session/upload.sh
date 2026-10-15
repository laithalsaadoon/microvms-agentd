# SPDX-License-Identifier: Apache-2.0
# Invoked with positional arguments, never interpolated shell source.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset ENV BASH_ENV CDPATH

refuse_links() {
    path=$1
    while [ "$path" != / ] && [ "$path" != . ]; do
        if [ -L "$path" ]; then
            echo "refusing upload through a symbolic link: $path" >&2
            exit 1
        fi
        case "$path" in */*) path=${path%/*} ;; *) path=. ;; esac
        [ -n "$path" ] || path=/
    done
}

refuse_tree_links() {
    # Source symlinks replace destination names instead of traversing them.
    [ ! -L "$1" ] || return 0
    refuse_links "$2"
    if [ -d "$1" ]; then
        # Positional arguments belong to each call. The loop variable is consumed
        # before recursion and overwritten by the next iteration, never read after
        # returning. This walks wide trees without starting a process per entry.
        for entry in "$1"/* "$1"/.[!.]* "$1"/..?*; do
            [ -e "$entry" ] || [ -L "$entry" ] || continue
            refuse_tree_links "$entry" "$2/${entry##*/}"
        done
    fi
}

case "$1" in
    prepare)
        refuse_links "$2"
        refuse_links /tmp
        # The stage name must stay root's until handoff. A root-owned sticky
        # parent, or a parent writable only by root, prevents replacement.
        [ "$(stat -c %u /tmp)" = 0 ]
        permissions=$(stat -c %a /tmp)
        permissions=$((0$permissions))
        [ "$((permissions & 0022))" = 0 ] || [ "$((permissions & 01000))" != 0 ]
        # This is the one shared sync root. Its parent is root's directory, so a
        # non-root workload cannot swap its name during this handoff. Never
        # recursively chown a destination that a workload can already edit.
        case "$2" in /workspace|/workspace/*)
            if [ ! -e /workspace ]; then mkdir /workspace; fi
            [ -d /workspace ]
            chown -h "$3:$4" /workspace
        esac
        umask 077
        mktemp -d /tmp/.microvms-upload-XXXXXXXXXX
        ;;
    handoff)
        # The enclosing directory remains root-owned and mode 0700 until every
        # child is done. A workload cannot plant a link or swap a directory in it.
        chown -hR "$3:$4" "$2/payload"
        chown -h "$3:$4" "$2"
        ;;
    install-file)
        refuse_links "$3"
        parent=${3%/*}
        mkdir -p -- "${parent:-/}"
        # Move replaces the name itself; it does not follow a final symlink.
        mv -fT -- "$2/payload" "$3"
        rm -rf -- "$2"
        ;;
    install-tar)
        refuse_links "$3"
        # Refuse existing symlinks at directories and regular-file leaves before
        # copying anything. Source symlinks replace their destination names.
        refuse_tree_links "$2/payload" "$3"
        mkdir -p -- "$3"
        # Unlink file leaves before copying, so a symlink planted after the
        # check cannot redirect the write. Traversal runs as the default user.
        # Do not preserve metadata onto existing directories: /tmp, for example,
        # is writable without being owned by this user. Newly created entries
        # still get the staged mode, without a second umask restriction.
        umask 000
        cp -dR --remove-destination -- "$2/payload/." "$3/"
        rm -rf -- "$2"
        ;;
    cleanup)
        rm -rf -- "$2"
        ;;
    cleanup-private)
        # After handoff the stage is workload-controlled. Root must not recurse
        # there: a workload could move in a root-owned directory it cannot read.
        # Root ownership of this 0700 entry under sticky /tmp also means the
        # workload cannot replace its name between this check and removal.
        [ "$(stat -c %u -- "$2")" = 0 ]
        [ "$(stat -c %a -- "$2")" = 700 ]
        rm -rf -- "$2"
        ;;
    *) echo 'unknown upload operation' >&2; exit 1 ;;
esac
