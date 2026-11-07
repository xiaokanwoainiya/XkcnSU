/* Test-only SUSFS check. It touches only its own temporary directory. */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "susfs_abi.h"

static int app_can_open(const char *path)
{
    pid_t child = fork();
    if (child < 0)
        return -1;
    if (child == 0) {
        if (setgid(28001) || setuid(28001))
            _exit(3);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd >= 0) {
            close(fd);
            _exit(0);
        }
        _exit(errno == ENOENT || errno == EACCES ? 1 : 2);
    }
    int status = 0;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status))
        return -1;
    return WEXITSTATUS(status);
}

int main(void)
{
    if (getuid() != 0)
        return 2;
    char directory[] = "/data/local/tmp/xkcnsu-susfs-check-XXXXXX";
    if (!mkdtemp(directory) || chmod(directory, 0755))
        return 3;
    char path[256];
    if (snprintf(path, sizeof(path), "%s/probe", directory) >= (int)sizeof(path))
        return 4;
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
    if (fd < 0)
        return 5;
    if (write(fd, "XKCNSU\n", 7) != 7) {
        close(fd);
        return 6;
    }
    close(fd);
    int before = app_can_open(path);
    struct st_susfs_sus_path request = {.err = ERR_CMD_NOT_SUPPORTED};
    snprintf(request.target_pathname, sizeof(request.target_pathname), "%s", path);
    syscall(SYS_reboot, (long)KSU_INSTALL_MAGIC1, (long)SUSFS_MAGIC,
            (long)CMD_SUSFS_ADD_SUS_PATH, &request);
    int root_fd = open(path, O_RDONLY | O_CLOEXEC);
    int root_visible = root_fd >= 0;
    if (root_fd >= 0)
        close(root_fd);
    int after = app_can_open(path);
    int passed = before == 0 && request.err == 0 && root_visible && after == 1;
    printf("{\"completed\":%s,\"baseline_app_open\":%d,\"rule_errno\":%d,"
           "\"root_still_visible\":%s,\"app_hidden_after_rule\":%s,\"path\":\"%s\"}\n",
           passed ? "true" : "false", before, request.err,
           root_visible ? "true" : "false", after == 1 ? "true" : "false", path);
    unlink(path);
    rmdir(directory);
    return passed ? 0 : 1;
}
