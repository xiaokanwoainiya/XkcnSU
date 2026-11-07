/* Inert AVC fixture: one signal-zero check against this process's own parent. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define ROOT_CONTEXT "u:r:ksu:s0"
#define APP_CONTEXT "u:r:untrusted_app:s0:c512,c768"
#define PROBE_COMM "XKCNSUAvcProbe"
#define CONTEXT_SIZE 256
#define TIMEOUT_MS 5000
#define CLEANUP_TIMEOUT_MS 250

struct child_report {
    pid_t pid;
    pid_t parent_pid;
    uid_t uid;
    uid_t euid;
    char context_before[CONTEXT_SIZE];
    char context_after[CONTEXT_SIZE];
    int setup_errno;
    int transition_errno;
    int readback_errno;
    int probe_errno;
    int probe_result;
    bool transition_attempted;
    bool probe_attempted;
    int64_t probe_monotonic_ms;
};

_Static_assert(sizeof(struct child_report) <= 4096, "pipe report must be atomic");

static int64_t monotonic_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        return -1;
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int read_text(const char *path, char *text, size_t capacity)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return errno;
    ssize_t size = read(fd, text, capacity - 1);
    int error = size < 0 ? errno : 0;
    close(fd);
    if (error)
        return error;
    if (size == 0 || (size_t)size == capacity - 1)
        return size == 0 ? ENODATA : EOVERFLOW;
    while (size > 0 && (text[size - 1] == '\n' || text[size - 1] == '\0'))
        size--;
    text[size] = '\0';
    if (size == 0 || memchr(text, '\0', (size_t)size))
        return EINVAL;
    return 0;
}

static bool denial_observed(const struct child_report *report)
{
    return report->probe_attempted && report->probe_result == -1 &&
           (report->probe_errno == EPERM || report->probe_errno == EACCES);
}

static void run_child(int output, int target_fd, pid_t parent_pid, int64_t deadline)
{
    struct child_report report = {
        .pid = getpid(),
        .parent_pid = getppid(),
        .uid = getuid(),
        .euid = geteuid(),
        .probe_result = -1,
    };
    if (report.parent_pid != parent_pid || report.uid != 0 || report.euid != 0) {
        report.setup_errno = EPERM;
        goto done;
    }
    report.setup_errno =
        read_text("/proc/self/attr/current", report.context_before, sizeof(report.context_before));
    if (report.setup_errno)
        goto done;
    if (strcmp(report.context_before, ROOT_CONTEXT) != 0) {
        report.setup_errno = EPERM;
        goto done;
    }
    int64_t now = monotonic_ms();
    if (now < 0 || now >= deadline) {
        report.setup_errno = now < 0 ? EIO : ETIMEDOUT;
        goto done;
    }
    int fd = open("/proc/self/attr/current", O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        report.transition_errno = errno;
        goto done;
    }
    report.transition_attempted = true;
    ssize_t written = write(fd, APP_CONTEXT, sizeof(APP_CONTEXT));
    report.transition_errno =
        written < 0 ? errno : ((size_t)written == sizeof(APP_CONTEXT) ? 0 : EIO);
    close(fd);
    report.readback_errno =
        read_text("/proc/self/attr/current", report.context_after, sizeof(report.context_after));
    if (report.transition_errno || report.readback_errno)
        goto done;
    if (strcmp(report.context_after, APP_CONTEXT) != 0) {
        report.readback_errno = EPROTO;
        goto done;
    }
    report.uid = getuid();
    report.euid = geteuid();
    now = monotonic_ms();
    if (report.uid != 0 || report.euid != 0 || getppid() != parent_pid) {
        report.setup_errno = EPERM;
        goto done;
    }
    if (now < 0 || now >= deadline) {
        report.setup_errno = now < 0 ? EIO : ETIMEDOUT;
        goto done;
    }
    report.probe_monotonic_ms = now;
    report.probe_attempted = true;
    errno = 0;
    /* The inherited pidfd fixes the parent identity even if its PID is reused. */
    report.probe_result = (int)syscall(__NR_pidfd_send_signal, target_fd, 0, NULL, 0);
    report.probe_errno = report.probe_result < 0 ? errno : 0;
done:
    close(target_fd);
    ssize_t sent = write(output, &report, sizeof(report));
    close(output);
    _exit(sent == (ssize_t)sizeof(report) ? 0 : 3);
}

static int receive_report(int fd, struct child_report *report, int64_t deadline)
{
    size_t received = 0;
    while (received < sizeof(*report)) {
        int64_t now = monotonic_ms();
        if (now < 0)
            return EIO;
        if (now >= deadline)
            return ETIMEDOUT;
        struct pollfd event = { .fd = fd, .events = POLLIN };
        int ready = poll(&event, 1, (int)(deadline - now));
        if (ready < 0) {
            if (errno == EINTR)
                continue;
            return errno;
        }
        if (ready == 0)
            return ETIMEDOUT;
        ssize_t size = read(fd, (char *)report + received, sizeof(*report) - received);
        if (size < 0) {
            if (errno == EINTR || errno == EAGAIN)
                continue;
            return errno;
        }
        if (size == 0)
            return EPIPE;
        received += (size_t)size;
    }
    return 0;
}

static int reap_child(pid_t child, int *status, int64_t deadline)
{
    for (;;) {
        pid_t result = waitpid(child, status, WNOHANG);
        if (result == child)
            return 0;
        if (result < 0 && errno != EINTR)
            return errno;
        int64_t now = monotonic_ms();
        if (now < 0)
            return EIO;
        if (now >= deadline)
            return ETIMEDOUT;
        int pause = deadline - now < 10 ? (int)(deadline - now) : 10;
        poll(NULL, 0, pause);
    }
}

static void print_string(const char *text)
{
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
        if (*p == '"' || *p == '\\')
            printf("\\%c", *p);
        else if (*p < 0x20 || *p >= 0x7f)
            printf("\\u%04x", (unsigned int)*p);
        else
            putchar(*p);
    }
    putchar('"');
}

int main(int argc, char **argv)
{
    char nonce[80] = {0};
    char parent_context[CONTEXT_SIZE] = {0};
    char enforcing_text[8] = {0};
    struct child_report report = { .probe_result = -1 };
    struct sigaction action = { .sa_handler = SIG_IGN };
    pid_t parent = getpid(), child = -1;
    int64_t started = monotonic_ms();
    int target_fd = -1, channel[2] = {-1, -1};
    int error = 0, status = 0, enforcing = -1;
    bool complete = false, reaped = false, timed_out = false;

    if (argc == 2 && strcmp(argv[1], "--help") == 0) {
        puts("Usage: avc-spoof-check [--nonce ASCII_TOKEN]");
        return 0;
    }
    if (argc == 3 && strcmp(argv[1], "--nonce") == 0 &&
        strlen(argv[2]) > 0 && strlen(argv[2]) <= 64 &&
        strspn(argv[2], "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-") ==
            strlen(argv[2])) {
        memcpy(nonce, argv[2], strlen(argv[2]) + 1);
    } else if (argc == 1) {
        snprintf(nonce, sizeof(nonce), "%ld-%" PRId64, (long)parent, started);
    } else {
        error = EINVAL;
        goto done;
    }
#if !defined(__ANDROID__) || !defined(__aarch64__)
    error = EOPNOTSUPP;
    goto done;
#endif
    if (started < 0) {
        error = EIO;
        goto done;
    }
    if (getuid() != 0 || geteuid() != 0) {
        error = EPERM;
        goto done;
    }
    error = read_text("/proc/self/attr/current", parent_context, sizeof(parent_context));
    if (error)
        goto done;
    if (strcmp(parent_context, ROOT_CONTEXT) != 0) {
        error = EPERM;
        goto done;
    }
    error = read_text("/sys/fs/selinux/enforce", enforcing_text, sizeof(enforcing_text));
    if (error)
        goto done;
    enforcing = strcmp(enforcing_text, "1") == 0 ? 1 : 0;
    if (enforcing != 1) {
        error = EPERM;
        goto done;
    }
    if (prctl(PR_SET_NAME, PROBE_COMM, 0, 0, 0) != 0 ||
        sigemptyset(&action.sa_mask) != 0 || sigaction(SIGPIPE, &action, NULL) != 0) {
        error = errno;
        goto done;
    }
    action.sa_handler = SIG_DFL;
    if (sigaction(SIGCHLD, &action, NULL) != 0) {
        error = errno;
        goto done;
    }
    target_fd = (int)syscall(__NR_pidfd_open, parent, 0);
    if (target_fd < 0 || pipe2(channel, O_CLOEXEC | O_NONBLOCK) != 0) {
        error = errno;
        goto done;
    }
    child = fork();
    if (child < 0) {
        error = errno;
        goto done;
    }
    if (child == 0) {
        close(channel[0]);
        run_child(channel[1], target_fd, parent, started + TIMEOUT_MS);
    }
    close(channel[1]);
    channel[1] = -1;
    close(target_fd);
    target_fd = -1;
    error = receive_report(channel[0], &report, started + TIMEOUT_MS);
    complete = error == 0;
    if (complete && (report.pid != child || report.parent_pid != parent)) {
        error = EPROTO;
        complete = false;
    }
    int64_t now = monotonic_ms();
    int reap_error = reap_child(child, &status, now < 0 ? 0 : now + CLEANUP_TIMEOUT_MS);
    reaped = reap_error == 0;
    timed_out = error == ETIMEDOUT || reap_error == ETIMEDOUT ||
                report.setup_errno == ETIMEDOUT;
    if (!error && reap_error)
        error = reap_error;
done:
    if (target_fd >= 0)
        close(target_fd);
    if (channel[0] >= 0)
        close(channel[0]);
    if (channel[1] >= 0)
        close(channel[1]);
    printf("{\"schema\":1,\"probe\":\"avc-signull\",\"nonce\":");
    print_string(nonce);
    printf(",\"comm\":\"%s\",\"signal\":0,\"method\":\"pidfd_send_signal\","
           "\"parent_pid\":%ld,\"child_pid\":%ld,\"target_pid\":%ld,"
           "\"parent_uid\":%lu,\"parent_euid\":%lu,\"parent_context\":",
           PROBE_COMM, (long)parent, (long)child, (long)parent,
           (unsigned long)getuid(), (unsigned long)geteuid());
    print_string(parent_context);
    printf(",\"enforcing\":%d,\"started_monotonic_ms\":%" PRId64 ","
           "\"finished_monotonic_ms\":%" PRId64 ",\"timeout_ms\":%d,\"cleanup_timeout_ms\":%d,"
           "\"parent_errno\":%d,\"report_complete\":%s,\"completed\":%s,\"timed_out\":%s,"
           "\"child_reaped\":%s,\"child_exit_code\":%d,\"child_term_signal\":%d,"
           "\"operation_denied\":%s,\"avc_log_verified\":false,\"child\":{"
           "\"pid\":%ld,\"parent_pid\":%ld,\"target_pid\":%ld,\"uid\":%lu,\"euid\":%lu,"
           "\"context_before\":",
           enforcing, started, monotonic_ms(), TIMEOUT_MS, CLEANUP_TIMEOUT_MS, error,
           complete ? "true" : "false",
           complete && report.probe_attempted && reaped && WIFEXITED(status) &&
               WEXITSTATUS(status) == 0 ? "true" : "false",
           timed_out ? "true" : "false", reaped ? "true" : "false",
           reaped && WIFEXITED(status) ? WEXITSTATUS(status) : -1,
           reaped && WIFSIGNALED(status) ? WTERMSIG(status) : 0,
           complete && denial_observed(&report) ? "true" : "false",
           (long)report.pid, (long)report.parent_pid, (long)parent,
           (unsigned long)report.uid, (unsigned long)report.euid);
    print_string(report.context_before);
    printf(",\"context_after\":");
    print_string(report.context_after);
    printf(",\"setup_errno\":%d,\"transition_attempted\":%s,\"transition_errno\":%d,"
           "\"readback_errno\":%d,\"probe_attempted\":%s,\"kill_result\":%d,"
           "\"kill_errno\":%d,\"probe_monotonic_ms\":%" PRId64 "}}\n",
           report.setup_errno, report.transition_attempted ? "true" : "false",
           report.transition_errno, report.readback_errno,
           report.probe_attempted ? "true" : "false", report.probe_result,
           report.probe_errno, report.probe_monotonic_ms);
    if (error || !complete || !reaped || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
        return 2;
    return denial_observed(&report) ? 0 : 1;
}
