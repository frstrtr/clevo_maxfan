/*
 * clevo-fan-cli.c — minimal Clevo fan duty setter for headless servers.
 *
 * Stripped from SkyLandTW/clevo-indicator (GPL): keeps the EC port-IO
 * code, drops the GTK/AppIndicator GUI so it builds with no deps beyond
 * glibc on any Ubuntu / Debian. Run as root (writes raw IO ports).
 *
 * Usage:  clevo-fan-cli <60-100>     # set duty cycle %
 *         clevo-fan-cli read         # read current GPU/CPU temp + duty
 *
 * Reverse-engineered EC ports:
 *   0x66 = status / command
 *   0x62 = data
 *   0x99 = "set fan duty" command, fan_id 0x01 = combined (P775DM has 1 fan
 *           plane; some chassis have separate CPU/GPU planes 0x01/0x02)
 *   0xCE = "read current fan duty" register
 *   0x07 = CPU temp register
 *   0xCD = GPU temp register
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <sys/io.h>
#include <unistd.h>

#define EC_SC          0x66
#define EC_DATA        0x62
#define IBF            1
#define OBF            0
#define EC_SC_READ_CMD 0x80
#define EC_REG_CPU_TEMP 0x07
#define EC_REG_GPU_TEMP 0xCD
#define EC_REG_FAN_DUTY 0xCE

static int ec_init(void) {
    if (ioperm(EC_DATA, 1, 1) != 0) return -1;
    if (ioperm(EC_SC,   1, 1) != 0) return -1;
    return 0;
}

static int ec_io_wait(uint32_t port, uint32_t flag, char value) {
    uint8_t data = inb(port);
    int i = 0;
    while ((((data >> flag) & 0x1) != value) && (i++ < 100)) {
        usleep(1000);
        data = inb(port);
    }
    if (i >= 100) {
        fprintf(stderr, "ec_wait timeout port=0x%x data=0x%x\n", port, data);
        return -1;
    }
    return 0;
}

static uint8_t ec_io_read(uint32_t port) {
    ec_io_wait(EC_SC, IBF, 0);
    outb(EC_SC_READ_CMD, EC_SC);
    ec_io_wait(EC_SC, IBF, 0);
    outb(port, EC_DATA);
    ec_io_wait(EC_SC, OBF, 1);
    return inb(EC_DATA);
}

static int ec_io_do(uint32_t cmd, uint32_t port, uint8_t value) {
    ec_io_wait(EC_SC, IBF, 0);
    outb(cmd, EC_SC);
    ec_io_wait(EC_SC, IBF, 0);
    outb(port, EC_DATA);
    ec_io_wait(EC_SC, IBF, 0);
    outb(value, EC_DATA);
    return ec_io_wait(EC_SC, IBF, 0);
}

/* Set fan duty for one specific plane (0x01 = CPU fan, 0x02 = GPU fan on
 * dual-plane chassis like the P77xDM2-G). Single-plane chassis (e.g.
 * P775DM) only respond to 0x01; passing 0x02 there is harmless.
 */
static int set_fan_duty_plane(int pct, uint8_t fan_id) {
    if (pct < 60 || pct > 100) {
        fprintf(stderr, "fan duty out of range (allowed 60-100): %d\n", pct);
        return -1;
    }
    int raw = (int)((double)pct / 100.0 * 255.0);
    return ec_io_do(0x99, fan_id, raw);
}

/* Default behaviour: set BOTH fan planes (CPU + GPU). The heat pipes
 * between CPU and GPU heatsinks mean either fan can shed thermal load
 * from either component — running both at the same duty maximises that
 * coupling. On single-plane chassis the 0x02 write is a harmless no-op.
 */
static int set_fan_duty(int pct) {
    int r1 = set_fan_duty_plane(pct, 0x01);  /* CPU fan */
    int r2 = set_fan_duty_plane(pct, 0x02);  /* GPU fan */
    if (r1 != 0 || r2 != 0) {
        fprintf(stderr, "set_fan_duty: CPU plane=%d GPU plane=%d\n", r1, r2);
        return -1;
    }
    return 0;
}

int main(int argc, char** argv) {
    if (geteuid() != 0) {
        fprintf(stderr, "must run as root (uses inb/outb)\n");
        return 1;
    }
    if (ec_init() != 0) {
        fprintf(stderr, "ec_init failed: %s\n", strerror(errno));
        return 2;
    }
    if (argc < 2) {
        fprintf(stderr, "usage: %s <60-100|read>\n", argv[0]);
        return 1;
    }
    if (strcmp(argv[1], "read") == 0) {
        uint8_t cpu = ec_io_read(EC_REG_CPU_TEMP);
        uint8_t gpu = ec_io_read(EC_REG_GPU_TEMP);
        uint8_t duty = ec_io_read(EC_REG_FAN_DUTY);
        printf("cpu=%d°C gpu=%d°C fan_duty_raw=%d (%.0f%%)\n",
               cpu, gpu, duty, (double)duty / 255.0 * 100.0);
        return 0;
    }
    int pct = atoi(argv[1]);
    if (set_fan_duty(pct) != 0) {
        fprintf(stderr, "set_fan_duty failed\n");
        return 3;
    }
    printf("fan duty set to %d%%\n", pct);
    return 0;
}
