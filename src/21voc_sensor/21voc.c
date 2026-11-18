#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <errno.h>
#include <stdarg.h>
#include <signal.h>
#include <poll.h>
#include <time.h>
#include <sys/file.h>
#include <mosquitto.h>
#include "ini.h"

enum {
    LOG_INFO,
    LOG_WARN,
    LOG_ERROR,
    LOG_DEBUG
};

typedef struct {
    int   tvoc;
    int   hcho;
    int   eco2;
    float temp;
    float humi;
} voc_data_t;

typedef struct {
    char serial_dev[64];
    int  serial_baud;
    char mqtt_host[64];
    int  mqtt_port;
    char mqtt_user[64];
    char mqtt_pass[64];
    int  mqtt_keepalive;
    char node_id[64];
    char state_topic[128];
    char discovery_prefix[64];
    int  publish_interval;
    char pid_file[128];
} config_t;

#define LOG_MSG(log_level, fmt, ...) \
    log_msg(log_level, "[21voc] [func]: %s, [line]: %d: " fmt, __func__, __LINE__, ##__VA_ARGS__)

#define CONF_FILE "21voc.conf"

static volatile sig_atomic_t g_running = 1;
static volatile sig_atomic_t g_received_signal = 0;
static int g_lock_fd = -1;

struct {
    char log_file[32];
    FILE *log_ptr;
    uint8_t log_level;
} g_log_cfg = { 0 };

char *g_log_string[] = {
    [LOG_INFO]  = "INFO",
    [LOG_WARN]  = "WARN",
    [LOG_ERROR] = "ERROR",
    [LOG_DEBUG] = "DEBUG"
};

static inline void log_msg(uint8_t log_level, char *fmt, ...)
{
    if (!g_log_cfg.log_ptr)  {
        printf("log_ptr is a NULL pointer\n");
        return;
    }

    if (log_level > g_log_cfg.log_level) {
        return;
    }

    fprintf(g_log_cfg.log_ptr, "[%s] ", g_log_string[log_level]);

    va_list args;
    va_start(args, fmt);
    vfprintf(g_log_cfg.log_ptr, fmt, args);
    va_end(args);
}

static int log_init(char *log_file)
{
    if (!log_file || !log_file[0]) {
        g_log_cfg.log_ptr = stdout;
    } else {
        g_log_cfg.log_ptr = fopen(log_file, "a");
        if (!(g_log_cfg.log_ptr)) {
            fprintf(stderr, "open log file %s failed: %s\n", log_file, strerror(errno));
            return -1;
        }
    }

    //block buffer -> line buffer
    setvbuf(g_log_cfg.log_ptr, NULL, _IOLBF, 0);
    fprintf(stdout, "log output to %s\n", g_log_cfg.log_ptr == stdout ? "stdout" : log_file);
    fprintf(g_log_cfg.log_ptr, "[21voc] start logging at %s\n", ctime(&(time_t){time(NULL)}));
    return 0;
}

// nevner call stdio in signal handler, use async-signal-safe functions instead
static void on_signal(int sig)
{
    g_running = 0;
    g_received_signal = sig;
}

static int install_signal_handlers(void)
{
    struct sigaction sa;

    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;//不设置 SA_RESTART

    if (sigaction(SIGINT,  &sa, NULL) != 0) return -1;
    if (sigaction(SIGTERM, &sa, NULL) != 0) return -1;

    signal(SIGPIPE, SIG_IGN);       /* 避免 MQTT 断开时被 SIGPIPE 杀掉 */
    return 0;
}

static void config_defaults(config_t *c)
{
    g_log_cfg.log_level = LOG_DEBUG;
    strncpy(c->serial_dev, "/dev/ttyS1", sizeof(c->serial_dev) - 1);
    c->serial_baud = B9600;
    strncpy(c->mqtt_host, "127.0.0.1", sizeof(c->mqtt_host) - 1);
    c->mqtt_port = 1883;
    c->mqtt_keepalive = 30;
    strncpy(c->node_id, "21voc_m2", sizeof(c->node_id) - 1);
    strncpy(c->state_topic, "home/21voc/state", sizeof(c->state_topic) - 1);
    strncpy(c->discovery_prefix, "homeassistant", sizeof(c->discovery_prefix) - 1);
    c->publish_interval = 5;
    strncpy(c->pid_file, "/var/run/21voc.pid", sizeof(c->pid_file) - 1);
}

//match callback
static int config_ini_handler(void *user, const char *section, const char *name, const char *value)
{
    config_t *c = (config_t *)user;

    #define MATCH(s, n) (strcmp(section, s) == 0 && strcmp(name, n) == 0)

    if (MATCH("mqtt", "host"))
        strncpy(c->mqtt_host, value, sizeof(c->mqtt_host) - 1);
    else if (MATCH("mqtt", "port"))
        c->mqtt_port = atoi(value);
    else if (MATCH("mqtt", "user"))
        strncpy(c->mqtt_user, value, sizeof(c->mqtt_user) - 1);
    else if (MATCH("mqtt", "password"))
        strncpy(c->mqtt_pass, value, sizeof(c->mqtt_pass) - 1);
    else if (MATCH("mqtt", "keepalive"))
        c->mqtt_keepalive = atoi(value);
    else if (MATCH("device", "node_id"))
        strncpy(c->node_id, value, sizeof(c->node_id) - 1);
    else if (MATCH("device", "state_topic"))
        strncpy(c->state_topic, value, sizeof(c->state_topic) - 1);
    else if (MATCH("device", "discovery_prefix"))
        strncpy(c->discovery_prefix, value, sizeof(c->discovery_prefix) - 1);
    else if (MATCH("device", "publish_interval"))
        c->publish_interval = atoi(value);
    else if (MATCH("device", "pid_file")) {
        strncpy(c->pid_file, value, sizeof(c->pid_file) - 1);
        c->pid_file[sizeof(c->pid_file) - 1] = '\0';
    }
    else if (MATCH("log", "log_file")) {
        strncpy(g_log_cfg.log_file, value, sizeof(g_log_cfg.log_file) - 1);
        g_log_cfg.log_file[sizeof(g_log_cfg.log_file) - 1] = '\0';
    }
    else if (MATCH("log", "log_level"))
        g_log_cfg.log_level = atoi(value);

    return 1;
    #undef MATCH
}

static int config_load(char *path, config_t *c)
{
    config_defaults(c);
    int rc = ini_parse(path, config_ini_handler, c);
    if (rc < 0) {
        fprintf(stderr, "open conf file failed, %s\n", path);
        return -1;
    }
    if (rc > 0) {
        fprintf(stderr, "syntax error at file: %s, line:%d\n", path, rc);
        return -1;
    }

    if (strlen(c->mqtt_user) == 0 || strlen(c->mqtt_pass) == 0) {
        fprintf(stderr, "username or password not configured for mqtt\n");
        return -1;
    }

    return 0;
}

static int open_serial(char *dev, int baud)
{
    struct termios tty;

    int fd = open(dev, O_RDWR | O_NOCTTY);
    if (fd < 0) {
        LOG_MSG(LOG_ERROR, "open serial failed %s, errno: %d\n", dev, errno);
        return -1;
    }

    if (tcgetattr(fd, &tty)) {
        LOG_MSG(LOG_ERROR, "tcgetattr failed for %s, errno: %d\n", dev, errno);
        close(fd);
        return -1;
    }

    cfmakeraw(&tty);
    cfsetispeed(&tty, baud);
    cfsetospeed(&tty, baud);

    tty.c_cflag |= (CLOCAL | CREAD);
    tty.c_cflag &= ~PARENB;
    tty.c_cflag &= ~CSTOPB;
    tty.c_cflag &= ~CSIZE;
    tty.c_cflag |= CS8;
    tty.c_cc[VMIN]  = 1;
    tty.c_cc[VTIME] = 2;

    if (tcsetattr(fd, TCSANOW, &tty)) {
        LOG_MSG(LOG_ERROR, "tcsetattr failed for %s, errno: %d\n", dev, errno);
        close(fd);
        return -1;
    }

    return fd;
}

static int wait_readable(int fd, int timeout_ms)
{
    struct pollfd pfd = { 0 };
    pfd.fd = fd;
    pfd.events = POLLIN;

    int rc = poll(&pfd, 1, timeout_ms);
    if (rc < 0)
        return (errno == EINTR) ? 0 : -1;
    if (rc == 0)
        return 0;//timeout
    if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))
        return -1;
    return 1;
}

static int read_frame(int fd, uint8_t *buf)
{
    uint8_t b = 0, got = 0;
    int ready;

    // wait frame header 0x2C
    for (;;) {
        if (!g_running) return -1;

        ready = wait_readable(fd, 500);
        if (ready < 0) return -1;
        if (ready == 0) continue;

        ssize_t n = read(fd, &b, 1);
        if (n == 0) continue;
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN) continue;
            return -1;
        }
        if (b == 0x2C) { buf[0] = 0x2C; break; }
    }

    got = 1;
    while (got < 12) {
        if (!g_running) return -1;

        ready = wait_readable(fd, 500);
        if (ready < 0) return -1;
        if (ready == 0) continue;

        ssize_t n = read(fd, buf + got, 12 - got);
        if (n <= 0) {
            if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
            return -1;
        }
        got += n;
    }
    return 0;
}

static int parse_frame(uint8_t *d, voc_data_t *out)
{
    uint8_t sum = 0;
    for (int i = 0; i < 11; i++) sum += d[i];
    uint8_t cs = (uint8_t)(0x100 - sum);   /* 等价于 (~sum + 1) & 0xFF */
    if (cs != d[11]) return -1;

    out->tvoc = (d[1] << 8) | d[2];
    out->hcho = (d[3] << 8) | d[4];
    out->eco2 = (d[5] << 8) | d[6];

    uint16_t t_raw = ((uint16_t)d[7] << 8) | d[8];
    if (t_raw & 0x8000)
        out->temp = -((0xFFFF - t_raw) * 0.1f);
    else
        out->temp = t_raw * 0.1f;

    uint16_t h_raw = ((uint16_t)d[9] << 8) | d[10];
    out->humi = h_raw * 0.1f;

    return 0;
}


static void publish_discovery(struct mosquitto *mosq, config_t *cfg)
{
    char device_json[512] = { 0 };
    snprintf(device_json, sizeof(device_json),
        "\"device\":{"
        "\"identifiers\":[\"" "%s" "\"],"
        "\"name\":\"21VOC 传感器\","
        "\"manufacturer\":\"传苣\","
        "\"model\":\"21VOC 五合一\""
        "}", cfg->node_id);

    struct {
        char *obj;
        char *name;
        char *tpl;
        char *unit;
        char *dev_class;
    } sensors[] = {
        { "tvoc", "TVOC",   "{{ value_json.tvoc }}", "µg/m³", NULL },
        { "hcho", "甲醛",   "{{ value_json.hcho }}", "µg/m³", NULL },
        { "eco2", "eCO2",   "{{ value_json.eco2 }}", "ppm", NULL },
        { "temp", "温度",   "{{ value_json.temp }}", "°C", "temperature" },
        { "humi", "湿度",   "{{ value_json.humi }}", "%", "humidity" },
    };

    char topic[128], payload[768];
    for (size_t i = 0; i < sizeof(sensors)/sizeof(sensors[0]); i++) {
        snprintf(topic, sizeof(topic),
                 "%s" "/sensor/" "%s" "/%s/config", cfg->discovery_prefix, cfg->node_id, sensors[i].obj);

        int n = snprintf(payload, sizeof(payload),
            "{"
            "\"name\":\"%s\","
            "\"unique_id\":\""  "%s_%s\","
            "\"state_topic\":\""  "%s" "\","
            "\"value_template\":\"%s\","
            "\"unit_of_measurement\":\"%s\","
            "\"state_class\": \"measurement\",",
            sensors[i].name, cfg->node_id, sensors[i].obj, cfg->state_topic, sensors[i].tpl, sensors[i].unit);

        if (sensors[i].dev_class)
            n += snprintf(payload + n, sizeof(payload) - n,
                          "\"device_class\":\"%s\",", sensors[i].dev_class);

        snprintf(payload + n, sizeof(payload) - n, "%s}", device_json);

        mosquitto_publish(mosq, NULL, topic, strlen(payload), payload, 0, true);
        LOG_MSG(LOG_INFO, "[discovery] %s\n", topic);
    }
}

static int single_instance_check(char *pid_file)
{
    int old_pid = 0;
    char buf[32] = { 0 };

    int fd = open(pid_file, O_RDWR | O_CREAT, 0644);
    if (fd < 0) {
        fprintf(stderr, "open pid file %s failed: %s\n", pid_file, strerror(errno));
        return 0;
    }

    if (flock(fd, LOCK_EX | LOCK_NB)) {//acquire lock failed
        (void)!pread(fd, buf, sizeof(buf) - 1, 0);// without change offset
        old_pid = atoi(buf);
        fprintf(stderr, "another instance already running (pid=%d), this startup is rejected\n", old_pid);
        close(fd);
        return -1;
    }

    if (ftruncate(fd, 0) == 0)// record current pid
        (void)!dprintf(fd, "%d\n", getpid());
    g_lock_fd = fd;// must keep opend to maintain the lock
    return 0;
}

int main(void)
{
    uint8_t buf[12];
    voc_data_t d;
    time_t last = 0;
    int serial_fd = 0;
    config_t cfg = { 0 };

    if (config_load(CONF_FILE, &cfg)) {
        fprintf(stderr, "Failed to load 21voc.conf\n");
        return 1;
    }

    if (single_instance_check(cfg.pid_file)) {
        return 1;
    }

    if (log_init(g_log_cfg.log_file)) {
        fprintf(stderr, "log init failed\n");
        return 1;
    }

    if (install_signal_handlers()) {
        LOG_MSG(LOG_ERROR, "install signal handlers failed, error: %s\n", strerror(errno));
        return 1;
    }

    serial_fd = open_serial(cfg.serial_dev, cfg.serial_baud);
    if (serial_fd < 0)
        return 1;

    mosquitto_lib_init();
    struct mosquitto *mosq = mosquitto_new(NULL, true, NULL);
    if (!mosq) {
        LOG_MSG(LOG_ERROR, "mosquitto_new failed\n");
        return 1;
    }

    mosquitto_username_pw_set(mosq, cfg.mqtt_user, cfg.mqtt_pass);
    if (mosquitto_connect(mosq, cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_keepalive) != MOSQ_ERR_SUCCESS) {
        LOG_MSG(LOG_ERROR, "mqtt connect failed\n");
        return 1;
    }
    mosquitto_loop_start(mosq);

    publish_discovery(mosq, &cfg);
    usleep(500 * 1000);

    while (g_running) {
        if (read_frame(serial_fd, buf)) {
            if (!g_running) break;
            usleep(100 * 1000);
            continue;
        }
        if (parse_frame(buf, &d) != 0) continue;

        time_t now = time(NULL);
        if (now - last < cfg.publish_interval) continue;
        last = now;

        char state[192];
        snprintf(state, sizeof(state),
                 "{\"tvoc\":%d,\"hcho\":%d,\"eco2\":%d,\"temp\":%.1f,\"humi\":%.1f}",
                 d.tvoc, d.hcho, d.eco2, d.temp, d.humi);

        mosquitto_publish(mosq, NULL, cfg.state_topic, strlen(state), state, 0, true);
        LOG_MSG(LOG_INFO, "[state] %s\n", state);
    }

    if (g_received_signal == SIGINT)
        LOG_MSG(LOG_INFO, "[signal] Received signal SIGINT, exiting...\n");
    else if (g_received_signal == SIGTERM)
        LOG_MSG(LOG_INFO, "[signal] Received signal SIGTERM, exiting...\n");
    else
        LOG_MSG(LOG_INFO, "[signal] Received signal %d, exiting...\n", g_received_signal);

    mosquitto_loop_stop(mosq, true);
    mosquitto_destroy(mosq);
    mosquitto_lib_cleanup();
    close(serial_fd);

    if (g_log_cfg.log_ptr && g_log_cfg.log_ptr != stdout)
        fclose(g_log_cfg.log_ptr);
    if (g_lock_fd >= 0)
        close(g_lock_fd);

    return 0;
}