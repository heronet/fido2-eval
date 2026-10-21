#include "stats.h"

void bench_stats_add(struct bench_stats *stats, uint64_t due, uint64_t start, uint64_t finish,
		     uint64_t deadline, uint64_t bin_units)
{
	uint64_t lateness = start > due ? start - due : 0;
	uint64_t response = finish > due ? finish - due : 0;
	uint64_t execution = finish - start;
	uint64_t bin = lateness / bin_units;
	unsigned int bucket = bin < BENCH_HIST_BINS - 1 ? bin : BENCH_HIST_BINS - 1;

	if (stats->captured_samples < BENCH_SAMPLE_CAPACITY) {
		struct bench_sample *sample = &stats->captured[stats->captured_samples++];

		sample->lateness_units = lateness;
		sample->response_units = response;
		sample->execution_units = execution;
	}
	stats->samples++;
	stats->sum_execution_units += execution;
	stats->sum_lateness_units += lateness;
	stats->histogram[bucket]++;
	if (lateness > stats->max_lateness_units) {
		stats->max_lateness_units = lateness;
	}
	if (response > stats->max_response_units) {
		stats->max_response_units = response;
	}
	if (execution > stats->max_execution_units) {
		stats->max_execution_units = execution;
	}
	if (response > deadline) {
		stats->deadline_misses++;
	}
}

uint64_t bench_skip_count(uint64_t next_due, uint64_t now, uint64_t period)
{
	return period && now > next_due ? (now - next_due) / period : 0;
}
