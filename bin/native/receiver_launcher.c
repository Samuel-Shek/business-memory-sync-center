#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *MEMORY_ROOT = NULL;
static const char *PYTHON_BIN = NULL;
static const char *RECEIVER_SCRIPT = NULL;

static volatile sig_atomic_t child_pid = -1;

static void forward_signal(int signo) {
    pid_t pid = (pid_t)child_pid;
    if (pid > 0) {
        (void)kill(pid, signo);
    }
}

static int authorize_memory_root(void) {
    DIR *directory = opendir(MEMORY_ROOT);
    if (directory == NULL) {
        fputs("business memory authorization is not available\n", stderr);
        return 77;
    }
    errno = 0;
    while (readdir(directory) != NULL) {
    }
    int read_error = errno;
    (void)closedir(directory);
    if (read_error != 0) {
        fputs("business memory directory read failed\n", stderr);
        return 77;
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *enabled = getenv("SYNC_CENTER_ENABLE_ARCHIVED_CODE");
    if (!enabled || strcmp(enabled, "1") != 0) return 78;
    MEMORY_ROOT = getenv("SYNC_CENTER_MEMORY_ROOT");
    if (!MEMORY_ROOT || MEMORY_ROOT[0] != '/') return 78;
    PYTHON_BIN = getenv("SYNC_CENTER_PYTHON_BIN");
    if (!PYTHON_BIN) PYTHON_BIN = "/usr/bin/python3";
    RECEIVER_SCRIPT = getenv("SYNC_CENTER_RECEIVER_SCRIPT");
    if (!RECEIVER_SCRIPT || RECEIVER_SCRIPT[0] != '/') return 78;

    (void)umask(0077);
    if (argc == 2 && strcmp(argv[1], "--authorize") == 0) {
        return authorize_memory_root();
    }
    if (argc != 1) {
        return 64;
    }

    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = forward_signal;
    sigemptyset(&action.sa_mask);
    (void)sigaction(SIGTERM, &action, NULL);
    (void)sigaction(SIGINT, &action, NULL);
    (void)sigaction(SIGHUP, &action, NULL);

    pid_t pid = fork();
    if (pid < 0) {
        fputs("receiver launch fork failed\n", stderr);
        return 70;
    }
    if (pid == 0) {
        execl(PYTHON_BIN, PYTHON_BIN, RECEIVER_SCRIPT, (char *)NULL);
        _exit(127);
    }

    child_pid = (sig_atomic_t)pid;
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno == EINTR) {
            continue;
        }
        fputs("receiver wait failed\n", stderr);
        return 70;
    }
    child_pid = -1;
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 70;
}
