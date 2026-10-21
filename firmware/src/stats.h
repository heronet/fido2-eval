#ifndef BENCH_STATS_H_
#define BENCH_STATS_H_

#include <stdint.h>

#define BENCH_HIST_BINS       257
#define BENCH_SAMPLE_CAPACITY 1024

struct bench_sample {
	uint64_t lateness_units;
	uint64_t response_units;
	uint64_t execution_units;
};

struct bench_stats {
	uint64_t samples;
	uint64_t deadline_misses;
	uint64_t skipped_releases;
	uint64_t max_lateness_units;
	uint64_t max_response_units;
	uint64_t max_execution_units;
	uint64_t sum_lateness_units;
	uint64_t sensor_errors;
	uint64_t sum_execution_units;
	uint32_t captured_samples;
	struct bench_sample captured[BENCH_SAMPLE_CAPACITY];
	uint32_t histogram[BENCH_HIST_BINS];
};

void bench_stats_add(struct bench_stats *stats, uint64_t due, uint64_t start, uint64_t finish,
		     uint64_t deadline, uint64_t bin_units);

/* Elapsed release periods; the caller caps the count at the run end. */
uint64_t bench_skip_count(uint64_t next_due, uint64_t now, uint64_t period);

#endif /* BENCH_STATS_H_ */
