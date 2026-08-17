#!/bin/sh
#
# jobchain-node, implemented in POSIX shell.
#
# A drop-in replacement for the compiled helper, for sites that cannot build
# it or whose compute nodes differ enough from the submit host to make a
# single binary awkward. Select it with:
#
#     export JOBCHAIN_NODE=/path/to/jobchain-node.sh
#
# It implements the same protocol and passes the same conformance tests. Two
# differences are worth knowing about:
#
#   * Claiming costs a process per row examined, because mkdir is not a shell
#     builtin. Late in a large run every chain pays that walk, so this
#     implementation suits runs up to roughly a thousand rows.
#   * There is no fsync. A node crash inside the write window can leave an
#     empty file where the compiled helper would leave the old contents or
#     the new ones.
#
# Correctness is otherwise identical: mkdir is the same system call and
# equally atomic, >> opens with O_APPEND so short appends do not interleave,
# and a temporary file renamed over its target is an atomic replacement.

set -u

JC_VERSION="0.6"

JC_EXIT_OK=0
JC_EXIT_USAGE=1
JC_EXIT_NONE_AVAILABLE=3
JC_EXIT_STATE=6
JC_EXIT_IO=8

# Which scheduler to submit to. jobchain exports JC_SCHEDULER into every
# generated script's environment (see RowContext.preamble), so the
# self-chaining submission at the end of a job always matches the run's
# configured scheduler. JOBCHAIN_SCHEDULER remains as a fallback for the
# helper invoked directly, outside a generated script (a manual `claim`
# or `mark` call from a shell with no JC_SCHEDULER of its own). PBS is the
# ultimate default, matching the tool itself.
JC_SCHEDULER="${JC_SCHEDULER:-${JOBCHAIN_SCHEDULER:-pbs}}"

usage() {
    cat >&2 <<'EOF'
jobchain-node (shell) - compute-node helper for jobchain

Usage:
  jobchain-node claim    --home DIR
  jobchain-node submit   --home DIR --next
  jobchain-node mark     --run DIR --stage NAME [--status WORD]
                         [--jobid ID] [--error MSG]
  jobchain-node emit     --run DIR KEY=VALUE [KEY=VALUE ...]
  jobchain-node event    --home DIR --message TEXT
  jobchain-node selftest --home DIR
  jobchain-node version

Exit codes:
  0  success
  1  usage error
  3  claim found no eligible row (the chain is complete)
  6  state directory missing or inconsistent
  8  filesystem error
EOF
}

error() {
    echo "jobchain-node: $*" >&2
}

# Read the value of a named option from the remaining arguments.
option() {
    wanted="$1"
    shift
    while [ $# -gt 1 ]; do
        if [ "$1" = "$wanted" ]; then
            echo "$2"
            return 0
        fi
        shift
    done
    return 1
}

# Write text to a path atomically: a sibling temporary, then a rename.
#
# chmod pins the file at 0644 regardless of the caller's umask, so a
# permissive umask (e.g. 000 or 002, as on some batch/service accounts)
# cannot leave run state group- or world-writable. This matches the
# compiled helper, which opens with an explicit mode rather than relying
# on whatever the shell's default file-creation mode would be.
write_atomic() {
    path="$1"
    text="$2"
    tmp="$path.tmp.$$"
    printf '%s' "$text" > "$tmp" || return 1
    chmod 644 "$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$path" || { rm -f "$tmp"; return 1; }
    return 0
}

timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

# ---------------------------------------------------------------------------
# mark
# ---------------------------------------------------------------------------

cmd_mark() {
    run_dir=$(option --run "$@") || { error "missing required option --run"
                                      return $JC_EXIT_USAGE; }
    stage=$(option --stage "$@") || { error "missing required option --stage"
                                      return $JC_EXIT_USAGE; }
    status=$(option --status "$@") || status=""
    jobid=$(option --jobid "$@") || jobid=""
    message=$(option --error "$@") || message=""

    if [ -z "$status" ] && [ -z "$jobid" ]; then
        error "missing required option --status"
        return $JC_EXIT_USAGE
    fi
    if [ ! -d "$run_dir" ]; then
        error "run directory does not exist: $run_dir"
        return $JC_EXIT_STATE
    fi

    # A status of nothing records only the job id: by the time a submit
    # command returns, the job may already have written its own status.
    if [ -n "$status" ]; then
        write_atomic "$run_dir/status.$stage" "$status" || return $JC_EXIT_IO
    fi
    [ -n "$jobid" ] && { write_atomic "$run_dir/jobid.$stage" "$jobid" ||
                         return $JC_EXIT_IO; }
    [ -n "$message" ] && { write_atomic "$run_dir/error.$stage" "$message" ||
                           return $JC_EXIT_IO; }

    printf '%s host=%s pid=%s stage=%s status=%s jobid=%s%s\n' \
        "$(timestamp)" "$(hostname 2>/dev/null || echo unknown)" "$$" \
        "$stage" "${status:--}" "${jobid:--}" \
        "${message:+ error=$message}" >> "$run_dir/timeline"
    return $JC_EXIT_OK
}

# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------

cmd_emit() {
    run_dir=$(option --run "$@") || { error "missing required option --run"
                                      return $JC_EXIT_USAGE; }
    if [ ! -d "$run_dir" ]; then
        error "run directory does not exist: $run_dir"
        return $JC_EXIT_STATE
    fi

    pairs=0
    skip=0
    for argument in "$@"; do
        if [ "$skip" = "1" ]; then skip=0; continue; fi
        case "$argument" in
            --*) skip=1; continue ;;
            *=*) ;;
            *) continue ;;
        esac
        key="${argument%%=*}"
        value="${argument#*=}"
        # Single-quote the value, escaping embedded quotes the way the shell
        # requires, so the next stage can source this directly.
        escaped=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
        printf "JC_OUT_%s='%s'\nexport JC_OUT_%s\n" \
            "$key" "$escaped" "$key" >> "$run_dir/handoff" || return $JC_EXIT_IO
        # Pins the mode at 0644 regardless of the caller's umask; see
        # write_atomic for why.
        chmod 644 "$run_dir/handoff" 2>/dev/null
        pairs=$((pairs + 1))
    done

    if [ "$pairs" -eq 0 ]; then
        error "emit needs at least one KEY=VALUE pair"
        return $JC_EXIT_USAGE
    fi
    return $JC_EXIT_OK
}

# ---------------------------------------------------------------------------
# event
# ---------------------------------------------------------------------------

cmd_event() {
    home=$(option --home "$@") || { error "missing required option --home"
                                    return $JC_EXIT_USAGE; }
    message=$(option --message "$@") || { error "missing required option --message"
                                          return $JC_EXIT_USAGE; }
    printf '%s host=%s pid=%s %s\n' "$(timestamp)" \
        "$(hostname 2>/dev/null || echo unknown)" "$$" "$message" \
        >> "$home/events.log" || return $JC_EXIT_IO
    # Pins the mode at 0644 the first time the file is created, regardless
    # of the caller's umask; see write_atomic for why. A no-op chmod on
    # every later append is harmless and cheaper than stat-ing first.
    chmod 644 "$home/events.log" 2>/dev/null
    return $JC_EXIT_OK
}

# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

# Claim the next eligible row. Prints shell assignments for the row it won.
claim_row() {
    home="$1"

    # A stopped run takes no new work. Checking here means a stop reaches
    # every chain at its next advance, without touching any node.
    [ -f "$home/stopped" ] && return $JC_EXIT_NONE_AVAILABLE

    index="$home/rows.idx"
    if [ ! -f "$index" ]; then
        error "could not open row index $index"
        return $JC_EXIT_STATE
    fi

    while IFS= read -r row; do
        [ -z "$row" ] && continue
        row_dir="$home/rows/$row"

        # A held row is being edited on the submit host.
        [ -f "$row_dir/hold" ] && continue
        # A row with no manifest failed validation and has no scripts.
        [ -f "$row_dir/manifest" ] || continue
        [ -f "$row_dir/gen" ] || continue

        generation=$(cat "$row_dir/gen" 2>/dev/null) || continue
        run_dir="$row_dir/run-$generation"

        # The claim itself. mkdir either creates the directory or fails,
        # and the filesystem decides which, so exactly one caller wins.
        # chmod pins the mode at 0755 regardless of the caller's umask (see
        # write_atomic for why), matching the compiled helper's explicit
        # mkdir(path, 0755).
        if mkdir "$run_dir" 2>/dev/null; then
            chmod 755 "$run_dir" 2>/dev/null
            printf 'host=%s pid=%s time=%s\n' \
                "$(hostname 2>/dev/null || echo unknown)" "$$" "$(timestamp)" \
                > "$run_dir/claim"
            chmod 644 "$run_dir/claim" 2>/dev/null
            JC_CLAIMED_ROW="$row"
            JC_CLAIMED_RUN="$run_dir"
            return $JC_EXIT_OK
        fi
    done < "$index"

    return $JC_EXIT_NONE_AVAILABLE
}

cmd_claim() {
    home=$(option --home "$@") || { error "missing required option --home"
                                    return $JC_EXIT_USAGE; }
    claim_row "$home"
    status=$?
    [ "$status" -ne 0 ] && return $status
    echo "JC_NEXT_ROW=$JC_CLAIMED_ROW"
    echo "JC_NEXT_RUN=$JC_CLAIMED_RUN"
    return $JC_EXIT_OK
}

# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

# Submit every stage of a row, threading each job id into the next
# submission's dependency argument. The manifest is three tab-separated
# columns, so no YAML parser and no knowledge of the pipeline is needed.
submit_row() {
    home="$1"
    row="$2"
    run_dir="$3"
    manifest="$home/rows/$row/manifest"

    if [ ! -f "$manifest" ]; then
        error "could not read manifest $manifest"
        return $JC_EXIT_STATE
    fi

    if [ "$JC_SCHEDULER" = "slurm" ]; then
        submit_cmd="sbatch"
        depend_flag="--dependency="
    else
        submit_cmd="qsub"
        depend_flag="-W depend="
    fi

    previous=""
    while IFS="$(printf '\t')" read -r stage depends script; do
        [ -z "$stage" ] && continue
        environment="JC_HOME=$home,JC_ROW=$row,JC_RUN=$run_dir,JC_CHAIN=1"

        # PBS's -v and Slurm's --export=ALL, take their KEY=VALUE list
        # differently: qsub wants it as a separate argv word after -v,
        # while sbatch wants it glued onto --export= as one word. Building
        # both cases explicitly, rather than gluing a flag string onto the
        # value unconditionally, keeps each submission command's argv
        # exactly what that scheduler expects -- a glued "-v KEY=VALUE"
        # passed as one quoted shell word is not the same as -v and
        # KEY=VALUE as two words, and only the latter is what qsub parses.
        if [ "$JC_SCHEDULER" = "slurm" ]; then
            if [ -n "$previous" ] && [ "$depends" != "-" ]; then
                output=$($submit_cmd "$depend_flag$depends:$previous" \
                         "--export=ALL,$environment" "$script" 2>&1)
            else
                output=$($submit_cmd "--export=ALL,$environment" "$script" 2>&1)
            fi
        else
            if [ -n "$previous" ] && [ "$depends" != "-" ]; then
                output=$($submit_cmd "$depend_flag$depends:$previous" \
                         -v "$environment" "$script" 2>&1)
            else
                output=$($submit_cmd -v "$environment" "$script" 2>&1)
            fi
        fi
        if [ $? -ne 0 ]; then
            error "submission of stage $stage failed: $output"
            cmd_mark --run "$run_dir" --stage "$stage" --status FAILED \
                     --error "submission rejected"
            return $JC_EXIT_IO
        fi

        jobid=$(echo "$output" | tail -n 1 | awk '{print $NF}')
        cmd_mark --run "$run_dir" --stage "$stage" --jobid "$jobid"
        previous="$jobid"
    done < "$manifest"
    return $JC_EXIT_OK
}

cmd_submit() {
    home=$(option --home "$@") || { error "missing required option --home"
                                    return $JC_EXIT_USAGE; }
    claim_row "$home"
    status=$?
    [ "$status" -ne 0 ] && return $status
    cmd_event --home "$home" --message "chain advanced"
    submit_row "$home" "$JC_CLAIMED_ROW" "$JC_CLAIMED_RUN"
}

# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

# Verify that this filesystem behaves the way the claim protocol requires.
# Running it once at setup turns an exotic mid-run failure into an immediate
# and understandable one.
cmd_selftest() {
    home=$(option --home "$@") || { error "missing required option --home"
                                    return $JC_EXIT_USAGE; }
    probe="$home/.selftest-$$"
    status=$JC_EXIT_OK

    if mkdir "$probe" 2>/dev/null; then
        echo "mkdir            ok"
    else
        error "mkdir failed in $home"
        return $JC_EXIT_IO
    fi

    if mkdir "$probe" 2>/dev/null; then
        error "a repeated mkdir succeeded; this filesystem cannot be used for
claiming because two jobs could take the same row"
        status=$JC_EXIT_IO
    else
        echo "mkdir exclusion  ok"
    fi

    if write_atomic "$probe/value" "probe" &&
       [ "$(cat "$probe/value")" = "probe" ]; then
        echo "atomic write     ok"
    else
        error "atomic write did not read back correctly under $probe"
        status=$JC_EXIT_IO
    fi

    rm -rf "$probe"
    return $status
}

# ---------------------------------------------------------------------------
# Front end
# ---------------------------------------------------------------------------

[ $# -lt 1 ] && { usage; exit $JC_EXIT_USAGE; }

command="$1"
case "$command" in
    claim)    cmd_claim "$@" ;;
    submit)   cmd_submit "$@" ;;
    mark)     cmd_mark "$@" ;;
    emit)     cmd_emit "$@" ;;
    event)    cmd_event "$@" ;;
    selftest) cmd_selftest "$@" ;;
    version)  echo "jobchain-node $JC_VERSION (shell)"; exit $JC_EXIT_OK ;;
    -h|--help|help) usage; exit $JC_EXIT_OK ;;
    *)        error "unknown command '$command'"; usage; exit $JC_EXIT_USAGE ;;
esac
exit $?
