// SPDX-License-Identifier: GPL-3.0-or-later
// Command-line test rig for the macOS USB accessory layer. Not shipped in the app.
//
//   aoa_probe list                 show USB devices, marking likely Android phones
//   aoa_probe run [location-hex]   switch a phone into accessory mode, open it, and talk to Login Relay:
//                                  prints every line the phone sends, pings it every 2 s, and sends
//                                  what you type: "callback <url>", "done", or any raw JSON line.
#include "../Sources/aoa_mac.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static double t0;
#define LOG(...) do { printf("[%6.2fs] ", now() - t0); printf(__VA_ARGS__); printf("\n"); fflush(stdout); } while (0)

static void send_line(aoa_conn *c, const char *json) {
    size_t n = strlen(json);
    uint8_t *buf = malloc(n + 2);
    memcpy(buf, json, n);
    buf[n++] = '\n';
    if (n % 64 == 0) buf[n++] = '\n';  // avoid a transfer that ends without a short packet
    int r = aoa_write(c, buf, (uint32_t)n, 1000);
    if (r <= 0) LOG("write returned %d (iokit 0x%x)", r, aoa_last_iokit_error());
    free(buf);
}

static int cmd_list(void) {
    aoa_info info[64];
    int n = aoa_list(info, 64);
    printf("%-10s %-10s %-10s %-4s %s\n", "location", "vid:pid", "android?", "aoa", "name / serial");
    for (int i = 0; i < n; i++) {
        printf("0x%08x %04x:%04x   %-8s   %-3s  %s%s%s\n", info[i].location_id, info[i].vendor_id, info[i].product_id,
               info[i].looks_android ? "likely" : "-", info[i].is_accessory ? "yes" : "-", info[i].name,
               info[i].serial[0] ? " / " : "", info[i].serial);
    }
    return 0;
}

static int find_phone(uint32_t wanted, int have_wanted, aoa_info *out) {
    aoa_info info[64];
    int n = aoa_list(info, 64), matches = 0;
    for (int i = 0; i < n; i++) {
        int ok = have_wanted ? info[i].location_id == wanted : info[i].looks_android;
        if (ok) {
            if (matches == 0) *out = info[i];
            matches++;
        }
    }
    return matches;
}

static int cmd_run(int argc, char **argv) {
    uint32_t wanted = 0;
    int have_wanted = 0;
    if (argc > 2) {
        wanted = (uint32_t)strtoul(argv[2], NULL, 16);
        have_wanted = 1;
    }
    aoa_info phone;
    int matches = find_phone(wanted, have_wanted, &phone);
    if (matches == 0) {
        LOG("No candidate phone. Plug it in and pick \"File transfer\" in its USB notification, or run `list`.");
        return 1;
    }
    if (matches > 1) {
        LOG("%d candidates; give a location from `list`.", matches);
        return 1;
    }
    LOG("Phone: %s  %04x:%04x at 0x%08x%s", phone.name, phone.vendor_id, phone.product_id, phone.location_id,
        phone.is_accessory ? " (already in accessory mode)" : "");

    if (!phone.is_accessory) {
        char host[128] = "probe";
        gethostname(host, sizeof host);
        const char *strings[6] = {"Login Relay", "Desktop", host, "1", "https://github.com/clippycoder/login-relay", "probe"};
        int proto = 0;
        double s = now();
        int r = aoa_switch(phone.location_id, strings, &proto);
        LOG("switch -> %d (AOA protocol %d, iokit 0x%x) in %.2fs", r, proto, aoa_last_iokit_error(), now() - s);
        if (r != AOA_OK) return 1;
        int appeared = 0;
        for (int i = 0; i < 60; i++) {  // up to 12 s: Android gives up after 10
            usleep(200 * 1000);
            aoa_info now_info;
            if (find_phone(phone.location_id, 1, &now_info) && now_info.is_accessory) {
                LOG("accessory device appeared: %04x:%04x after %.2fs", now_info.vendor_id, now_info.product_id,
                    now() - s);
                appeared = 1;
                break;
            }
        }
        if (!appeared) {
            LOG("the phone never reappeared as an accessory");
            return 1;
        }
    }

    aoa_conn *c = NULL;
    int err = 0;
    for (int attempt = 0; attempt < 6 && !c; attempt++) {
        c = aoa_open(phone.location_id, &err);
        if (!c) {
            LOG("open attempt %d failed (%d, iokit 0x%x)", attempt + 1, err, aoa_last_iokit_error());
            usleep(500 * 1000);
        }
    }
    if (!c) return 1;
    LOG("opened. Waiting for Login Relay on the phone (it launches by itself; the first time Android asks to allow it).");
    LOG("type: callback <url> | done | <raw json>");

    uint8_t buf[16384];
    char line[65536];
    size_t len = 0;
    double last_ping = 0;
    for (;;) {
        int n = aoa_read(c, buf, sizeof buf, 200);
        if (n < 0) {
            LOG("read error %d (iokit 0x%x): the phone was unplugged or the link broke", n, aoa_last_iokit_error());
            break;
        }
        for (int i = 0; i < n; i++) {
            if (buf[i] == '\n') {
                if (len) {
                    line[len] = 0;
                    LOG("phone: %s", line);
                    len = 0;
                }
            } else if (len < sizeof line - 1) {
                line[len++] = (char)buf[i];
            }
        }
        if (now() - last_ping > 2) {
            send_line(c, "{\"type\":\"ping\"}");
            last_ping = now();
        }
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(0, &fds);
        struct timeval tv = {0, 0};
        if (select(1, &fds, NULL, NULL, &tv) > 0) {
            char in[8192];
            if (!fgets(in, sizeof in, stdin)) break;
            in[strcspn(in, "\r\n")] = 0;
            if (!strncmp(in, "callback ", 9)) {
                char json[9000];
                char esc[8192];
                size_t k = 0;
                for (const char *p = in + 9; *p && k < sizeof esc - 2; p++) {
                    if (*p == '"' || *p == '\\') esc[k++] = '\\';
                    esc[k++] = *p;
                }
                esc[k] = 0;
                snprintf(json, sizeof json, "{\"type\":\"callback\",\"url\":\"%s\"}", esc);
                send_line(c, json);
            } else if (!strcmp(in, "done")) {
                send_line(c, "{\"type\":\"done\"}");
            } else if (in[0]) {
                send_line(c, in);
            }
        }
    }
    aoa_close(c);
    return 0;
}

int main(int argc, char **argv) {
    t0 = now();
    if (argc > 1 && !strcmp(argv[1], "list")) return cmd_list();
    if (argc > 1 && !strcmp(argv[1], "run")) return cmd_run(argc, argv);
    fprintf(stderr, "usage: aoa_probe list | run [location-hex]\n");
    return 2;
}
