#include <errno.h>

#include <zephyr/authentication/fido2/fido2_up.h>
#include <zephyr/input/input.h>
#include <zephyr/kernel.h>

#include "bench.h"

static K_SEM_DEFINE(up_sem, 0, 1);
static struct k_spinlock snapshot_lock;
static struct bench_up_snapshot last_wait;
static uint64_t event_ns;
static bool event_seen;
static bool cancelled;
static bool armed;

static uint64_t now_ns(void)
{
#if defined(CONFIG_TIMER_HAS_64BIT_CYCLE_COUNTER)
	return k_cyc_to_ns_floor64(k_cycle_get_64());
#else
	return k_ticks_to_ns_floor64(k_uptime_ticks());
#endif
}

static void up_input_cb(struct input_event *event, void *user_data)
{
	k_spinlock_key_t key;

	ARG_UNUSED(user_data);
	if (!event->sync || !event->value) {
		return;
	}

	key = k_spin_lock(&snapshot_lock);
	if (armed && !cancelled && !event_seen) {
		event_ns = now_ns();
		event_seen = true;
		k_sem_give(&up_sem);
	}
	k_spin_unlock(&snapshot_lock, key);
}

INPUT_CALLBACK_DEFINE(DEVICE_DT_GET(DT_CHOSEN(zephyr_fido2_up_input)), up_input_cb, NULL);

int fido2_up_wait(void)
{
	uint64_t begin = now_ns();
	int ret = k_sem_take(&up_sem, K_MSEC(CONFIG_FIDO2_UP_TIMEOUT_MS));
	uint64_t end = now_ns();
	k_spinlock_key_t key = k_spin_lock(&snapshot_lock);

	ret = cancelled ? -ECANCELED : (ret ? -ETIMEDOUT : 0);
	last_wait.sequence++;
	last_wait.duration_ns = end - begin;
	last_wait.event_valid = !ret && event_seen && event_ns <= end;
	last_wait.pre_event_ns = last_wait.event_valid && event_ns > begin ? event_ns - begin : 0;
	last_wait.resume_delay_ns = last_wait.event_valid ? end - MAX(event_ns, begin) : 0;
	last_wait.result = ret;
	armed = false;
	k_spin_unlock(&snapshot_lock, key);
	return ret;
}

void fido2_up_reset(void)
{
	k_spinlock_key_t key = k_spin_lock(&snapshot_lock);

	k_sem_reset(&up_sem);
	cancelled = false;
	event_seen = false;
	armed = true;
	k_spin_unlock(&snapshot_lock, key);
}

void fido2_up_cancel(void)
{
	k_spinlock_key_t key = k_spin_lock(&snapshot_lock);

	cancelled = true;
	armed = false;
	k_sem_give(&up_sem);
	k_spin_unlock(&snapshot_lock, key);
}

void bench_up_get(struct bench_up_snapshot *snapshot)
{
	k_spinlock_key_t key = k_spin_lock(&snapshot_lock);

	*snapshot = last_wait;
	k_spin_unlock(&snapshot_lock, key);
}
