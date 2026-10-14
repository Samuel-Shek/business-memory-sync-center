#include <CommonCrypto/CommonCryptor.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define MAX_IMAGE_BYTES (512U * 1024U * 1024U)

static const unsigned char V2_MAGIC[6] = {0x07, 0x08, 'V', '2', 0x08, 0x07};
static const unsigned char V1_MAGIC[6] = {0x07, 0x08, 'V', '1', 0x08, 0x07};

static void secure_zero(void *pointer, size_t length) {
    volatile unsigned char *cursor = (volatile unsigned char *)pointer;
    while (length-- > 0) *cursor++ = 0;
}

static int read_all_fd(int fd, unsigned char *buffer, size_t length) {
    while (length > 0) {
        ssize_t got = read(fd, buffer, length);
        if (got < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (got == 0) return -1;
        buffer += (size_t)got;
        length -= (size_t)got;
    }
    return 0;
}

static int write_all_fd(int fd, const unsigned char *buffer, size_t length) {
    while (length > 0) {
        ssize_t wrote = write(fd, buffer, length);
        if (wrote < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        buffer += (size_t)wrote;
        length -= (size_t)wrote;
    }
    return 0;
}

static const char *format_for(const unsigned char *data, size_t length) {
    if (length >= 3 && data[0] == 0xff && data[1] == 0xd8 && data[2] == 0xff) return "jpg";
    if (length >= 8 && memcmp(data, "\x89PNG\r\n\x1a\n", 8) == 0) return "png";
    if (length >= 4 && memcmp(data, "GIF8", 4) == 0) return "gif";
    if (length >= 2 && data[0] == 'B' && data[1] == 'M') return "bmp";
    if (length >= 12 && memcmp(data, "RIFF", 4) == 0 && memcmp(data + 8, "WEBP", 4) == 0) return "webp";
    if (length >= 4 && data[0] == 0x49 && data[1] == 0x49 && data[2] == 0x2a && data[3] == 0x00) return "tif";
    if (length >= 4 && memcmp(data, "wxgf", 4) == 0) return "hevc";
    return NULL;
}

static uint32_t little_u32(const unsigned char *value) {
    return (uint32_t)value[0] | ((uint32_t)value[1] << 8) | ((uint32_t)value[2] << 16) |
           ((uint32_t)value[3] << 24);
}

static size_t aligned_aes_size(uint32_t plain_size) {
    size_t remainder = plain_size % 16U;
    return remainder ? (size_t)plain_size + (16U - remainder) : (size_t)plain_size + 16U;
}

static int contains_iend(const unsigned char *data, size_t length) {
    if (length < 12) return 0;
    size_t start = length > 16 ? length - 16 : 0;
    for (size_t i = start; i + 4 <= length; i++) if (memcmp(data + i, "IEND", 4) == 0) return 1;
    return 0;
}

static int infer_xor(const char *format, const unsigned char *cipher, size_t length,
                     unsigned char *key_out) {
    if (length == 0) {
        *key_out = 0;
        return 0;
    }
    if (strcmp(format, "jpg") == 0 && length >= 2) {
        unsigned char key = cipher[length - 1] ^ 0xd9;
        if ((cipher[length - 2] ^ key) == 0xff) {
            *key_out = key;
            return 0;
        }
        return -1;
    }
    if (strcmp(format, "png") == 0) {
        unsigned char tail[32];
        if (length > sizeof(tail)) {
            cipher += length - sizeof(tail);
            length = sizeof(tail);
        }
        for (int candidate = 0; candidate <= 255; candidate++) {
            for (size_t i = 0; i < length; i++) tail[i] = cipher[i] ^ (unsigned char)candidate;
            if (contains_iend(tail, length)) {
                *key_out = (unsigned char)candidate;
                return 0;
            }
        }
    }
    return -1;
}

static int decode_old_xor(const unsigned char *source, size_t length,
                          unsigned char **decoded_out, const char **format_out) {
    static const unsigned char magics[][4] = {
        {0xff, 0xd8, 0xff, 0x00}, {0x89, 'P', 'N', 'G'}, {'G', 'I', 'F', '8'},
        {'B', 'M', 0x00, 0x00}, {'R', 'I', 'F', 'F'}, {0x49, 0x49, 0x2a, 0x00},
    };
    static const int lengths[] = {3, 4, 4, 2, 4, 4};
    for (size_t item = 0; item < sizeof(lengths) / sizeof(lengths[0]); item++) {
        unsigned char key = source[0] ^ magics[item][0];
        int matches = 1;
        for (int i = 1; i < lengths[item]; i++) if ((source[i] ^ key) != magics[item][i]) matches = 0;
        if (!matches) continue;
        unsigned char *decoded = malloc(length);
        if (!decoded) return -1;
        for (size_t i = 0; i < length; i++) decoded[i] = source[i] ^ key;
        const char *format = format_for(decoded, length);
        if (format) {
            *decoded_out = decoded;
            *format_out = format;
            return 0;
        }
        free(decoded);
    }
    return -1;
}

static int decode_v1_v2(const unsigned char *source, size_t length,
                        unsigned char **decoded_out, size_t *decoded_length_out,
                        const char **format_out) {
    if (length < 31) return -1;
    int is_v1 = memcmp(source, V1_MAGIC, 6) == 0;
    int is_v2 = memcmp(source, V2_MAGIC, 6) == 0;
    if (!is_v1 && !is_v2) return -1;
    uint32_t aes_plain_size = little_u32(source + 6);
    uint32_t xor_size = little_u32(source + 10);
    size_t aes_cipher_size = aligned_aes_size(aes_plain_size);
    size_t offset = 15;
    if (aes_cipher_size > length || offset + aes_cipher_size > length || xor_size > length - offset - aes_cipher_size) return -1;
    unsigned char key[16];
    if (is_v1) {
        memcpy(key, "cfcd208495d565ef", 16);
    } else {
        if (isatty(STDIN_FILENO) || read_all_fd(STDIN_FILENO, key, sizeof(key)) != 0) return -2;
    }
    unsigned char *aes_plain = malloc(aes_cipher_size + 16);
    if (!aes_plain) {
        secure_zero(key, sizeof(key));
        return -1;
    }
    size_t aes_moved = 0;
    CCCryptorStatus status = CCCrypt(kCCDecrypt, kCCAlgorithmAES128,
                                     kCCOptionECBMode | kCCOptionPKCS7Padding,
                                     key, sizeof(key), NULL, source + offset, aes_cipher_size,
                                     aes_plain, aes_cipher_size + 16, &aes_moved);
    secure_zero(key, sizeof(key));
    if (status != kCCSuccess || aes_moved != aes_plain_size) {
        secure_zero(aes_plain, aes_cipher_size + 16);
        free(aes_plain);
        return -1;
    }
    const char *format = format_for(aes_plain, aes_moved);
    if (!format) {
        secure_zero(aes_plain, aes_cipher_size + 16);
        free(aes_plain);
        return -1;
    }
    offset += aes_cipher_size;
    size_t raw_size = length - offset - xor_size;
    unsigned char xor_key = 0;
    if (xor_size && infer_xor(format, source + offset + raw_size, xor_size, &xor_key) != 0) {
        secure_zero(aes_plain, aes_cipher_size + 16);
        free(aes_plain);
        return -1;
    }
    size_t output_size = aes_moved + raw_size + xor_size;
    unsigned char *output = malloc(output_size);
    if (!output) {
        secure_zero(aes_plain, aes_cipher_size + 16);
        free(aes_plain);
        return -1;
    }
    memcpy(output, aes_plain, aes_moved);
    memcpy(output + aes_moved, source + offset, raw_size);
    for (size_t i = 0; i < xor_size; i++) output[aes_moved + raw_size + i] = source[offset + raw_size + i] ^ xor_key;
    secure_zero(aes_plain, aes_cipher_size + 16);
    free(aes_plain);
    if (strcmp(format, "jpg") == 0 && output_size >= 2 &&
        (output[output_size - 2] != 0xff || output[output_size - 1] != 0xd9)) {
        secure_zero(output, output_size); free(output); return -1;
    }
    if (strcmp(format, "png") == 0 && !contains_iend(output, output_size)) {
        secure_zero(output, output_size); free(output); return -1;
    }
    *decoded_out = output;
    *decoded_length_out = output_size;
    *format_out = format;
    return 0;
}

static int self_test(void) {
    unsigned char old_plain[7] = {0xff, 0xd8, 0xff, 0xe0, 0x00, 0xff, 0xd9};
    unsigned char old_cipher[7];
    for (size_t i = 0; i < sizeof(old_plain); i++) old_cipher[i] = old_plain[i] ^ 0x88;
    unsigned char *old_decoded = NULL;
    const char *old_format = NULL;
    int old_result = decode_old_xor(old_cipher, sizeof(old_cipher), &old_decoded, &old_format);
    int old_ok = old_result == 0 && old_decoded && strcmp(old_format, "jpg") == 0 &&
                 memcmp(old_decoded, old_plain, sizeof(old_plain)) == 0;
    if (old_decoded) {
        secure_zero(old_decoded, sizeof(old_plain));
        free(old_decoded);
    }

    unsigned char v1_plain[16] = {
        0xff,0xd8,0xff,0xe0,0x00,0x10,'J','F','I','F',0x00,0x01,0x00,0x00,0xff,0xd9
    };
    unsigned char v1_cipher[32];
    size_t cipher_moved = 0;
    CCCryptorStatus crypt = CCCrypt(kCCEncrypt, kCCAlgorithmAES128,
        kCCOptionECBMode | kCCOptionPKCS7Padding, "cfcd208495d565ef", 16, NULL,
        v1_plain, sizeof(v1_plain), v1_cipher, sizeof(v1_cipher), &cipher_moved);
    unsigned char v1_source[47];
    memset(v1_source, 0, sizeof(v1_source));
    memcpy(v1_source, V1_MAGIC, sizeof(V1_MAGIC));
    v1_source[6] = 16;
    memcpy(v1_source + 15, v1_cipher, cipher_moved);
    unsigned char *v1_decoded = NULL;
    size_t v1_length = 0;
    const char *v1_format = NULL;
    int v1_result = decode_v1_v2(v1_source, sizeof(v1_source), &v1_decoded,
                                 &v1_length, &v1_format);
    int v1_ok = crypt == kCCSuccess && cipher_moved == 32 && v1_result == 0 &&
                v1_decoded && v1_length == sizeof(v1_plain) &&
                strcmp(v1_format, "jpg") == 0 &&
                memcmp(v1_decoded, v1_plain, sizeof(v1_plain)) == 0;
    if (v1_decoded) {
        secure_zero(v1_decoded, v1_length);
        free(v1_decoded);
    }
    secure_zero(v1_cipher, sizeof(v1_cipher));
    secure_zero(v1_source, sizeof(v1_source));
    if (!old_ok || !v1_ok) return 2;
    printf("{\"verdict\":\"pass\",\"checks\":2}\n");
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--self-test") == 0) return self_test();
    if (argc != 3) {
        fprintf(stderr, "usage_error\n");
        return 2;
    }
    int source_fd = open(argv[1], O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (source_fd < 0) { fprintf(stderr, "source_open_failed\n"); return 2; }
    struct stat info;
    if (fstat(source_fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_size <= 0 ||
        (uint64_t)info.st_size > MAX_IMAGE_BYTES) {
        close(source_fd); fprintf(stderr, "source_invalid\n"); return 2;
    }
    size_t length = (size_t)info.st_size;
    unsigned char *source = malloc(length);
    if (!source || read_all_fd(source_fd, source, length) != 0) {
        close(source_fd); free(source); fprintf(stderr, "source_read_failed\n"); return 2;
    }
    close(source_fd);
    unsigned char *decoded = NULL;
    size_t decoded_length = length;
    const char *format = NULL;
    int result;
    if (length >= 6 && (memcmp(source, V1_MAGIC, 6) == 0 || memcmp(source, V2_MAGIC, 6) == 0)) {
        result = decode_v1_v2(source, length, &decoded, &decoded_length, &format);
    } else {
        result = decode_old_xor(source, length, &decoded, &format);
    }
    secure_zero(source, length);
    free(source);
    if (result != 0 || !decoded || !format) {
        fprintf(stderr, result == -2 ? "image_key_unavailable\n" : "decode_failed\n");
        return result == -2 ? 3 : 2;
    }
    int output_fd = open(argv[2], O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (output_fd < 0 || write_all_fd(output_fd, decoded, decoded_length) != 0 || fsync(output_fd) != 0) {
        if (output_fd >= 0) close(output_fd);
        secure_zero(decoded, decoded_length); free(decoded);
        fprintf(stderr, "output_failed\n"); return 2;
    }
    close(output_fd);
    secure_zero(decoded, decoded_length);
    free(decoded);
    printf("{\"verdict\":\"pass\",\"format\":\"%s\",\"bytes\":%zu}\n", format, decoded_length);
    return 0;
}
