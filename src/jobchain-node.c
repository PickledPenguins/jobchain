/*
 * jobchain-node - compute-node helper for jobchain.
 *
 * This is the part of jobchain that runs inside a batch job, on compute
 * nodes that are not guaranteed to have a Python interpreter. It
 * deliberately knows nothing about schemas or delimited files: the submit
 * host renders every row's parameters into a plain shell fragment ahead of
 * time, so the helper only ever manipulates directories and small files.
 *
 * Concurrency rests entirely on mkdir(2). Creating a directory is atomic
 * across NFS clients and succeeds for exactly one caller, so claiming a row
 * needs no lock, no timeout, and no stale-lock recovery.
 *
 * The file is organized in four sections, each depending only on those
 * above it:
 *
 *   1. Utilities   bounded paths, atomic write, append, timestamps
 *   2. State       recording a run's status, appending events
 *   3. Claiming    the claim protocol and the filesystem selftest
 *   4. Front end   argument parsing and subcommand dispatch
 *
 * Build with: cc -O2 -std=c99 -Wall -Wextra -Werror -o jobchain-node jobchain-node.c
 */

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/types.h>

#define JC_VERSION "0.6"

/* Longest path the helper will construct. Paths are bounded rather than
 * dynamically grown so that no allocation can fail on the hot path. */
#define JC_PATH_MAX 4096

/* Longest single line the helper reads from a state file. */
#define JC_LINE_MAX 1024

/* Which scheduler to submit to. Read at run time from JC_SCHEDULER,
 * exported by every generated script's preamble to match that run's
 * configured scheduler; JOBCHAIN_SCHEDULER is a fallback for the helper
 * invoked directly, outside a generated script. PBS is the default,
 * matching the tool itself. A single build of this binary serves both
 * schedulers -- there is no compile-time scheduler flag.
 *
 * export_flag carries its own trailing separator so the caller can paste
 * it directly against the KEY=VALUE list with no space in between: PBS's
 * "-v" needs a following space before its argument, while Slurm's
 * "--export=ALL," must be glued straight onto the list with a comma and
 * no space, or the shell splits the KEY=VALUE list into a second,
 * unwanted positional argument. */
static void jc_scheduler_commands(const char **submit_cmd,
                                   const char **export_flag,
                                   const char **depend_flag)
{
    const char *scheduler = getenv("JC_SCHEDULER");
    if (scheduler == NULL || scheduler[0] == '\0') {
        scheduler = getenv("JOBCHAIN_SCHEDULER");
    }
    if (scheduler != NULL && strcmp(scheduler, "slurm") == 0) {
        *submit_cmd = "sbatch";
        *export_flag = "--export=ALL,";
        *depend_flag = "--dependency=";
    } else {
        *submit_cmd = "qsub";
        *export_flag = "-v ";
        *depend_flag = "-W depend=";
    }
}

/* Exit codes. These mirror the Python front end's taxonomy where the two
 * overlap, so a wrapper script can branch on the cause either way. */
#define JC_EXIT_OK 0
#define JC_EXIT_USAGE 1
#define JC_EXIT_INTERNAL 2
#define JC_EXIT_NONE_AVAILABLE 3 /* claim found no eligible row */
#define JC_EXIT_STATE 6          /* run directory missing or inconsistent */
#define JC_EXIT_IO 8             /* an unexpected filesystem error */

/* Forward declarations for the few routines used before they are defined. */
static void jc_error(const char *fmt, ...);
static int jc_path_join(char *dest, size_t dest_size, const char *base,
                        const char *leaf);
static int jc_shell_quote(char *dest, size_t dest_size, const char *value);
static int jc_exists(const char *path);
static int jc_read_line(const char *path, char *buf, size_t buf_size);
static int jc_write_atomic(const char *path, const char *text);
static int jc_append_line(const char *path, const char *text);
static void jc_timestamp(char *buf, size_t buf_size);
static void jc_hostname(char *buf, size_t buf_size);
static int jc_mark(const char *run_dir, const char *stage, const char *status,
                   const char *jobid, const char *error_message);
static int jc_emit(const char *run_dir, char **pairs, int pair_count);
static int jc_submit_next(const char *home);

/* =====================================================================
 * 1. Utilities
 *
 * Every routine here is bounded and allocation-free. Buffers are sized by
 * the caller and truncation is reported rather than silently accepted,
 * because a truncated path would address the wrong file.
 * ===================================================================== */

/* Join base and leaf with a separator, refusing to truncate. */
static int jc_path_join(char *dest, size_t dest_size, const char *base, const char *leaf)
{
    int written;

    if (dest == NULL || base == NULL || leaf == NULL) {
        return -1;
    }
    written = snprintf(dest, dest_size, "%s/%s", base, leaf);
    if (written < 0 || (size_t)written >= dest_size) {
        return -1;
    }
    return 0;
}

/*
 * Single-quote a string for safe inclusion in a shell command line, the way
 * store.py's _shell_quote does on the Python side: wrap in single quotes,
 * and escape any embedded single quote as '\'' (close the quote, emit an
 * escaped literal quote, reopen). This is the only escape POSIX single
 * quoting needs, and it is what makes jc_submit_row's popen() command safe
 * against a value containing shell metacharacters. Truncation is refused
 * rather than silently accepted, matching every other bounded routine here.
 */
static int jc_shell_quote(char *dest, size_t dest_size, const char *value)
{
    size_t out = 0;

    if (dest == NULL || value == NULL || dest_size < 3) {
        return -1;
    }
    dest[out++] = '\'';
    for (; *value != '\0'; value++) {
        if (*value == '\'') {
            if (out + 4 >= dest_size) {
                return -1;
            }
            dest[out++] = '\'';
            dest[out++] = '\\';
            dest[out++] = '\'';
            dest[out++] = '\'';
        } else {
            if (out + 1 >= dest_size) {
                return -1;
            }
            dest[out++] = *value;
        }
    }
    if (out + 2 > dest_size) {
        return -1;
    }
    dest[out++] = '\'';
    dest[out] = '\0';
    return 0;
}

/* Report whether a path currently exists. */
static int jc_exists(const char *path)
{
    struct stat info;
    return stat(path, &info) == 0 ? 1 : 0;
}

/* Read the first line of a file, discarding any trailing newline. */
static int jc_read_line(const char *path, char *buf, size_t buf_size)
{
    FILE *handle;
    size_t length;

    handle = fopen(path, "r");
    if (handle == NULL) {
        return -1;
    }
    if (fgets(buf, (int)buf_size, handle) == NULL) {
        fclose(handle);
        return -1;
    }
    fclose(handle);

    length = strlen(buf);
    while (length > 0 && (buf[length - 1] == '\n' || buf[length - 1] == '\r')) {
        buf[--length] = '\0';
    }
    return 0;
}

/*
 * Write text to path via a sibling temporary and a rename.
 *
 * The temporary carries this process's pid so that two writers to the same
 * target cannot collide on the intermediate file. fsync before the rename
 * ensures the contents are durable before the name becomes visible, which
 * matters when a node dies mid-write.
 */
static int jc_write_atomic(const char *path, const char *text)
{
    char temp_path[JC_PATH_MAX];
    int written;
    int fd;
    size_t length;
    ssize_t result;

    written = snprintf(temp_path, sizeof(temp_path), "%s.tmp.%ld",
                       path, (long)getpid());
    if (written < 0 || (size_t)written >= sizeof(temp_path)) {
        return -1;
    }

    fd = open(temp_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        return -1;
    }

    length = strlen(text);
    while (length > 0) {
        result = write(fd, text, length);
        if (result < 0) {
            if (errno == EINTR) {
                continue; /* a signal interrupted the write; retry it */
            }
            close(fd);
            unlink(temp_path);
            return -1;
        }
        text += result;
        length -= (size_t)result;
    }

    if (fsync(fd) != 0) {
        close(fd);
        unlink(temp_path);
        return -1;
    }
    if (close(fd) != 0) {
        unlink(temp_path);
        return -1;
    }
    if (rename(temp_path, path) != 0) {
        unlink(temp_path);
        return -1;
    }
    return 0;
}

/*
 * Append one line to a file.
 *
 * O_APPEND makes the offset selection and the write a single operation, so
 * short writes from concurrent processes do not interleave. The log is
 * advisory, so a failure here is reported but never aborts a job.
 */
static int jc_append_line(const char *path, const char *text)
{
    int fd;
    size_t length;
    ssize_t result;

    fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) {
        return -1;
    }

    length = strlen(text);
    while (length > 0) {
        result = write(fd, text, length);
        if (result < 0) {
            if (errno == EINTR) {
                continue;
            }
            close(fd);
            return -1;
        }
        text += result;
        length -= (size_t)result;
    }
    if (close(fd) != 0) {
        return -1;
    }
    return 0;
}

/* Format the current local time as YYYY-MM-DD HH:MM:SS. */
static void jc_timestamp(char *buf, size_t buf_size)
{
    time_t now;
    struct tm broken_down;

    now = time(NULL);
    if (localtime_r(&now, &broken_down) == NULL) {
        snprintf(buf, buf_size, "unknown-time");
        return;
    }
    if (strftime(buf, buf_size, "%Y-%m-%d %H:%M:%S", &broken_down) == 0) {
        snprintf(buf, buf_size, "unknown-time");
    }
}

/* Determine this host's name, falling back to a placeholder. */
static void jc_hostname(char *buf, size_t buf_size)
{
    if (gethostname(buf, buf_size) != 0) {
        snprintf(buf, buf_size, "unknown");
        return;
    }
    buf[buf_size - 1] = '\0';
}

/* Print a diagnostic to stderr with a consistent prefix. */
static void jc_error(const char *fmt, ...)
{
    va_list args;

    fputs("jobchain-node: ", stderr);
    va_start(args, fmt);
    vfprintf(stderr, fmt, args);
    va_end(args);
    fputc('\n', stderr);
}

/* =====================================================================
 * 2. State
 *
 * A run directory holds one small file per attribute rather than a single
 * structured document. That keeps each update independent, so a partial
 * write can never corrupt an unrelated attribute, and it makes the state
 * legible with cat and ls when something needs diagnosing on a node.
 * ===================================================================== */

/*
 * Record a status word for a run.
 *
 * The status file is replaced atomically so a reader always sees either the
 * previous status or the new one, never a partial word. The timeline append
 * that follows is advisory: losing a timeline entry degrades diagnostics but
 * not correctness, so it warns instead of failing the job.
 */
/*
 * Record a status for one stage of a run.
 *
 * Each attribute is its own small file, named for the stage, so a partial
 * write can never corrupt an unrelated stage and the state stays legible
 * with cat and ls on a node. Each file is replaced atomically, so a reader
 * always sees either the previous value or the new one.
 */
static int jc_mark(const char *run_dir, const char *stage, const char *status,
                   const char *jobid, const char *error_message)
{
    char path[JC_PATH_MAX];
    char leaf[JC_LINE_MAX];
    char line[JC_LINE_MAX];
    char stamp[64];
    char host[256];
    int written;

    if (!jc_exists(run_dir)) {
        jc_error("run directory does not exist: %s", run_dir);
        return JC_EXIT_STATE;
    }

    /* A status of NULL records only the job id. A submitter uses that: by
     * the time qsub returns, the job may already be running and may have
     * written its own status, which must not be overwritten. */
    if (status != NULL) {
        written = snprintf(leaf, sizeof(leaf), "status.%s", stage);
        if (written < 0 || (size_t)written >= sizeof(leaf)) {
            return JC_EXIT_IO;
        }
        if (jc_path_join(path, sizeof(path), run_dir, leaf) != 0) {
            jc_error("path too long for run directory: %s", run_dir);
            return JC_EXIT_IO;
        }
        if (jc_write_atomic(path, status) != 0) {
            jc_error("could not write status to %s", path);
            return JC_EXIT_IO;
        }
    }

    if (jobid != NULL && jobid[0] != '\0') {
        written = snprintf(leaf, sizeof(leaf), "jobid.%s", stage);
        if (written < 0 || (size_t)written >= sizeof(leaf)) {
            return JC_EXIT_IO;
        }
        if (jc_path_join(path, sizeof(path), run_dir, leaf) != 0 ||
            jc_write_atomic(path, jobid) != 0) {
            jc_error("could not write jobid for stage %s", stage);
            return JC_EXIT_IO;
        }
    }

    if (error_message != NULL && error_message[0] != '\0') {
        written = snprintf(leaf, sizeof(leaf), "error.%s", stage);
        if (written < 0 || (size_t)written >= sizeof(leaf)) {
            return JC_EXIT_IO;
        }
        if (jc_path_join(path, sizeof(path), run_dir, leaf) != 0 ||
            jc_write_atomic(path, error_message) != 0) {
            jc_error("could not write error for stage %s", stage);
            return JC_EXIT_IO;
        }
    }

    jc_timestamp(stamp, sizeof(stamp));
    jc_hostname(host, sizeof(host));
    snprintf(line, sizeof(line),
             "%s host=%s pid=%ld stage=%s status=%s jobid=%s%s%s\n",
             stamp, host, (long)getpid(), stage,
             status != NULL ? status : "-",
             (jobid != NULL && jobid[0] != '\0') ? jobid : "-",
             (error_message != NULL && error_message[0] != '\0') ? " error=" : "",
             (error_message != NULL && error_message[0] != '\0') ? error_message : "");

    if (jc_path_join(path, sizeof(path), run_dir, "timeline") == 0) {
        if (jc_append_line(path, line) != 0) {
            jc_error("warning: could not append to timeline %s", path);
        }
    }
    return JC_EXIT_OK;
}

/*
 * Publish key=value pairs for later stages of the same row.
 *
 * Handoff is written as a shell fragment so the next stage can source it
 * directly, with no parsing and no interpreter. Values are single-quoted,
 * with embedded quotes escaped the way the shell requires.
 */
static int jc_emit(const char *run_dir, char **pairs, int pair_count)
{
    char path[JC_PATH_MAX];
    char line[JC_LINE_MAX];
    size_t used;
    int i;
    const char *cursor;
    const char *equals;

    if (!jc_exists(run_dir)) {
        jc_error("run directory does not exist: %s", run_dir);
        return JC_EXIT_STATE;
    }
    if (jc_path_join(path, sizeof(path), run_dir, "handoff") != 0) {
        return JC_EXIT_IO;
    }

    for (i = 0; i < pair_count; i++) {
        equals = strchr(pairs[i], '=');
        if (equals == NULL) {
            jc_error("emit expects KEY=VALUE, got '%s'", pairs[i]);
            return JC_EXIT_USAGE;
        }

        used = 0;
        used += (size_t)snprintf(line, sizeof(line), "JC_OUT_");
        for (cursor = pairs[i]; cursor < equals && used + 4 < sizeof(line); cursor++) {
            line[used++] = *cursor;
        }
        line[used++] = '=';
        line[used++] = '\'';
        for (cursor = equals + 1; *cursor != '\0' && used + 8 < sizeof(line); cursor++) {
            if (*cursor == '\'') {
                line[used++] = '\'';
                line[used++] = '\\';
                line[used++] = '\'';
                line[used++] = '\'';
            } else {
                line[used++] = *cursor;
            }
        }
        line[used++] = '\'';
        line[used++] = '\n';
        line[used] = '\0';
        if (jc_append_line(path, line) != 0) {
            jc_error("could not append to handoff %s", path);
            return JC_EXIT_IO;
        }

        used = 0;
        used += (size_t)snprintf(line, sizeof(line), "export JC_OUT_");
        for (cursor = pairs[i]; cursor < equals && used + 3 < sizeof(line); cursor++) {
            line[used++] = *cursor;
        }
        line[used++] = '\n';
        line[used] = '\0';
        if (jc_append_line(path, line) != 0) {
            return JC_EXIT_IO;
        }
    }
    return JC_EXIT_OK;
}

/*
 * Append a free-form message to the global event log.
 *
 * Every process writing here uses O_APPEND with a single short write, so
 * entries from concurrent jobs remain whole and in arrival order.
 */
static int jc_event(const char *home, const char *message)
{
    char path[JC_PATH_MAX];
    char line[JC_LINE_MAX];
    char stamp[64];
    char host[256];

    if (jc_path_join(path, sizeof(path), home, "events.log") != 0) {
        jc_error("path too long for home directory: %s", home);
        return JC_EXIT_IO;
    }

    jc_timestamp(stamp, sizeof(stamp));
    jc_hostname(host, sizeof(host));
    snprintf(line, sizeof(line), "%s host=%s pid=%ld %s\n",
             stamp, host, (long)getpid(), message);

    if (jc_append_line(path, line) != 0) {
        jc_error("could not append to event log %s", path);
        return JC_EXIT_IO;
    }
    return JC_EXIT_OK;
}

/* =====================================================================
 * 3. Claiming
 * ===================================================================== */

/*
 * Attempt to claim one specific row.
 *
 * Returns JC_EXIT_OK when this process won the row, JC_EXIT_NONE_AVAILABLE
 * when the row is not claimable (already taken, held, or malformed), and
 * JC_EXIT_IO for an unexpected filesystem error.
 */
static int try_claim_row(const char *rows_dir, const char *row_name,
                         char *run_out, size_t run_out_size)
{
    char row_dir[JC_PATH_MAX];
    char path[JC_PATH_MAX];
    char generation[JC_LINE_MAX];
    char claim_line[JC_LINE_MAX];
    char stamp[64];
    char host[256];
    int written;

    if (jc_path_join(row_dir, sizeof(row_dir), rows_dir, row_name) != 0) {
        return JC_EXIT_IO;
    }

    /* A held row is being edited on the submit host; skip it entirely. */
    if (jc_path_join(path, sizeof(path), row_dir, "hold") != 0) {
        return JC_EXIT_IO;
    }
    if (jc_exists(path)) {
        return JC_EXIT_NONE_AVAILABLE;
    }

    /* A row with no manifest failed validation and has no scripts, so it is
     * not claimable until it is corrected. */
    if (jc_path_join(path, sizeof(path), row_dir, "manifest") != 0) {
        return JC_EXIT_IO;
    }
    if (!jc_exists(path)) {
        return JC_EXIT_NONE_AVAILABLE;
    }

    if (jc_path_join(path, sizeof(path), row_dir, "gen") != 0) {
        return JC_EXIT_IO;
    }
    if (jc_read_line(path, generation, sizeof(generation)) != 0) {
        /* No generation file means this is not a usable row directory.
         * Skipping is right: the submit host owns row creation. */
        return JC_EXIT_NONE_AVAILABLE;
    }

    written = snprintf(path, sizeof(path), "%s/run-%s", row_dir, generation);
    if (written < 0 || (size_t)written >= sizeof(path)) {
        return JC_EXIT_IO;
    }

    /* The claim itself. Everything before this point is advisory; this
     * single call is what decides ownership. */
    if (mkdir(path, 0755) != 0) {
        if (errno == EEXIST) {
            return JC_EXIT_NONE_AVAILABLE;
        }
        jc_error("could not create run directory %s: %s", path, strerror(errno));
        return JC_EXIT_IO;
    }

    if (snprintf(run_out, run_out_size, "%s", path) < 0) {
        return JC_EXIT_IO;
    }

    /* Record who took it, for diagnosis only. The mkdir above is the sole
     * source of truth about ownership. */
    jc_timestamp(stamp, sizeof(stamp));
    jc_hostname(host, sizeof(host));
    snprintf(claim_line, sizeof(claim_line), "host=%s pid=%ld time=%s\n",
             host, (long)getpid(), stamp);
    if (jc_path_join(path, sizeof(path), run_out, "claim") == 0) {
        if (jc_write_atomic(path, claim_line) != 0) {
            jc_error("warning: could not record claim metadata in %s", path);
        }
    }

    return JC_EXIT_OK;
}

/*
 * Claim the next eligible row listed in the run's row index.
 *
 * Rows are visited in index order, which is the order they appeared in the
 * parameter file. The index is read line by line rather than by scanning the
 * directory, so ordering does not depend on readdir and the cost of a claim
 * stays proportional to the number of rows examined, not to the total.
 */
static int jc_claim(const char *home, char *row_out, size_t row_out_size,
             char *run_out, size_t run_out_size)
{
    char index_path[JC_PATH_MAX];
    char rows_dir[JC_PATH_MAX];
    char row_name[JC_LINE_MAX];
    FILE *index;
    size_t length;
    int outcome;

    if (jc_path_join(index_path, sizeof(index_path), home, "rows.idx") != 0 ||
        jc_path_join(rows_dir, sizeof(rows_dir), home, "rows") != 0) {
        jc_error("path too long for home directory: %s", home);
        return JC_EXIT_IO;
    }

    /* A stopped run takes no new work. Checking here means a stop reaches
     * every chain at its next advance, without having to touch any node. */
    if (jc_path_join(row_name, sizeof(row_name), home, "stopped") == 0 &&
        jc_exists(row_name)) {
        return JC_EXIT_NONE_AVAILABLE;
    }

    index = fopen(index_path, "r");
    if (index == NULL) {
        jc_error("could not open row index %s: %s", index_path, strerror(errno));
        return JC_EXIT_STATE;
    }

    while (fgets(row_name, sizeof(row_name), index) != NULL) {
        length = strlen(row_name);
        while (length > 0 && (row_name[length - 1] == '\n' || row_name[length - 1] == '\r')) {
            row_name[--length] = '\0';
        }
        if (length == 0) {
            continue;
        }

        outcome = try_claim_row(rows_dir, row_name, run_out, run_out_size);
        if (outcome == JC_EXIT_OK) {
            fclose(index);
            if (snprintf(row_out, row_out_size, "%s", row_name) < 0) {
                return JC_EXIT_IO;
            }
            return JC_EXIT_OK;
        }
        if (outcome == JC_EXIT_IO) {
            fclose(index);
            return JC_EXIT_IO;
        }
        /* Otherwise the row was not claimable; continue to the next one. */
    }

    fclose(index);
    return JC_EXIT_NONE_AVAILABLE;
}

/*
 * Verify that this filesystem behaves the way the claim protocol requires.
 *
 * The check is deliberately concrete: create a directory, confirm a second
 * creation fails with EEXIST, confirm an atomic write survives a read back,
 * then clean up. Running this once at setup turns an exotic mid-run failure
 * into an immediate, understandable one.
 */
static int jc_selftest(const char *home)
{
    char probe[JC_PATH_MAX];
    char file[JC_PATH_MAX];
    char readback[JC_LINE_MAX];
    int written;
    int status = JC_EXIT_OK;

    written = snprintf(probe, sizeof(probe), "%s/.selftest-%ld", home, (long)getpid());
    if (written < 0 || (size_t)written >= sizeof(probe)) {
        jc_error("path too long for home directory: %s", home);
        return JC_EXIT_IO;
    }

    if (mkdir(probe, 0755) != 0) {
        jc_error("mkdir failed in %s: %s", home, strerror(errno));
        return JC_EXIT_IO;
    }
    printf("mkdir            ok\n");

    if (mkdir(probe, 0755) == 0) {
        jc_error("a repeated mkdir succeeded; this filesystem cannot be used "
                 "for claiming because two jobs could take the same row");
        status = JC_EXIT_IO;
    } else if (errno != EEXIST) {
        jc_error("repeated mkdir failed with %s, expected EEXIST", strerror(errno));
        status = JC_EXIT_IO;
    } else {
        printf("mkdir exclusion  ok\n");
    }

    if (jc_path_join(file, sizeof(file), probe, "value") != 0 ||
        jc_write_atomic(file, "probe") != 0) {
        jc_error("atomic write failed under %s", probe);
        status = JC_EXIT_IO;
    } else if (jc_read_line(file, readback, sizeof(readback)) != 0 ||
               strcmp(readback, "probe") != 0) {
        jc_error("atomic write did not read back correctly under %s", probe);
        status = JC_EXIT_IO;
    } else {
        printf("atomic write     ok\n");
    }

    unlink(file);
    if (rmdir(probe) != 0) {
        jc_error("warning: could not remove probe directory %s", probe);
    }
    return status;
}

/*
 * Submit every stage of a row, threading each job id into the next
 * submission's dependency argument.
 *
 * The manifest is three tab-separated columns, so no YAML parser and no
 * knowledge of the pipeline is needed here. Submission itself is delegated
 * to a small shell fragment because capturing a command's output requires a
 * pipe, and popen is the least code that does it correctly.
 */
static int jc_submit_row(const char *home, const char *row, const char *run_dir)
{
    char manifest_path[JC_PATH_MAX];
    char command[JC_PATH_MAX * 3];
    char previous[JC_LINE_MAX];
    char jobid[JC_LINE_MAX];
    char line[JC_LINE_MAX];
    char q_home[JC_PATH_MAX * 2];
    char q_row[JC_PATH_MAX * 2];
    char q_run_dir[JC_PATH_MAX * 2];
    char q_script[JC_PATH_MAX * 2];
    char *stage;
    char *depends;
    char *script;
    char *cursor;
    FILE *manifest;
    FILE *pipe_handle;
    size_t length;
    int written;
    int submitted = 0;
    const char *submit_cmd;
    const char *export_flag;
    const char *depend_flag;

    jc_scheduler_commands(&submit_cmd, &export_flag, &depend_flag);

    /* home, row, and run_dir reach here from the run's own state directory
     * names, but run_dir's ancestry traces back to a schema-validated path
     * template that a row column can influence -- quote every value that
     * crosses into the submission command line, not just the ones that look
     * risky today. */
    if (jc_shell_quote(q_home, sizeof(q_home), home) != 0 ||
        jc_shell_quote(q_row, sizeof(q_row), row) != 0 ||
        jc_shell_quote(q_run_dir, sizeof(q_run_dir), run_dir) != 0) {
        jc_error("home, row, or run directory too long to quote safely");
        return JC_EXIT_IO;
    }

    snprintf(manifest_path, sizeof(manifest_path), "%s/rows/%s/manifest", home, row);

    manifest = fopen(manifest_path, "r");
    if (manifest == NULL) {
        jc_error("could not read manifest %s", manifest_path);
        return JC_EXIT_STATE;
    }

    previous[0] = '\0';
    while (fgets(line, sizeof(line), manifest) != NULL) {
        length = strlen(line);
        while (length > 0 && (line[length - 1] == '\n' || line[length - 1] == '\r')) {
            line[--length] = '\0';
        }
        if (length == 0) {
            continue;
        }

        stage = line;
        depends = strchr(line, '\t');
        if (depends == NULL) {
            continue;
        }
        *depends++ = '\0';
        script = strchr(depends, '\t');
        if (script == NULL) {
            continue;
        }
        *script++ = '\0';

        if (jc_shell_quote(q_script, sizeof(q_script), script) != 0) {
            jc_error("script path too long to quote safely for stage %s", stage);
            fclose(manifest);
            return JC_EXIT_IO;
        }

        if (previous[0] != '\0' && strcmp(depends, "-") != 0) {
            written = snprintf(command, sizeof(command),
                     "%s %s%s:%s %sJC_HOME=%s,JC_ROW=%s,JC_RUN=%s,JC_CHAIN=1 %s 2>&1",
                     submit_cmd, depend_flag, depends, previous,
                     export_flag, q_home, q_row, q_run_dir, q_script);
        } else {
            written = snprintf(command, sizeof(command),
                     "%s %sJC_HOME=%s,JC_ROW=%s,JC_RUN=%s,JC_CHAIN=1 %s 2>&1",
                     submit_cmd, export_flag, q_home, q_row, q_run_dir, q_script);
        }
        if (written < 0 || (size_t)written >= sizeof(command)) {
            jc_error("submission command too long for stage %s", stage);
            fclose(manifest);
            return JC_EXIT_IO;
        }

        pipe_handle = popen(command, "r");
        if (pipe_handle == NULL) {
            jc_error("could not run %s", submit_cmd);
            fclose(manifest);
            return JC_EXIT_IO;
        }
        jobid[0] = '\0';
        while (fgets(jobid, sizeof(jobid), pipe_handle) != NULL) {
            /* keep the last line: site prologues sometimes print banners */
        }
        if (pclose(pipe_handle) != 0) {
            jc_error("submission of stage %s failed: %s", stage, jobid);
            jc_mark(run_dir, stage, "FAILED", NULL, "submission rejected");
            fclose(manifest);
            return JC_EXIT_IO;
        }

        length = strlen(jobid);
        while (length > 0 && (jobid[length - 1] == '\n' || jobid[length - 1] == '\r')) {
            jobid[--length] = '\0';
        }
        cursor = strrchr(jobid, ' ');
        if (cursor != NULL) {
            memmove(jobid, cursor + 1, strlen(cursor + 1) + 1);
        }

        jc_mark(run_dir, stage, NULL, jobid, NULL);
        snprintf(previous, sizeof(previous), "%s", jobid);
        submitted++;
    }
    fclose(manifest);

    if (submitted == 0) {
        jc_error("manifest for row %s lists no stages", row);
        return JC_EXIT_STATE;
    }
    return JC_EXIT_OK;
}

/* Claim the next row and submit its whole pipeline. */
static int jc_submit_next(const char *home)
{
    char row[JC_LINE_MAX];
    char run_dir[JC_PATH_MAX];
    int status;

    status = jc_claim(home, row, sizeof(row), run_dir, sizeof(run_dir));
    if (status != JC_EXIT_OK) {
        return status;
    }
    jc_event(home, "chain advanced");
    return jc_submit_row(home, row, run_dir);
}

/* =====================================================================
 * 4. Front end
 *
 * Output is designed to be consumed by a shell rather than read by a
 * person: claim prints assignments that the caller evaluates directly.
 *
 *     eval "$(jobchain-node claim --home "$JC_HOME")"
 * ===================================================================== */

static void usage(FILE *out)
{
    fprintf(out,
        "jobchain-node " JC_VERSION " - compute-node helper for jobchain\n"
        "\n"
        "Usage:\n"
        "  jobchain-node claim    --home DIR\n"
        "  jobchain-node submit   --home DIR --next\n"
        "  jobchain-node mark     --run DIR --stage NAME --status WORD\n"
        "                         [--jobid ID] [--error MSG]\n"
        "  jobchain-node emit     --run DIR KEY=VALUE [KEY=VALUE ...]\n"
        "  jobchain-node event    --home DIR --message TEXT\n"
        "  jobchain-node selftest --home DIR\n"
        "  jobchain-node version\n"
        "\n"
        "claim prints shell assignments for the row it won:\n"
        "  JC_NEXT_ROW=000123\n"
        "  JC_NEXT_RUN=/path/.jobchain/rows/000123/run-1\n"
        "\n"
        "Exit codes:\n"
        "  0  success\n"
        "  1  usage error\n"
        "  3  claim found no eligible row (the chain is complete)\n"
        "  6  state directory missing or inconsistent\n"
        "  8  filesystem error\n");
}

/*
 * Look up the value of a named option.
 *
 * Returns the value, or NULL when the option is absent. An option present
 * without a value is treated as absent by the callers, which then report a
 * usage error naming the missing argument.
 */
static const char *option(int argc, char **argv, const char *name)
{
    int i;

    for (i = 2; i < argc - 1; i++) {
        if (strcmp(argv[i], name) == 0) {
            return argv[i + 1];
        }
    }
    return NULL;
}

/* Report a missing required option and return the usage exit code. */
static int missing(const char *name)
{
    jc_error("missing required option %s", name);
    return JC_EXIT_USAGE;
}

static int cmd_claim(int argc, char **argv)
{
    const char *home;
    char row[JC_LINE_MAX];
    char run[JC_PATH_MAX];
    int status;

    home = option(argc, argv, "--home");
    if (home == NULL) {
        return missing("--home");
    }

    status = jc_claim(home, row, sizeof(row), run, sizeof(run));
    if (status != JC_EXIT_OK) {
        return status;
    }

    /* Printed as shell assignments so the caller can eval the output
     * directly instead of parsing it. */
    printf("JC_NEXT_ROW=%s\n", row);
    printf("JC_NEXT_RUN=%s\n", run);
    return JC_EXIT_OK;
}

static int cmd_mark(int argc, char **argv)
{
    const char *run_dir;
    const char *stage;
    const char *status;

    run_dir = option(argc, argv, "--run");
    if (run_dir == NULL) {
        return missing("--run");
    }
    stage = option(argc, argv, "--stage");
    if (stage == NULL) {
        return missing("--stage");
    }
    status = option(argc, argv, "--status");
    if (status == NULL && option(argc, argv, "--jobid") == NULL) {
        return missing("--status");
    }
    return jc_mark(run_dir, stage, status,
                   option(argc, argv, "--jobid"),
                   option(argc, argv, "--error"));
}

static int cmd_emit(int argc, char **argv)
{
    const char *run_dir;
    int i;
    int first = -1;
    int count = 0;

    run_dir = option(argc, argv, "--run");
    if (run_dir == NULL) {
        return missing("--run");
    }
    /* Everything that is not an option or its value is a key=value pair. */
    for (i = 2; i < argc; i++) {
        if (strncmp(argv[i], "--", 2) == 0) {
            i++;
            continue;
        }
        if (first < 0) {
            first = i;
        }
        count++;
    }
    if (count == 0) {
        jc_error("emit needs at least one KEY=VALUE pair");
        return JC_EXIT_USAGE;
    }
    return jc_emit(run_dir, &argv[first], count);
}

static int cmd_submit(int argc, char **argv)
{
    const char *home;

    home = option(argc, argv, "--home");
    if (home == NULL) {
        return missing("--home");
    }
    return jc_submit_next(home);
}

static int cmd_event(int argc, char **argv)
{
    const char *home;
    const char *message;

    home = option(argc, argv, "--home");
    if (home == NULL) {
        return missing("--home");
    }
    message = option(argc, argv, "--message");
    if (message == NULL) {
        return missing("--message");
    }
    return jc_event(home, message);
}

static int cmd_selftest(int argc, char **argv)
{
    const char *home;

    home = option(argc, argv, "--home");
    if (home == NULL) {
        return missing("--home");
    }
    return jc_selftest(home);
}

int main(int argc, char **argv)
{
    const char *command;

    if (argc < 2) {
        usage(stderr);
        return JC_EXIT_USAGE;
    }
    command = argv[1];

    if (strcmp(command, "claim") == 0) {
        return cmd_claim(argc, argv);
    }
    if (strcmp(command, "mark") == 0) {
        return cmd_mark(argc, argv);
    }
    if (strcmp(command, "emit") == 0) {
        return cmd_emit(argc, argv);
    }
    if (strcmp(command, "submit") == 0) {
        return cmd_submit(argc, argv);
    }
    if (strcmp(command, "event") == 0) {
        return cmd_event(argc, argv);
    }
    if (strcmp(command, "selftest") == 0) {
        return cmd_selftest(argc, argv);
    }
    if (strcmp(command, "version") == 0) {
        printf("jobchain-node %s\n", JC_VERSION);
        return JC_EXIT_OK;
    }
    if (strcmp(command, "-h") == 0 || strcmp(command, "--help") == 0 ||
        strcmp(command, "help") == 0) {
        usage(stdout);
        return JC_EXIT_OK;
    }

    jc_error("unknown command '%s'", command);
    usage(stderr);
    return JC_EXIT_USAGE;
}
