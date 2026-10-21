#ifndef BENCH_CLOCK_H_
#define BENCH_CLOCK_H_

#include <zephyr/kernel.h>

static inline uint64_t bench_clock_now(void)
{
#if defined(CONFIG_TIMER_HAS_64BIT_CYCLE_COUNTER)
	return k_cycle_get_64();
#else
	return k_uptime_ticks();
#endif
}

static inline uint32_t bench_clock_hz(void)
{
#if defined(CONFIG_TIMER_HAS_64BIT_CYCLE_COUNTER)
	return sys_clock_hw_cycles_per_sec();
#else
	return CONFIG_SYS_CLOCK_TICKS_PER_SEC;
#endif
}

static inline uint64_t bench_clock_from_ticks(uint64_t ticks)
{
	return ticks * (bench_clock_hz() / CONFIG_SYS_CLOCK_TICKS_PER_SEC);
}

static inline uint64_t bench_hist_bin_units(void)
{
	return DIV_ROUND_UP(bench_clock_hz(), 1000000U);
}

static inline bool bench_clock_valid(void)
{
	uint64_t before = k_uptime_ticks();
	uint64_t units = bench_clock_now();
	uint64_t after = k_uptime_ticks();
	uint32_t hz = bench_clock_hz();
	uint64_t tick;

	if (hz < CONFIG_SYS_CLOCK_TICKS_PER_SEC || hz % CONFIG_SYS_CLOCK_TICKS_PER_SEC) {
		return false;
	}

	/* Absolute release times require the counter and ticks to share an epoch. */
	tick = units / (hz / CONFIG_SYS_CLOCK_TICKS_PER_SEC);
	return tick >= before && tick <= after;
}

#endif /* BENCH_CLOCK_H_ */
