#include <CommonCrypto/CommonCryptor.h>
#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <fts.h>
#include <libproc.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/proc_info.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MAX_TARGETS 64
#define MAX_IMAGE_PATTERNS 32
#define MAX_IMAGE_FILES 50000
#define SCAN_CHUNK (4U * 1024U * 1024U)
#define SCAN_OVERLAP 160U

typedef struct {
    char absolute_path[PROC_PIDPATHINFO_MAXSIZE];
    char relative_path[PROC_PIDPATHINFO_MAXSIZE];
    unsigned char salt[16];
    unsigned char key[32];
    int found;
} db_target_t;

typedef struct {
    unsigned char ciphertext[16];
    unsigned char key[16];
    int found;
} image_target_t;

static void secure_zero(void *pointer, size_t length) {
    volatile unsigned char *cursor = (volatile unsigned char *)pointer;
    while (length-- > 0) *cursor++ = 0;
}

static int reverse_name_order(const FTSENT **left, const FTSENT **right) {
    return strcmp((*right)->fts_name, (*left)->fts_name);
}

static int write_all(int fd, const void *buffer, size_t length) {
    const unsigned char *cursor = (const unsigned char *)buffer;
    while (length > 0) {
        ssize_t written = write(fd, cursor, length);
        if (written < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        cursor += (size_t)written;
        length -= (size_t)written;
    }
    return 0;
}

static int write_u16(int fd, uint16_t value) {
    unsigned char bytes[2] = {(unsigned char)(value >> 8), (unsigned char)(value & 0xff)};
    return write_all(fd, bytes, sizeof(bytes));
}

static int has_suffix(const char *value, const char *suffix) {
    size_t value_len = strlen(value), suffix_len = strlen(suffix);
    return value_len >= suffix_len && strcmp(value + value_len - suffix_len, suffix) == 0;
}

static int decimal_between(const char *value, const char *prefix, const char *suffix) {
    size_t prefix_len = strlen(prefix), value_len = strlen(value), suffix_len = strlen(suffix);
    if (value_len <= prefix_len + suffix_len || strncmp(value, prefix, prefix_len) != 0 ||
        strcmp(value + value_len - suffix_len, suffix) != 0) return 0;
    for (size_t i = prefix_len; i < value_len - suffix_len; i++) {
        if (value[i] < '0' || value[i] > '9') return 0;
    }
    return 1;
}

static int is_target_relative(const char *relative) {
    if (strcmp(relative, "contact/contact.db") == 0) return 1;
    if (strcmp(relative, "session/session.db") == 0) return 1;
    if (strcmp(relative, "message/message_resource.db") == 0) return 1;
    if (decimal_between(relative, "message/message_", ".db")) return 1;
    if (decimal_between(relative, "message/media_", ".db")) return 1;
    return 0;
}

static pid_t find_wechat_pid(void) {
    int bytes = proc_listpids(PROC_ALL_PIDS, 0, NULL, 0);
    if (bytes <= 0) return -1;
    pid_t *pids = calloc((size_t)bytes / sizeof(pid_t), sizeof(pid_t));
    if (!pids) return -1;
    bytes = proc_listpids(PROC_ALL_PIDS, 0, pids, bytes);
    pid_t match = -1;
    int matches = 0;
    for (int i = 0; i < bytes / (int)sizeof(pid_t); i++) {
        if (pids[i] <= 0) continue;
        char name[PROC_PIDPATHINFO_MAXSIZE] = {0};
        if (proc_name(pids[i], name, sizeof(name)) > 0 && strcmp(name, "WeChat") == 0) {
            match = pids[i];
            matches++;
        }
    }
    free(pids);
    return matches == 1 ? match : -1;
}

static int add_target(db_target_t *targets, int *count, const char *absolute, const char *relative) {
    for (int i = 0; i < *count; i++) {
        if (strcmp(targets[i].relative_path, relative) == 0) return 0;
    }
    if (*count >= MAX_TARGETS) return -1;
    int fd = open(absolute, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    unsigned char salt[16];
    ssize_t got = read(fd, salt, sizeof(salt));
    close(fd);
    if (got != (ssize_t)sizeof(salt) || memcmp(salt, "SQLite format 3", 15) == 0) return -1;
    db_target_t *target = &targets[*count];
    memset(target, 0, sizeof(*target));
    if (snprintf(target->absolute_path, sizeof(target->absolute_path), "%s", absolute) >=
            (int)sizeof(target->absolute_path) ||
        snprintf(target->relative_path, sizeof(target->relative_path), "%s", relative) >=
            (int)sizeof(target->relative_path)) return -1;
    memcpy(target->salt, salt, sizeof(salt));
    (*count)++;
    return 0;
}

static int discover_targets(pid_t pid, db_target_t *targets, int *target_count,
                            unsigned char account_digest[CC_SHA256_DIGEST_LENGTH],
                            char account_root_out[PROC_PIDPATHINFO_MAXSIZE]) {
    int bytes = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, NULL, 0);
    if (bytes <= 0) return -1;
    struct proc_fdinfo *fds = malloc((size_t)bytes);
    if (!fds) return -1;
    bytes = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, fds, bytes);
    if (bytes <= 0) {
        free(fds);
        return -1;
    }
    char account_root[PROC_PIDPATHINFO_MAXSIZE] = {0};
    int fd_count = bytes / (int)sizeof(struct proc_fdinfo);
    for (int i = 0; i < fd_count; i++) {
        if (fds[i].proc_fdtype != PROX_FDTYPE_VNODE) continue;
        struct vnode_fdinfowithpath info;
        int result = proc_pidfdinfo(pid, fds[i].proc_fd, PROC_PIDFDVNODEPATHINFO,
                                    &info, sizeof(info));
        if (result <= 0) continue;
        const char *path = info.pvip.vip_path;
        const char *storage = strstr(path, "/db_storage/");
        if (!storage || !strstr(path, "/xwechat_files/") || !has_suffix(path, ".db")) continue;
        size_t root_len = (size_t)(storage - path);
        if (root_len == 0 || root_len >= sizeof(account_root)) {
            free(fds);
            return -1;
        }
        if (!account_root[0]) {
            memcpy(account_root, path, root_len);
            account_root[root_len] = '\0';
        } else if (strlen(account_root) != root_len || strncmp(account_root, path, root_len) != 0) {
            free(fds);
            return -1;
        }
        const char *relative = storage + strlen("/db_storage/");
        if (!is_target_relative(relative)) continue;
        if (add_target(targets, target_count, path, relative) != 0) {
            free(fds);
            return -1;
        }
    }
    free(fds);
    if (*target_count < 3 || !account_root[0]) return -1;
    const char *account_name = strrchr(account_root, '/');
    account_name = account_name ? account_name + 1 : account_root;
    CC_SHA256(account_name, (CC_LONG)strlen(account_name), account_digest);
    if (snprintf(account_root_out, PROC_PIDPATHINFO_MAXSIZE, "%s", account_root) >=
        PROC_PIDPATHINFO_MAXSIZE) return -1;
    return 0;
}

static int discover_image_patterns(const char *account_root, image_target_t *targets,
                                   int *target_count) {
    char attach_root[PROC_PIDPATHINFO_MAXSIZE];
    if (snprintf(attach_root, sizeof(attach_root), "%s/msg/attach", account_root) >=
        (int)sizeof(attach_root)) return -1;
    struct stat root_info;
    if (lstat(attach_root, &root_info) != 0 || !S_ISDIR(root_info.st_mode) ||
        S_ISLNK(root_info.st_mode)) return 0;
    char *paths[] = {attach_root, NULL};
    FTS *tree = fts_open(paths, FTS_PHYSICAL | FTS_NOCHDIR, reverse_name_order);
    if (!tree) return -1;
    int files_seen = 0;
    struct timespec started;
    if (clock_gettime(CLOCK_MONOTONIC, &started) != 0) {
        fts_close(tree);
        return -1;
    }
    FTSENT *entry;
    while ((entry = fts_read(tree)) != NULL && files_seen < MAX_IMAGE_FILES &&
           *target_count < MAX_IMAGE_PATTERNS) {
        if (entry->fts_info != FTS_F) continue;
        files_seen++;
        if ((files_seen & 127) == 0) {
            struct timespec current;
            if (clock_gettime(CLOCK_MONOTONIC, &current) != 0 ||
                current.tv_sec - started.tv_sec >= 15) break;
        }
        const char *extension = strrchr(entry->fts_name, '.');
        if (!extension || strcasecmp(extension, ".dat") != 0) continue;
        int fd = open(entry->fts_accpath, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (fd < 0) continue;
        unsigned char header[31];
        ssize_t got = read(fd, header, sizeof(header));
        close(fd);
        if (got != (ssize_t)sizeof(header) || memcmp(header, "\x07\x08V2\x08\x07", 6) != 0)
            continue;
        int duplicate = 0;
        for (int i = 0; i < *target_count; i++) {
            if (memcmp(targets[i].ciphertext, header + 15, 16) == 0) duplicate = 1;
        }
        if (duplicate) continue;
        memcpy(targets[*target_count].ciphertext, header + 15, 16);
        (*target_count)++;
    }
    fts_close(tree);
    return 0;
}

static int hex_value(unsigned char value) {
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
}

static int decode_hex(const unsigned char *source, size_t bytes, unsigned char *destination) {
    for (size_t i = 0; i < bytes; i++) {
        int high = hex_value(source[i * 2]);
        int low = hex_value(source[i * 2 + 1]);
        if (high < 0 || low < 0) return -1;
        destination[i] = (unsigned char)((high << 4) | low);
    }
    return 0;
}

static int all_found(const db_target_t *targets, int target_count) {
    for (int i = 0; i < target_count; i++) if (!targets[i].found) return 0;
    return 1;
}

static int all_images_found(const image_target_t *targets, int target_count) {
    for (int i = 0; i < target_count; i++) if (!targets[i].found) return 0;
    return 1;
}

static int image_magic(const unsigned char plain[16]) {
    if (plain[0] == 0xff && plain[1] == 0xd8 && plain[2] == 0xff) return 1;
    if (memcmp(plain, "\x89PNG\r\n\x1a\n", 8) == 0) return 1;
    if (memcmp(plain, "GIF8", 4) == 0) return 1;
    if (plain[0] == 'B' && plain[1] == 'M') return 1;
    if (memcmp(plain, "RIFF", 4) == 0 && memcmp(plain + 8, "WEBP", 4) == 0) return 1;
    if (memcmp(plain, "II*\x00", 4) == 0 || memcmp(plain, "MM\x00*", 4) == 0) return 1;
    return 0;
}

static void test_image_key(const unsigned char candidate[16], image_target_t *targets,
                           int target_count) {
    for (int i = 0; i < target_count; i++) {
        if (targets[i].found) continue;
        unsigned char plain[16];
        size_t moved = 0;
        CCCryptorStatus result = CCCrypt(kCCDecrypt, kCCAlgorithmAES128, kCCOptionECBMode,
                                         candidate, 16, NULL, targets[i].ciphertext, 16,
                                         plain, sizeof(plain), &moved);
        if (result == kCCSuccess && moved == 16 && image_magic(plain)) {
            memcpy(targets[i].key, candidate, 16);
            targets[i].found = 1;
        }
        secure_zero(plain, sizeof(plain));
    }
}

static void inspect_image_candidates(const unsigned char *buffer, size_t length,
                                     image_target_t *targets, int target_count) {
    if (target_count == 0 || length < 16 || all_images_found(targets, target_count)) return;
    for (size_t offset = 0; offset + 16 <= length && !all_images_found(targets, target_count);
         offset += 16) {
        test_image_key(buffer + offset, targets, target_count);
    }
    size_t run_start = 0, run_length = 0;
    for (size_t offset = 0; offset <= length && !all_images_found(targets, target_count); offset++) {
        int is_hex = offset < length &&
            ((buffer[offset] >= '0' && buffer[offset] <= '9') ||
             (buffer[offset] >= 'a' && buffer[offset] <= 'f'));
        if (is_hex) {
            if (run_length == 0) run_start = offset;
            run_length++;
            continue;
        }
        if (run_length >= 16) {
            for (size_t candidate = run_start;
                 candidate + 16 <= run_start + run_length &&
                 !all_images_found(targets, target_count); candidate++) {
                if (candidate % 16 != 0) test_image_key(buffer + candidate, targets, target_count);
            }
        }
        run_length = 0;
    }
}

static int inspect_buffer(const unsigned char *buffer, size_t length,
                          db_target_t *targets, int target_count) {
    if (length < 99) return 0;
    for (size_t offset = 0; offset + 99 <= length; offset++) {
        if ((buffer[offset] != 'x' && buffer[offset] != 'X') || buffer[offset + 1] != '\'') continue;
        if (buffer[offset + 98] != '\'') continue;
        unsigned char candidate_key[32], candidate_salt[16];
        if (decode_hex(buffer + offset + 2, 32, candidate_key) != 0 ||
            decode_hex(buffer + offset + 66, 16, candidate_salt) != 0) continue;
        for (int i = 0; i < target_count; i++) {
            if (memcmp(candidate_salt, targets[i].salt, 16) != 0) continue;
            if (targets[i].found && memcmp(targets[i].key, candidate_key, 32) != 0) {
                secure_zero(candidate_key, sizeof(candidate_key));
                secure_zero(candidate_salt, sizeof(candidate_salt));
                return -1;
            }
            memcpy(targets[i].key, candidate_key, 32);
            targets[i].found = 1;
        }
        secure_zero(candidate_key, sizeof(candidate_key));
        secure_zero(candidate_salt, sizeof(candidate_salt));
        if (all_found(targets, target_count)) return 1;
    }
    return 0;
}

static int scan_process(pid_t pid, db_target_t *targets, int target_count,
                        image_target_t *image_targets, int image_target_count) {
    mach_port_t task = MACH_PORT_NULL;
    kern_return_t result = task_for_pid(mach_task_self(), pid, &task);
    if (result != KERN_SUCCESS) return -2;
    mach_vm_address_t address = 0;
    natural_t depth = 0;
    while (!all_found(targets, target_count) ||
           (image_target_count > 0 && !all_images_found(image_targets, image_target_count))) {
        mach_vm_size_t region_size = 0;
        vm_region_submap_info_data_64_t info;
        mach_msg_type_number_t count = VM_REGION_SUBMAP_INFO_COUNT_64;
        result = mach_vm_region_recurse(task, &address, &region_size, &depth,
                                        (vm_region_recurse_info_t)&info, &count);
        if (result != KERN_SUCCESS) break;
        if (info.is_submap) {
            depth++;
            continue;
        }
        mach_vm_address_t region_start = address;
        address += region_size;
        if (!(info.protection & VM_PROT_READ) || !(info.protection & VM_PROT_WRITE) ||
            (info.protection & VM_PROT_EXECUTE) || region_size < 99) continue;
        mach_vm_size_t consumed = 0;
        while (consumed < region_size &&
               (!all_found(targets, target_count) ||
                (image_target_count > 0 && !all_images_found(image_targets, image_target_count)))) {
            mach_vm_size_t wanted = region_size - consumed;
            if (wanted > SCAN_CHUNK) wanted = SCAN_CHUNK;
            unsigned char *buffer = malloc((size_t)wanted);
            if (!buffer) {
                mach_port_deallocate(mach_task_self(), task);
                return -1;
            }
            mach_vm_size_t received = 0;
            result = mach_vm_read_overwrite(task, region_start + consumed, wanted,
                                            (mach_vm_address_t)buffer, &received);
            if (result == KERN_SUCCESS && received > 0) {
                int inspected = inspect_buffer(buffer, (size_t)received, targets, target_count);
                inspect_image_candidates(buffer, (size_t)received, image_targets, image_target_count);
                secure_zero(buffer, (size_t)received);
                free(buffer);
                if (inspected < 0) {
                    mach_port_deallocate(mach_task_self(), task);
                    return -1;
                }
                if (inspected > 0 &&
                    (image_target_count == 0 || all_images_found(image_targets, image_target_count))) break;
                if (received <= SCAN_OVERLAP) break;
                consumed += received - SCAN_OVERLAP;
            } else {
                free(buffer);
                break;
            }
        }
    }
    mach_port_deallocate(mach_task_self(), task);
    if (!all_found(targets, target_count)) return -3;
    if (image_target_count > 0 && !all_images_found(image_targets, image_target_count)) return -4;
    return 0;
}

static int emit_protocol(db_target_t *targets, int target_count,
                         image_target_t *image_targets, int image_target_count,
                         const unsigned char account_digest[CC_SHA256_DIGEST_LENGTH]) {
    if (isatty(STDOUT_FILENO)) return -1;
    const unsigned char magic[4] = {'L', 'W', 'K', '1'};
    if (write_all(STDOUT_FILENO, magic, sizeof(magic)) != 0 ||
        write_all(STDOUT_FILENO, account_digest, CC_SHA256_DIGEST_LENGTH) != 0) return -1;
    const char kind[] = "database";
    for (int i = 0; i < target_count; i++) {
        size_t path_len = strlen(targets[i].relative_path);
        if (!targets[i].found || path_len == 0 || path_len > UINT16_MAX) return -1;
        if (write_u16(STDOUT_FILENO, (uint16_t)path_len) != 0 ||
            write_u16(STDOUT_FILENO, 32) != 0 || write_u16(STDOUT_FILENO, 16) != 0 ||
            write_u16(STDOUT_FILENO, (uint16_t)strlen(kind)) != 0 ||
            write_all(STDOUT_FILENO, targets[i].relative_path, path_len) != 0 ||
            write_all(STDOUT_FILENO, kind, strlen(kind)) != 0 ||
            write_all(STDOUT_FILENO, targets[i].salt, 16) != 0 ||
            write_all(STDOUT_FILENO, targets[i].key, 32) != 0) return -1;
    }
    const char image_kind[] = "image_aes";
    const char image_path[] = "msg/attach";
    for (int i = 0; i < image_target_count; i++) {
        if (!image_targets[i].found) continue;
        if (write_u16(STDOUT_FILENO, (uint16_t)strlen(image_path)) != 0 ||
            write_u16(STDOUT_FILENO, 16) != 0 || write_u16(STDOUT_FILENO, 16) != 0 ||
            write_u16(STDOUT_FILENO, (uint16_t)strlen(image_kind)) != 0 ||
            write_all(STDOUT_FILENO, image_path, strlen(image_path)) != 0 ||
            write_all(STDOUT_FILENO, image_kind, strlen(image_kind)) != 0 ||
            write_all(STDOUT_FILENO, image_targets[i].ciphertext, 16) != 0 ||
            write_all(STDOUT_FILENO, image_targets[i].key, 16) != 0) return -1;
    }
    for (int i = 0; i < 4; i++) if (write_u16(STDOUT_FILENO, 0) != 0) return -1;
    return 0;
}

static int self_test(void) {
    db_target_t target;
    memset(&target, 0, sizeof(target));
    for (int i = 0; i < 16; i++) target.salt[i] = (unsigned char)(0xa0 + i);
    unsigned char buffer[100];
    memset(buffer, 0, sizeof(buffer));
    buffer[0] = 'x'; buffer[1] = '\'';
    for (int i = 0; i < 32; i++) snprintf((char *)buffer + 2 + i * 2, 3, "%02x", i);
    for (int i = 0; i < 16; i++) snprintf((char *)buffer + 66 + i * 2, 3, "%02x", target.salt[i]);
    buffer[98] = '\'';
    int result = inspect_buffer(buffer, 99, &target, 1);
    image_target_t image_target;
    memset(&image_target, 0, sizeof(image_target));
    const unsigned char image_key[16] = {
        '0','1','2','3','4','5','6','7','8','9','a','b','c','d','e','f'
    };
    unsigned char image_plain[16] = {0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 'J', 'F', 'I', 'F'};
    size_t moved = 0;
    CCCryptorStatus crypt = CCCrypt(kCCEncrypt, kCCAlgorithmAES128, kCCOptionECBMode,
                                    image_key, 16, NULL, image_plain, 16,
                                    image_target.ciphertext, 16, &moved);
    unsigned char image_buffer[64];
    memset(image_buffer, 0x5a, sizeof(image_buffer));
    memcpy(image_buffer + 16, image_key, 16);
    inspect_image_candidates(image_buffer, sizeof(image_buffer), &image_target, 1);
    int passed = result == 1 && target.found && crypt == kCCSuccess && moved == 16 &&
                 image_target.found && memcmp(image_target.key, image_key, 16) == 0;
    secure_zero(&target, sizeof(target));
    secure_zero(&image_target, sizeof(image_target));
    secure_zero(buffer, sizeof(buffer));
    secure_zero(image_buffer, sizeof(image_buffer));
    if (!passed) return 2;
    printf("{\"verdict\":\"pass\",\"checks\":5}\n");
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--self-test") == 0) return self_test();
    const char *enabled = getenv("SYNC_CENTER_ENABLE_ARCHIVED_CODE");
    const char *home = getenv("SYNC_CENTER_HOME");
    struct stat home_status;
    if (!enabled || strcmp(enabled, "1") != 0 || !home || home[0] != '/' ||
        stat(home, &home_status) != 0 || !S_ISDIR(home_status.st_mode)) {
        fprintf(stderr, "Archived integration is disabled; explicit opt-in and an isolated directory are required.\n");
        return 2;
    }
    pid_t pid = find_wechat_pid();
    if (pid <= 0) {
        fprintf(stderr, "wechat_process_ambiguous\n");
        return 2;
    }
    db_target_t targets[MAX_TARGETS];
    memset(targets, 0, sizeof(targets));
    int target_count = 0;
    image_target_t image_targets[MAX_IMAGE_PATTERNS];
    memset(image_targets, 0, sizeof(image_targets));
    int image_target_count = 0;
    unsigned char account_digest[CC_SHA256_DIGEST_LENGTH];
    char account_root[PROC_PIDPATHINFO_MAXSIZE] = {0};
    if (discover_targets(pid, targets, &target_count, account_digest, account_root) != 0) {
        fprintf(stderr, "wechat_database_targets_unavailable\n");
        return 2;
    }
    if (discover_image_patterns(account_root, image_targets, &image_target_count) != 0) {
        fprintf(stderr, "wechat_image_patterns_unavailable\n");
        return 2;
    }
    if (argc == 2 && strcmp(argv[1], "--dry-run-metadata") == 0) {
        int message_count = 0, media_count = 0;
        for (int i = 0; i < target_count; i++) {
            message_count += decimal_between(targets[i].relative_path, "message/message_", ".db");
            media_count += decimal_between(targets[i].relative_path, "message/media_", ".db");
        }
        printf("{\"verdict\":\"pass\",\"account_roots\":1,\"database_targets\":%d,"
               "\"message_shards\":%d,\"media_shards\":%d,\"image_patterns\":%d}\n",
               target_count, message_count, media_count, image_target_count);
        return 0;
    }
    int scan_result = scan_process(pid, targets, target_count, image_targets, image_target_count);
    if (scan_result == -2) {
        fprintf(stderr, "task_port_unavailable\n");
        return 3;
    }
    if (scan_result != 0) {
        fprintf(stderr, "%s\n", scan_result == -4 ? "image_keys_incomplete" :
                                                     "database_keys_incomplete");
        return 4;
    }
    int emit_result = emit_protocol(targets, target_count, image_targets, image_target_count,
                                    account_digest);
    secure_zero(account_digest, sizeof(account_digest));
    secure_zero(targets, sizeof(targets));
    secure_zero(image_targets, sizeof(image_targets));
    secure_zero(account_root, sizeof(account_root));
    if (emit_result != 0) {
        fprintf(stderr, "secret_output_refused\n");
        return 5;
    }
    return 0;
}
