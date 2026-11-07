/* Test-only: probe transport, with one fixed, inert XKCNSU echo fixture. */
#include <errno.h>
#include <elf.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (getuid() != 0 || argc != 2)
        return 2;
    uint32_t command;
    const char *path = NULL;
    const char *arguments = NULL;
    void *image = NULL;
    size_t image_length = 0;
    unsigned char invalid_image[] = { 0x7f, 'E', 'L', 'F' };
    printf("{\"stage\":\"started\",\"operation\":\"%s\"}\n", argv[1]);
    fflush(stdout);
    if (!strcmp(argv[1], "hello")) {
        command = 0x1000;
    } else if (!strcmp(argv[1], "null-load")) {
        command = 0x1020;
    } else if (!strcmp(argv[1], "missing-load")) {
        command = 0x1020;
        path = "/data/local/tmp/XKCNSU-file-that-does-not-exist.kpm";
        arguments = "XKCNSU-transport-check";
    } else if (!strcmp(argv[1], "buffer-null") || !strcmp(argv[1], "buffer-invalid-elf")) {
        command = 0x1023;
        if (!strcmp(argv[1], "buffer-invalid-elf")) {
            image = invalid_image;
            image_length = sizeof(invalid_image);
        }
    } else if (!strcmp(argv[1], "fixture-load") || !strcmp(argv[1], "fixture-buffer-load") ||
               !strcmp(argv[1], "fixture-buffer-nullargs") ||
               !strcmp(argv[1], "fixture-bad-magic") || !strcmp(argv[1], "fixture-bad-symbol") ||
               !strcmp(argv[1], "fixture-bad-callback") ||
               !strcmp(argv[1], "fixture-read")) {
        command = !strcmp(argv[1], "fixture-load") ? 0x1020 : 0x1023;
        path = "/data/adb/ksu/XKCNSU/kpm/"
               "940d22509b5cb6acadc5f4fb7a208a152d14cdc5eacd0eb5b597ae6a6b481638.kpm";
        arguments = "XKCNSU-native-fixture-check";
        if (!strcmp(argv[1], "fixture-buffer-nullargs"))
            arguments = NULL;
        if (command == 0x1023) {
            puts("{\"stage\":\"file-open-start\"}");
            fflush(stdout);
            FILE *input = fopen(path, "rb");
            if (!input) {
                printf("{\"stage\":\"file-open-error\",\"errno\":%d}\n", errno);
                return 3;
            }
            puts("{\"stage\":\"file-open-returned\"}");
            fflush(stdout);
            if (fseek(input, 0, SEEK_END))
                return 3;
            long length = ftell(input);
            if (length <= 0 || length > (32L << 20) || fseek(input, 0, SEEK_SET))
                return 3;
            image_length = (size_t)length;
            image = malloc(image_length);
            if (!image || fread(image, 1, image_length, input) != image_length)
                return 3;
            fclose(input);
            printf("{\"stage\":\"file-read-returned\",\"bytes\":%zu}\n", image_length);
            fflush(stdout);
            if (!strcmp(argv[1], "fixture-read")) {
                free(image);
                puts("{\"stage\":\"returned\",\"result\":0,\"errno\":0}");
                return 0;
            }
            if (!strcmp(argv[1], "fixture-bad-magic")) {
                ((unsigned char *)image)[0] = 0;
            } else if (!strcmp(argv[1], "fixture-bad-symbol") ||
                       !strcmp(argv[1], "fixture-bad-callback")) {
                Elf64_Ehdr *header = image;
                if (image_length < sizeof(*header) || header->e_shentsize != sizeof(Elf64_Shdr) ||
                    header->e_shoff > image_length ||
                    header->e_shnum > (image_length - header->e_shoff) / sizeof(Elf64_Shdr) ||
                    header->e_shstrndx >= header->e_shnum)
                    return 4;
                Elf64_Shdr *sections = (void *)((char *)image + header->e_shoff);
                int changed = 0;
                for (unsigned int i = 1; i < header->e_shnum; i++) {
                    if (!strcmp(argv[1], "fixture-bad-symbol") &&
                        sections[i].sh_type == SHT_SYMTAB && sections[i].sh_link < header->e_shnum &&
                        sections[i].sh_offset <= image_length - sizeof(Elf64_Sym)) {
                        Elf64_Sym *symbol = (void *)((char *)image + sections[i].sh_offset);
                        symbol->st_name = sections[sections[i].sh_link].sh_size;
                        changed = 1;
                        break;
                    }
                    Elf64_Shdr *strings = &sections[header->e_shstrndx];
                    if (!strcmp(argv[1], "fixture-bad-callback") &&
                        strings->sh_offset < image_length &&
                        strings->sh_size <= image_length - strings->sh_offset &&
                        sections[i].sh_name < strings->sh_size) {
                        const char *name = (char *)image + strings->sh_offset + sections[i].sh_name;
                        if (memchr(name, 0, strings->sh_size - sections[i].sh_name) &&
                            !strcmp(name, ".kpm.init")) {
                            sections[i].sh_size = 16;
                            changed = 1;
                            break;
                        }
                    }
                }
                if (!changed)
                    return 4;
            }
        }
    } else {
        return 2;
    }
    uint64_t word = (UINT64_C(0x0d05) << 32) | (UINT64_C(0x2026) << 16) | command;
    printf("{\"stage\":\"before\",\"operation\":\"%s\"}\n", argv[1]);
    fflush(stdout);
    errno = 0;
    long result = command == 0x1023 ?
        syscall(45, 0UL, word, image, image_length, arguments, 0UL) :
        syscall(45, 0UL, word, path, arguments, 0UL, 0UL);
    printf("{\"stage\":\"returned\",\"result\":%ld,\"errno\":%d}\n", result, errno);
    if (image != invalid_image)
        free(image);
    return 0;
}
