#include <ctype.h>
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>

#if defined(CONFIG_FIDO2)
#include <zephyr/authentication/fido2/fido2.h>
#endif

#if defined(CONFIG_BENCH_SENSOR)
#include <zephyr/drivers/sensor.h>
#endif

#include "bench.h"
#include "bench_clock.h"
#include "stats.h"

LOG_MODULE_REGISTER(bench, LOG_LEVEL_INF);

#if defined(CONFIG_BENCH_SENSOR)
static const struct device *const sensor = DEVICE_DT_GET(DT_ALIAS(bench_sensor));
#endif

#if DT_NODE_HAS_STATUS(DT_ALIAS(led0), okay) && DT_NODE_HAS_PROP(DT_ALIAS(led0), gpios)
#define HAVE_ALARM_LED 1
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static bool led_ready;
#endif

BUILD_ASSERT(CONFIG_BENCH_DEADLINE_MS <= CONFIG_BENCH_PERIOD_MS,
	     "This experiment assumes a deadline no longer than the period");

static const struct device *const control_uart = DEVICE_DT_GET(DT_CHOSEN(bench_control_uart));

K_MSGQ_DEFINE(rx_bytes, 1, 512, 1);
K_SEM_DEFINE(run_start, 0, 1);
K_SEM_DEFINE(run_done, 1, 1);
K_SEM_DEFINE(tx_done, 0, 1);

static atomic_t running;
static atomic_t rx_overflows;
static atomic_t tx_timeouts;
static atomic_t threshold = ATOMIC_INIT(30000);
static atomic_t sample_value;
static atomic_t filtered_value;
static atomic_t alarm_value;
static atomic_t generation;
/* Prevent optimization of the synthetic workload. */
static volatile uint32_t work_sink;
static uint32_t work_rounds = CONFIG_BENCH_WORK_ROUNDS;
static struct bench_stats stats;
static uint64_t run_begin;
static uint64_t run_end;
static uint64_t run_finished;
static uint64_t run_duration_ticks;
static uint32_t run_id;
static bool run_complete;
static int fido_error;
static int usb_error;
static int sensor_ready_error;
static char last_request[65];
static char reply[4096];
static size_t reply_len;
static bool reply_overflow;
static uint8_t tx_frame[4128];
static size_t tx_offset;
static size_t tx_length;
#if defined(CONFIG_BENCH_CONTROL_CDC_ACM)
K_MUTEX_DEFINE(tx_mutex);
#else
static struct k_spinlock tx_spinlock;
#endif
static k_tid_t workload_tid;

static k_spinlock_key_t tx_lock(void)
{
#if defined(CONFIG_BENCH_CONTROL_CDC_ACM)
	/* The CDC callback runs on a workqueue and may wait for the UDC mutex. */
	k_mutex_lock(&tx_mutex, K_FOREVER);
	return (k_spinlock_key_t){0};
#else
	return k_spin_lock(&tx_spinlock);
#endif
}

static void tx_unlock(k_spinlock_key_t key)
{
#if defined(CONFIG_BENCH_CONTROL_CDC_ACM)
	ARG_UNUSED(key);
	k_mutex_unlock(&tx_mutex);
#else
	k_spin_unlock(&tx_spinlock, key);
#endif
}

static bool safe_identifier(const char *s)
{
	if (!*s || strlen(s) > 63) {
		return false;
	}
	for (; *s; s++) {
		if (!isalnum((unsigned char)*s) && *s != '-' && *s != '_') {
			return false;
		}
	}
	return true;
}

static void reply_append(const char *fmt, ...)
{
	va_list args;
	int len;

	if (reply_overflow) {
		return;
	}

	va_start(args, fmt);
	len = vsnprintf(reply + reply_len, sizeof(reply) - reply_len, fmt, args);
	va_end(args);

	if (len < 0 || (size_t)len >= sizeof(reply) - reply_len) {
		reply_overflow = true;
	} else {
		reply_len += (size_t)len;
	}
}

static void send_reply(unsigned int id)
{
	const char *text =
		reply_overflow ? "{\"ok\":false,\"error\":\"response_overflow\"}" : reply;
	k_spinlock_key_t key = tx_lock();
	int n = snprintf((char *)tx_frame, sizeof(tx_frame), "\n@%u %s\n", id, text);

	if (n < 0 || (size_t)n >= sizeof(tx_frame)) {
		tx_unlock(key);
		return;
	}
	k_sem_reset(&tx_done);
	tx_offset = 0;
	tx_length = (size_t)n;
	uart_irq_tx_enable(control_uart);
	tx_unlock(key);

	/* The UART callback continues partial FIFO writes. */
	if (k_sem_take(&tx_done, K_SECONDS(3))) {
		key = tx_lock();
		uart_irq_tx_disable(control_uart);
		tx_length = 0;
		tx_unlock(key);
		atomic_inc(&tx_timeouts);
	}
}

static void uart_irq(const struct device *dev, void *unused)
{
	uint8_t data[16];

	ARG_UNUSED(unused);

	uart_irq_update(dev);

	while (uart_irq_rx_ready(dev)) {
		int n = uart_fifo_read(dev, data, sizeof(data));

		if (n <= 0) {
			break;
		}
		for (int i = 0; i < n; i++) {
			if (k_msgq_put(&rx_bytes, &data[i], K_NO_WAIT)) {
				atomic_inc(&rx_overflows);
			}
		}
	}
	if (uart_irq_tx_ready(dev)) {
		bool done = false;
		k_spinlock_key_t key = tx_lock();

		if (tx_offset < tx_length) {
			int accepted =
				uart_fifo_fill(dev, tx_frame + tx_offset, tx_length - tx_offset);

			if (accepted > 0) {
				tx_offset += (size_t)accepted;
			}
		}
		if (tx_offset >= tx_length) {
			uart_irq_tx_disable(dev);
			done = tx_length != 0;
			tx_length = 0;
		}
		if (done) {
			k_sem_give(&tx_done);
		}
		tx_unlock(key);
	}
}

static int read_sample(uint64_t index, int32_t *value)
{
#if defined(CONFIG_BENCH_SENSOR)
	struct sensor_value temp;
	int ret = sensor_sample_fetch(sensor);

	ARG_UNUSED(index);

	if (!ret) {
		ret = sensor_channel_get(sensor, SENSOR_CHAN_AMBIENT_TEMP, &temp);
	}
	if (ret) {
		return ret;
	}
	*value = temp.val1 * 1000 + temp.val2 / 1000;
#else
	/* Triangular temperature trace: 20..40 degrees C. */
	uint32_t phase = index % 400;

	*value = 20000 + (phase <= 200 ? phase : 400 - phase) * 100;
#endif
	return 0;
}

static void workload(void *a, void *b, void *c)
{
	const uint64_t period = k_ms_to_ticks_ceil64(CONFIG_BENCH_PERIOD_MS);
	const uint64_t deadline =
		bench_clock_from_ticks(k_ms_to_ticks_ceil64(CONFIG_BENCH_DEADLINE_MS));

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		uint64_t due;
		int32_t filtered = 25000;
		uint32_t mixed = 0x9e3779b9U;
		uint64_t index = 0;

		k_sem_take(&run_start, K_FOREVER);
		due = run_begin;

		while (atomic_get(&running) && due < run_end) {
			uint64_t start;
			uint64_t finish;
			int32_t value = filtered;
			bool alarm;
			int ret;

			k_sleep(K_TIMEOUT_ABS_TICKS(due));
			if (!atomic_get(&running)) {
				break;
			}

			start = bench_clock_now();
			ret = read_sample(index, &value);
			if (ret) {
				stats.sensor_errors++;
			} else {
				filtered = (7 * filtered + value) / 8;
			}
			for (unsigned int i = 0; i < work_rounds; i++) {
				mixed ^= mixed << 13;
				mixed ^= mixed >> 17;
				mixed ^= mixed << 5;
			}
			work_sink = mixed;
			atomic_set(&sample_value, value);
			atomic_set(&filtered_value, filtered);
			alarm = filtered >= atomic_get(&threshold);
			atomic_set(&alarm_value, alarm);
#ifdef HAVE_ALARM_LED
			if (led_ready) {
				gpio_pin_set_dt(&led, alarm);
			}
#endif
			finish = bench_clock_now();
			bench_stats_add(&stats, bench_clock_from_ticks(due), start, finish,
					deadline, bench_hist_bin_units());
			due += period;
			index++;
			if (due < run_end) {
				uint64_t skipped = bench_skip_count(due, k_uptime_ticks(), period);
				uint64_t remaining = (run_end - due + period - 1) / period;

				if (skipped > remaining) {
					skipped = remaining;
				}
				stats.skipped_releases += skipped;
				due += skipped * period;
				index += skipped;
			}
		}
		if (atomic_get(&running) && due >= run_end) {
			k_sleep(K_TIMEOUT_ABS_TICKS(run_end));
		}
		run_complete = due >= run_end && k_uptime_ticks() >= run_end;
		run_finished = k_uptime_ticks();
		atomic_clear(&running);
		k_sem_give(&run_done);
	}
}

K_THREAD_DEFINE(workload_thread, 3072, workload, NULL, NULL, NULL, CONFIG_BENCH_WORK_PRIORITY, 0,
		0);

static void reply_hello(void)
{
	reply_append("{\"build_id\":\"%s\",", CONFIG_BENCH_BUILD_ID);
#if defined(CONFIG_FIDO2)
	reply_append("\"fido_priority\":%d,", CONFIG_FIDO2_THREAD_PRIORITY);
#else
	reply_append("\"fido_priority\":null,");
#endif
	reply_append("\"ok\":true,\"schema\":1,\"node_id\":\"%s\",\"variant\":\"%s\",",
		     CONFIG_BENCH_NODE_ID, CONFIG_BENCH_VARIANT);
	reply_append("\"fido_enabled\":%s,\"fido_error\":%d,\"usb_error\":%d,\"sensor_error\":%d,",
		     IS_ENABLED(CONFIG_FIDO2) ? "true" : "false", fido_error, usb_error,
		     sensor_ready_error);
	reply_append("\"workload\":\"%s\",\"period_ticks\":%llu,\"deadline_ticks\":%llu,",
		     IS_ENABLED(CONFIG_BENCH_SENSOR) ? "ambient_temperature_sensor"
						     : "synthetic_triangle",
		     (unsigned long long)k_ms_to_ticks_ceil64(CONFIG_BENCH_PERIOD_MS),
		     (unsigned long long)k_ms_to_ticks_ceil64(CONFIG_BENCH_DEADLINE_MS));
	reply_append("\"tick_hz\":%u,\"work_rounds\":%u,\"work_priority\":%d,",
		     CONFIG_SYS_CLOCK_TICKS_PER_SEC, work_rounds, CONFIG_BENCH_WORK_PRIORITY);
	reply_append("\"measurement_schema\":3,\"workload_clock\":\"%s\",\"clock_hz\":%u,",
		     IS_ENABLED(CONFIG_TIMER_HAS_64BIT_CYCLE_COUNTER) ? "cycle64" : "kernel_ticks",
		     bench_clock_hz());
	reply_append(
		"\"psa_provider\":\"%s\",\"up_clock\":\"%s\",",
		IS_ENABLED(CONFIG_BUILD_WITH_TFM)
			? "tfm"
			: (IS_ENABLED(CONFIG_SECURE_STORAGE) ? "zephyr_secure_storage" : "none"),
		IS_ENABLED(CONFIG_TIMER_HAS_64BIT_CYCLE_COUNTER) ? "cycle64" : "kernel_ticks");
	reply_append("\"control_transport\":\"%s\",\"spiram_enabled\":%s,\"touch_enabled\":%s}",
		     IS_ENABLED(CONFIG_BENCH_CONTROL_CDC_ACM) ? "usb_cdc_acm" : "physical_uart",
		     IS_ENABLED(CONFIG_ESP_SPIRAM) ? "true" : "false",
		     IS_ENABLED(CONFIG_INPUT_ESP32_TOUCH_SENSOR) ? "true" : "false");
}

static void reply_run_state(void)
{
	uint64_t now = k_uptime_ticks();
	bool active = atomic_get(&running) && now >= run_begin && now < run_end;

	reply_append(",\"run_id\":%u,\"now_tick\":%llu,\"begin_tick\":%llu,\"end_tick\":%llu,",
		     run_id, (unsigned long long)now, (unsigned long long)run_begin,
		     (unsigned long long)run_end);
	reply_append("\"window_active\":%s", active ? "true" : "false");
}

static void reply_status(void)
{
	reply_append("{\"ok\":true,\"running\":%s,\"threshold_mdegc\":%d,\"generation\":%u,",
		     atomic_get(&running) ? "true" : "false", (int)atomic_get(&threshold),
		     (unsigned int)atomic_get(&generation));
	reply_append("\"sample_mdegc\":%d,\"filtered_mdegc\":%d,\"alarm\":%s,",
		     (int)atomic_get(&sample_value), (int)atomic_get(&filtered_value),
		     atomic_get(&alarm_value) ? "true" : "false");
	reply_append("\"rx_overflows\":%u,\"tx_timeouts\":%u",
		     (unsigned int)atomic_get(&rx_overflows),
		     (unsigned int)atomic_get(&tx_timeouts));
	reply_run_state();
	reply_append("}");
}

static void reply_presence(void)
{
	struct bench_up_snapshot up = {0};

#if defined(CONFIG_FIDO2)
	bench_up_get(&up);
#endif
	reply_append("{\"ok\":true,\"sequence\":%llu,\"duration_ns\":%llu,\"result\":%d",
		     (unsigned long long)up.sequence, (unsigned long long)up.duration_ns,
		     up.result);
	reply_append(",\"pre_event_ns\":%llu,\"resume_delay_ns\":%llu,\"event_valid\":%s",
		     (unsigned long long)up.pre_event_ns, (unsigned long long)up.resume_delay_ns,
		     up.event_valid ? "true" : "false");
	reply_run_state();
	reply_append("}");
}

static void start_run(const char *duration_text)
{
	char *end;
	unsigned long duration = strtoul(duration_text, &end, 10);

	if (!*duration_text || *end || duration < 100 || duration > 3600000 || sensor_ready_error) {
		reply_append("{\"ok\":false,\"error\":\"invalid_duration_or_sensor\"}");
	} else if (!bench_clock_valid()) {
		reply_append("{\"ok\":false,\"error\":\"unsupported_clock_epoch_or_rate\"}");
	} else if (k_sem_take(&run_done, K_NO_WAIT)) {
		reply_append("{\"ok\":false,\"error\":\"already_running\"}");
	} else {
		memset(&stats, 0, sizeof(stats));
		run_begin = k_uptime_ticks() + k_ms_to_ticks_ceil64(CONFIG_BENCH_PERIOD_MS);
		run_duration_ticks = k_ms_to_ticks_ceil64(duration);
		run_end = run_begin + run_duration_ticks;
		run_finished = 0;
		run_complete = false;
		run_id++;
		atomic_set(&running, 1);
		k_sem_give(&run_start);
		reply_append("{\"ok\":true");
		reply_run_state();
		reply_append("}");
	}
}

static void stop_run(void)
{
	int ret;

	atomic_clear(&running);
	k_wakeup(workload_tid);
	ret = k_sem_take(&run_done, K_SECONDS(5));
	if (!ret) {
		k_sem_give(&run_done);
	}
	reply_append("{\"ok\":%s}", ret ? "false" : "true");
}

static void reply_stats(void)
{
	if (k_sem_take(&run_done, K_NO_WAIT)) {
		reply_append("{\"ok\":false,\"error\":\"stop_before_dump\"}");
		return;
	}

	reply_append("{\"ok\":true,\"complete\":%s,\"samples\":%llu,\"deadline_misses\":%llu,",
		     run_complete ? "true" : "false", (unsigned long long)stats.samples,
		     (unsigned long long)stats.deadline_misses);
	reply_append("\"skipped_releases\":%llu,\"sensor_errors\":%llu,",
		     (unsigned long long)stats.skipped_releases,
		     (unsigned long long)stats.sensor_errors);
	reply_append("\"measurement_schema\":3,\"clock_hz\":%u,\"histogram_bin_units\":%llu,",
		     bench_clock_hz(), (unsigned long long)bench_hist_bin_units());
	reply_append("\"max_lateness_units\":%llu,\"max_response_units\":%llu,",
		     (unsigned long long)stats.max_lateness_units,
		     (unsigned long long)stats.max_response_units);
	reply_append("\"max_execution_units\":%llu,\"sum_lateness_units\":%llu,",
		     (unsigned long long)stats.max_execution_units,
		     (unsigned long long)stats.sum_lateness_units);
	reply_append(
		"\"sum_execution_units\":%llu,\"captured_samples\":%u,\"dropped_samples\":%llu,",
		(unsigned long long)stats.sum_execution_units, stats.captured_samples,
		(unsigned long long)(stats.samples - stats.captured_samples));
	reply_append("\"run_id\":%u,\"begin_tick\":%llu,", run_id, (unsigned long long)run_begin);
	reply_append("\"end_tick\":%llu,\"finished_tick\":%llu,\"histogram\":[",
		     (unsigned long long)run_end, (unsigned long long)run_finished);

	for (unsigned int i = 0; i < BENCH_HIST_BINS; i++) {
		reply_append("%s%u", i ? "," : "", stats.histogram[i]);
	}

	reply_append("]}");
	k_sem_give(&run_done);
}

static void configure_work(const char *text)
{
	char *end;
	unsigned long value = strtoul(text, &end, 10);

	if (!*text || *end || value > 10000000) {
		reply_append("{\"ok\":false,\"error\":\"invalid_rounds\"}");
	} else if (k_sem_take(&run_done, K_NO_WAIT)) {
		reply_append("{\"ok\":false,\"error\":\"stop_before_configure\"}");
	} else {
		work_rounds = value;
		k_sem_give(&run_done);
		reply_append("{\"ok\":true,\"work_rounds\":%u}", work_rounds);
	}
}

static void reply_samples(const char *text)
{
	char *end;
	unsigned long offset = strtoul(text, &end, 10);

	if (!*text || *end || offset > BENCH_SAMPLE_CAPACITY) {
		reply_append("{\"ok\":false,\"error\":\"invalid_offset\"}");
		return;
	}
	if (k_sem_take(&run_done, K_NO_WAIT)) {
		reply_append("{\"ok\":false,\"error\":\"stop_before_dump\"}");
		return;
	}

	reply_append("{\"ok\":true,\"run_id\":%u,\"offset\":%lu,\"samples\":[", run_id, offset);
	for (unsigned int i = offset; i < stats.captured_samples && i < offset + 32; i++) {
		const struct bench_sample *sample = &stats.captured[i];

		reply_append("%s[%llu,%llu,%llu]", i > offset ? "," : "",
			     (unsigned long long)sample->lateness_units,
			     (unsigned long long)sample->response_units,
			     (unsigned long long)sample->execution_units);
	}
	reply_append("]}");
	k_sem_give(&run_done);
}

static void apply_threshold(const char *nonce, const char *threshold_text)
{
	char *end;
	bool valid = strlen(nonce) == 64;
	long value;

	for (size_t i = 0; valid && i < 64; i++) {
		valid = isxdigit((unsigned char)nonce[i]);
	}

	value = strtol(threshold_text, &end, 10);
	if (!valid || !*threshold_text || *end || value < -40000 || value > 125000) {
		reply_append("{\"ok\":false,\"error\":\"invalid_parameters\"}");
	} else if (!strcmp(last_request, nonce)) {
		reply_append("{\"ok\":false,\"error\":\"duplicate_last_request\"}");
	} else {
		/* The host verifies approval before sending APPLY. */
		atomic_set(&threshold, value);
		atomic_inc(&generation);
		strcpy(last_request, nonce);
		reply_append("{\"ok\":true,\"threshold_mdegc\":%ld,\"generation\":%u", value,
			     (unsigned int)atomic_get(&generation));
		reply_run_state();
		reply_append("}");
	}
}

static void handle_command(char *line)
{
	char *save;
	char *tokens[6];
	char *end;
	const char *op;
	unsigned long id_number;
	unsigned int id;
	int count = 0;

	for (char *t = strtok_r(line, " \r\t", &save); t && count < 6;
	     t = strtok_r(NULL, " \r\t", &save)) {
		tokens[count++] = t;
	}
	if (count < 2 || tokens[0][0] != '@') {
		return;
	}

	id_number = strtoul(tokens[0] + 1, &end, 10);
	if (*end || !tokens[0][1] || id_number > UINT32_MAX) {
		return;
	}

	id = id_number;
	op = tokens[1];
	reply_len = 0;
	reply_overflow = false;

	if (!strcmp(op, "HELLO") && count == 2) {
		reply_hello();
	} else if (!strcmp(op, "STATUS") && count == 2) {
		reply_status();
	} else if (!strcmp(op, "UP") && count == 2) {
		reply_presence();
	} else if (!strcmp(op, "START") && count == 3) {
		start_run(tokens[2]);
	} else if (!strcmp(op, "STOP") && count == 2) {
		stop_run();
	} else if (!strcmp(op, "STATS") && count == 2) {
		reply_stats();
	} else if (!strcmp(op, "WORK") && count == 3) {
		configure_work(tokens[2]);
	} else if (!strcmp(op, "SAMPLES") && count == 3) {
		reply_samples(tokens[2]);
	} else if (!strcmp(op, "APPLY") && count == 4) {
		apply_threshold(tokens[2], tokens[3]);
	} else {
		reply_append("{\"ok\":false,\"error\":\"unknown_command_or_arity\"}");
	}
	send_reply(id);
}

int main(void)
{
	char line[192];
	size_t length = 0;
	bool discard = false;
	int ret;

	workload_tid = workload_thread;
	if (!safe_identifier(CONFIG_BENCH_NODE_ID) || !safe_identifier(CONFIG_BENCH_VARIANT)) {
		LOG_ERR("Invalid node identifier or firmware variant");
		return -EINVAL;
	}
	if (!device_is_ready(control_uart)) {
		LOG_ERR("Control UART is not ready");
		return -ENODEV;
	}
#ifdef HAVE_ALARM_LED
	led_ready = gpio_is_ready_dt(&led) && !gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
#endif
#if defined(CONFIG_BENCH_SENSOR)
	if (!device_is_ready(sensor)) {
		sensor_ready_error = -ENODEV;
	}
#endif
#if defined(CONFIG_FIDO2)
	LOG_INF("Initializing FIDO2");
	fido_error = fido2_init();
	if (fido_error) {
		LOG_ERR("FIDO2 initialization failed: %d", fido_error);
	}
#endif
#if defined(CONFIG_USB_DEVICE_STACK_NEXT)
	LOG_INF("Initializing USB");
	usb_error = bench_usb_start(IS_ENABLED(CONFIG_FIDO2) && !fido_error);
	if (usb_error) {
		return usb_error;
	}
#endif

	ret = uart_irq_callback_user_data_set(control_uart, uart_irq, NULL);
	if (ret) {
		LOG_ERR("Control UART callback registration failed: %d", ret);
		return ret;
	}
	uart_irq_rx_enable(control_uart);
#if defined(CONFIG_FIDO2)
	if (!fido_error) {
		fido_error = fido2_start();
		if (fido_error) {
			LOG_ERR("FIDO2 start failed: %d", fido_error);
		}
	}
#endif
	LOG_INF("Control interface ready");

	for (;;) {
		uint8_t byte;

		k_msgq_get(&rx_bytes, &byte, K_FOREVER);
		if (byte == '\n') {
			if (!discard) {
				line[length] = '\0';
				handle_command(line);
			}
			length = 0;
			discard = false;
		} else if (!discard) {
			if (length < sizeof(line) - 1) {
				line[length++] = byte;
			} else {
				discard = true;
				atomic_inc(&rx_overflows);
			}
		}
	}
	return 0;
}
