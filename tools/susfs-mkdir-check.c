/* Regression check for native mkdir semantics while the SUSFS guard is loaded. */
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static int expect_existing(int fd, const char *path)
{
    errno = 0;
    int result = mkdirat(fd, path, 0700);
    int saved = errno;
    printf("{\"path\":\"%s\",\"result\":%d,\"errno\":%d,\"passed\":%s}\n",
           path, result, saved,
           result == -1 && saved == EEXIST ? "true" : "false");
    return result == -1 && saved == EEXIST ? 0 : 1;
}

int main(int argc, char **argv)
{
    if (argc != 2 || getuid() != 0)
        return 2;

    int failures = 0;
    for (const char **path = (const char *[]){"/", "/.", "/..", NULL}; *path; path++)
        failures += expect_existing(AT_FDCWD, *path);

    char directory[] = "/data/local/tmp/xkcnsu-mkdir-check-XXXXXX";
    if (!mkdtemp(directory))
        return 3;
    int fd = open(directory, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0)
        return 4;
    failures += expect_existing(fd, ".");
    failures += expect_existing(fd, "..");
    int regular = openat(fd, "file", O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    bool file_created = regular >= 0;
    if (regular >= 0)
        close(regular);
    bool symlink_created = symlinkat("file", fd, "symlink") == 0;
    bool link_created = linkat(fd, "file", fd, "hardlink", 0) == 0;
    bool fifo_created = mknodat(fd, "fifo", S_IFIFO | 0600, 0) == 0;
    if (!file_created || !symlink_created || !link_created || !fifo_created)
        failures++;
    printf("{\"file\":%s,\"symlinkat\":%s,\"linkat\":%s,\"mknodat\":%s}\n",
           file_created ? "true" : "false", symlink_created ? "true" : "false",
           link_created ? "true" : "false", fifo_created ? "true" : "false");
    unlinkat(fd, "fifo", 0);
    unlinkat(fd, "hardlink", 0);
    unlinkat(fd, "symlink", 0);
    unlinkat(fd, "file", 0);
    close(fd);

    char leaf[256], middle[256], first[256];
    if (snprintf(leaf, sizeof(leaf), "%s/a/b/c", directory) >= (int)sizeof(leaf) ||
        snprintf(middle, sizeof(middle), "%s/a/b", directory) >= (int)sizeof(middle) ||
        snprintf(first, sizeof(first), "%s/a", directory) >= (int)sizeof(first))
        return 5;

    pid_t child = fork();
    if (child == 0) {
        execl(argv[1], argv[1], "mkdir", "-p", leaf, (char *)NULL);
        _exit(127);
    }
    int status = 0;
    bool command_ok = child > 0 && waitpid(child, &status, 0) == child &&
                      WIFEXITED(status) && WEXITSTATUS(status) == 0;
    struct stat info;
    bool created = stat(leaf, &info) == 0 && S_ISDIR(info.st_mode);
    if (!command_ok || !created)
        failures++;
    printf("{\"busybox_recursive_mkdir\":%s,\"leaf_created\":%s}\n",
           command_ok ? "true" : "false", created ? "true" : "false");

    if (created)
        rmdir(leaf);
    rmdir(middle);
    rmdir(first);
    rmdir(directory);
    printf("{\"completed\":%s,\"failures\":%d}\n",
           failures == 0 ? "true" : "false", failures);
    return failures ? 1 : 0;
}
