#define _GNU_SOURCE

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <libgen.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *env_or_default(const char *name, const char *fallback) {
  const char *value = getenv(name);
  return (value && value[0]) ? value : fallback;
}

static int read_pid_file(const char *path, pid_t *pid) {
  FILE *file = fopen(path, "r");
  long value;

  if (!file) {
    return 0;
  }

  if (fscanf(file, "%ld", &value) != 1 || value <= 0) {
    fclose(file);
    return 0;
  }

  fclose(file);
  *pid = (pid_t)value;
  return 1;
}

static int cmdline_matches(pid_t pid, const char *config) {
  char path[64];
  char buffer[8192];
  int fd;
  ssize_t bytes;
  int has_binary = 0;
  int has_config_flag = 0;
  int has_config_value = 0;
  size_t i = 0;

  snprintf(path, sizeof(path), "/proc/%ld/cmdline", (long)pid);
  fd = open(path, O_RDONLY);
  if (fd < 0) {
    return 0;
  }

  bytes = read(fd, buffer, sizeof(buffer) - 1);
  close(fd);
  if (bytes <= 0) {
    return 0;
  }

  buffer[bytes] = '\0';
  while (i < (size_t)bytes) {
    const char *arg = &buffer[i];
    size_t len = strlen(arg);

    if (strstr(arg, "cc-connect") != NULL) {
      has_binary = 1;
    }
    if (strcmp(arg, "--config") == 0) {
      has_config_flag = 1;
    }
    if (strcmp(arg, config) == 0) {
      has_config_value = 1;
    }

    i += len + 1;
  }

  return has_binary && has_config_flag && has_config_value;
}

static int find_matching_process(const char *config, pid_t *pid_out) {
  DIR *proc = opendir("/proc");
  struct dirent *entry;
  pid_t self = getpid();

  if (!proc) {
    return 0;
  }

  while ((entry = readdir(proc)) != NULL) {
    pid_t pid;
    char *end = NULL;

    if (!isdigit((unsigned char)entry->d_name[0])) {
      continue;
    }

    errno = 0;
    pid = (pid_t)strtol(entry->d_name, &end, 10);
    if (errno || !end || *end || pid <= 0 || pid == self) {
      continue;
    }

    if (cmdline_matches(pid, config)) {
      closedir(proc);
      *pid_out = pid;
      return 1;
    }
  }

  closedir(proc);
  return 0;
}

static int write_pid_file(const char *path, pid_t pid) {
  FILE *file = fopen(path, "w");

  if (!file) {
    return -1;
  }

  fprintf(file, "%ld\n", (long)pid);
  fclose(file);
  return 0;
}

static int chdir_to_executable_dir(const char *argv0) {
  char resolved[PATH_MAX];
  char fallback[PATH_MAX];
  char *dir;

  if (realpath(argv0, resolved) == NULL) {
    if (!strchr(argv0, '/')) {
      return 0;
    }
    if (getcwd(fallback, sizeof(fallback)) == NULL) {
      return -1;
    }
    if (snprintf(resolved, sizeof(resolved), "%s/%s", fallback, argv0) >=
        (int)sizeof(resolved)) {
      errno = ENAMETOOLONG;
      return -1;
    }
  }

  dir = dirname(resolved);
  return chdir(dir);
}

int main(int argc, char **argv) {
  const char *config = env_or_default("CC_CONFIG", "/home/ai/.cc-connect/config.toml");
  const char *cc_bin = env_or_default("CC_BIN", "cc-connect");
  const char *log_file = env_or_default("CC_LOG_FILE", "logs/cc-connect.log");
  const char *pid_file = env_or_default("CC_PID_FILE", "logs/cc-connect.pid");
  pid_t pid = 0;

  (void)argc;

  if (chdir_to_executable_dir(argv[0]) != 0) {
    perror("failed to change to executable directory");
    return 1;
  }

  if (mkdir("logs", 0755) != 0 && errno != EEXIST) {
    perror("failed to create logs directory");
    return 1;
  }

  if (access(config, F_OK) != 0) {
    fprintf(stderr, "cc-connect config not found: %s\n", config);
    return 1;
  }

  if (read_pid_file(pid_file, &pid) && kill(pid, 0) == 0) {
    printf("cc-connect already running: %ld\n", (long)pid);
    return 0;
  }

  if (find_matching_process(config, &pid)) {
    kill(pid, SIGTERM);
    sleep(1);
  }

  pid = fork();
  if (pid < 0) {
    perror("failed to fork");
    return 1;
  }

  if (pid == 0) {
    int null_fd;
    int log_fd;

    setsid();

    null_fd = open("/dev/null", O_RDONLY);
    if (null_fd >= 0) {
      dup2(null_fd, STDIN_FILENO);
      close(null_fd);
    }

    log_fd = open(log_file, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (log_fd >= 0) {
      dup2(log_fd, STDOUT_FILENO);
      dup2(log_fd, STDERR_FILENO);
      close(log_fd);
    }

    execlp(cc_bin, cc_bin, "--config", config, "--force", (char *)NULL);
    perror("failed to exec cc-connect");
    _exit(127);
  }

  if (write_pid_file(pid_file, pid) != 0) {
    perror("failed to write pid file");
    return 1;
  }

  for (int i = 0; i < 10; i++) {
    if (find_matching_process(config, &pid)) {
      printf("cc-connect started: %s\n", pid_file);
      return 0;
    }
    sleep(1);
  }

  fprintf(stderr, "cc-connect start requested; see %s\n", log_file);
  return 0;
}
