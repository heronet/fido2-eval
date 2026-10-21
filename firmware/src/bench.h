#ifndef BENCH_H_
#define BENCH_H_

#include <stdbool.h>
#include <stdint.h>

struct bench_up_snapshot {
	uint64_t sequence;
	uint64_t duration_ns;
	uint64_t pre_event_ns;
	uint64_t resume_delay_ns;
	bool event_valid;
	int result;
};

void bench_up_get(struct bench_up_snapshot *snapshot);
int bench_usb_start(bool fido_ready);

#endif /* BENCH_H_ */
