# Experiment procedure

Use one source checkout, Zephyr revision, toolchain, board configuration, and
host installation for the complete evaluation. Each build receives an identity
that the host checks against the attached manifest. Build directories and run
output directories must be new.

## Measurements

| Measurement | Firmware | Collection |
| --- | --- | --- |
| ROM and static RAM | Baseline, Settings, PSA PS | Three matched build manifests |
| Approval/rejection checks | Settings, PSA PS | One complete checks suite each |
| Approval latency | Settings, PSA PS | 5 warmups, 20 measured approvals each |
| Reference workload | Baseline | 60 s, light load |
| PSA PS coexistence | PSA PS | 60 s idle, 60 s active at 0.2 approvals/s |
| Load and priority | Settings | 3 loads × 2 priority orders × 3 repetitions × idle/active |

The scheduling experiment uses a 100 ms application period and deadline. The
application priority is 5; FIDO2 priority is 7 or 4. Lower numbers have higher
priority. Each scheduling phase lasts 60 s. Each active phase requests at most
12 approvals, spaced at least 5 s apart. The two scheduling commands total
36 minutes of measurement windows and up to 216 button presses.

Press and release the physical button once when prompted. Do not hold it down.
The procedure uses manual presence. Optional electrical button actuation is
recorded separately when `--gpio-pin` is supplied; the commands below do not
require it. No burst condition is included.

## 1. Build on the Mac

Extract the source into a new folder and open a terminal there. Activate your
existing Zephyr environment. Copy your working `board.conf` into this folder;
keep its USB identifiers and board settings. The board profile includes the
working H523 USB and storage overlay.

```sh
source "$HOME/Tools/zephyrproject/.venv/bin/activate"

for variant in baseline settings psa; do
    python tools/build.py \
        --profile blackpill-h523ce-cdc \
        --zephyr-base "$HOME/Source/embedded/zephyr" \
        --variant "$variant" \
        --board-conf board.conf \
        --build-dir "build/h523-$variant" || break
done

python tools/build.py \
    --profile blackpill-h523ce-cdc \
    --zephyr-base "$HOME/Source/embedded/zephyr" \
    --variant settings --fido-priority 4 \
    --board-conf board.conf \
    --build-dir build/h523-settings-fido-high
```

Continue when all four builds have completed. Generate the footprint CSV and
copy the complete build records into the repository:

```sh
python tools/footprint.py \
    build/h523-baseline/bench-artifacts/manifest.json \
    build/h523-settings/bench-artifacts/manifest.json \
    build/h523-psa/bench-artifacts/manifest.json \
    --out footprint.csv

mkdir artifacts
for name in baseline settings psa settings-fido-high; do
    cp -R "build/h523-$name/bench-artifacts" "artifacts/h523-$name"
done

scp -r host/fido_bench sirat@raspberrypi.local:~/fido2-paper/host/
scp pyproject.toml sirat@raspberrypi.local:~/fido2-paper/
scp -r artifacts sirat@raspberrypi.local:~/fido2-paper/
```

Recalculate footprints from these builds: all variants now contain the raw
sample buffer. The priority-4 Settings build is supplementary and is not a
second storage backend in the footprint comparison.

## 2. Prepare the Raspberry Pi

```sh
cd ~/fido2-paper
source .venv/bin/activate
python -m pip install --no-deps -e .

run_dir="results/evaluation-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$run_dir" "credentials/$(basename "$run_dir")"
printf '%s\n' "$run_dir" > evaluation.path
```

Keep this Pi session open. In a new session, restore it with:

```sh
cd ~/fido2-paper
source .venv/bin/activate
run_dir="$(cat evaluation.path)"
```

Use the same Pi power supply and CPU governor throughout. The host records the
reported governor and software identity in each run's metadata. Keep other
workloads on the Pi unchanged.

## 3. Baseline

On the Mac, connect and flash the board:

```sh
west flash -d build/h523-baseline
```

Move its USB connection to the Pi, then run:

```sh
fido-bench --port /dev/ttyACM0 stop &&
fido-bench --port /dev/ttyACM0 doctor > "$run_dir/baseline-doctor.json" &&
fido-bench --port /dev/ttyACM0 \
    --build-manifest artifacts/h523-baseline/manifest.json \
    coexist --mode idle --duration 60 --work-rounds 1000 \
    --out "$run_dir/baseline/idle" &&
fido-bench report "$run_dir/baseline/idle" --out "$run_dir/baseline/report"
```

## 4. Settings: calibration, checks, latency, scheduling

On the Mac:

```sh
west flash -d build/h523-settings
```

Move the USB connection to the Pi. Define the command and commission a new
credential. `commission` sets a PIN only when the authenticator has no PIN;
otherwise it asks for the existing PIN. It does not reset the authenticator.

```sh
bench_settings() {
    fido-bench --port /dev/ttyACM0 \
        --binding "credentials/$(basename "$run_dir")/settings.json" \
        --gate-db "$run_dir/settings-approvals.sqlite" \
        --build-manifest artifacts/h523-settings/manifest.json "$@"
}

bench_settings stop &&
bench_settings doctor > "$run_dir/settings-doctor.json" &&
bench_settings commission &&
bench_settings calibrate --out "$run_dir/calibration"
```

Calibration measures the original light load, then finds mixing-round counts
for approximately 30 ms and 70 ms of idle job execution. Each target must pass a
separate validation window within one percentage point of its target fraction.
Every calibration attempt is saved. These are nominal loads calibrated without
authentication, not measurements of CPU utilization under contention.

Continue only after `calibration/calibration.json` has been written:

```sh
bench_settings checks --wrong-pin --no-presence \
    --out "$run_dir/settings/checks" &&
bench_settings latency --trials 20 --warmup 5 --work-rounds 1000 \
    --out "$run_dir/settings/latency" &&
fido-bench report "$run_dir/settings/latency" \
    --out "$run_dir/settings/report" &&
bench_settings scheduling \
    --calibration "$run_dir/calibration/calibration.json" \
    --out "$run_dir/scheduling-app-high"
```

The scheduling runner prompts for the PIN once, writes its run order before
collecting, and executes all 18 phases. Idle phases require no button presses.
It restores the light workload when finished. Credentials and PIN persist
across the next ordinary flash.

## 5. Settings: FIDO2 higher priority

On the Mac:

```sh
west flash -d build/h523-settings-fido-high
```

Move the USB connection to the Pi. Reuse the Settings credential:

```sh
fido-bench --port /dev/ttyACM0 stop &&
fido-bench --port /dev/ttyACM0 \
    --binding "credentials/$(basename "$run_dir")/settings.json" \
    --gate-db "$run_dir/settings-approvals.sqlite" \
    --build-manifest artifacts/h523-settings-fido-high/manifest.json \
    scheduling \
    --calibration "$run_dir/calibration/calibration.json" \
    --out "$run_dir/scheduling-fido-high" &&
fido-bench scheduling-report \
    "$run_dir/scheduling-app-high" \
    "$run_dir/scheduling-fido-high" \
    --out "$run_dir/scheduling-report"
```

## 6. PSA Protected Storage

On the Mac:

```sh
west flash -d build/h523-psa
```

Move the USB connection to the Pi. Commission a separate PSA credential:

```sh
bench_psa() {
    fido-bench --port /dev/ttyACM0 \
        --binding "credentials/$(basename "$run_dir")/psa.json" \
        --gate-db "$run_dir/psa-approvals.sqlite" \
        --build-manifest artifacts/h523-psa/manifest.json "$@"
}

bench_psa stop &&
bench_psa doctor > "$run_dir/psa-doctor.json" &&
bench_psa commission &&
bench_psa checks --wrong-pin --no-presence \
    --out "$run_dir/psa/checks" &&
bench_psa latency --trials 20 --warmup 5 --work-rounds 1000 \
    --out "$run_dir/psa/latency" &&
bench_psa coexist --mode idle --duration 60 --work-rounds 1000 \
    --out "$run_dir/psa/idle" &&
bench_psa coexist --mode active --duration 60 --rate 0.2 --work-rounds 1000 \
    --out "$run_dir/psa/active" &&
fido-bench report \
    "$run_dir/psa/latency" "$run_dir/psa/idle" "$run_dir/psa/active" \
    --out "$run_dir/psa/report"
```

No erase is part of this procedure. If persistent storage is full, commission
fails explicitly; do not silently replace a binding or reset during collection.

## 7. Retrieve the data

On the Pi:

```sh
tar --exclude='*.sqlite*' -czf evaluation-results.tar.gz "$run_dir"
```

On the Mac, from the source folder:

```sh
scp sirat@raspberrypi.local:~/fido2-paper/evaluation-results.tar.gz .
tar -xzf evaluation-results.tar.gz
```

The extracted `results/` tree, `artifacts/`, `board.conf`, and `footprint.csv` are
included by the repository's ignore rules. Keep credentials and SQLite gate
state private. Commit either the extracted data or its archive; both contain
the same results.

## Output and interpretation

Each measurement directory contains:

- `metadata.json` and `build-manifest.json`: firmware identity, runtime load,
  priorities, host identity, requested duration/rate, and presence method.
- `latency.csv`: all approval attempts, warmup markers, timing components,
  errors, and whether dispatch completed inside the workload window.
- `workload.json` and `workload-samples.csv`: aggregate counters and individual
  job lateness, response, and elapsed execution intervals in clock units.
- `collection.json` and `final-state.json`: completion and device state.

Scheduling output adds `plan.json` and `completion.json`.
`scheduling-report/runs.csv` contains every repetition.
`scheduling-report/cells.csv` contains the minimum, median, and maximum of each
per-run timing statistic across three repetitions, plus summed counts. p99
uses the nearest-rank method within each run; it is not a percentile of maxima.
Dedicated latency and PSA coexistence summaries are in their `report/summary.csv`.

Approval latency is CTAP elapsed time plus SBC verification/database time,
minus the interval from entering the UP wait to delivery of the button-press
input callback. It includes the subsequent delay before the FIDO thread
resumes. `pre_event_ns`, `resume_delay_ns`, and the legacy full
`up_wait_ns` subtraction are retained separately. The input callback timestamp
is not a hardware GPIO-edge timestamp; debounce/input-delivery delay is part
of the excluded interval. Dispatch time is reported separately. PIN entry is
outside the measured interval.

The application provides an instrumented custom UP backend using the physical
input device and a semaphore. It accepts one press event per request, records
the timestamp before waking the FIDO thread, and supports cancellation and
timeout. CTAP core sources are unchanged by this harness.

Job execution is an elapsed interval and includes preemption. Raw samples have
no histogram upper-bound limit. The buffer stores 1,024 executed jobs: at a
100 ms period, all jobs in a 60 s phase fit. Longer custom runs report dropped
samples and omit incomplete raw-sample quantiles. The older 1 microsecond
histogram remains available, but its overflow bucket is not used for p99.
A skipped release is reported separately from an executed job that misses its
deadline. A delayed job may finish after the nominal window; its timing is
retained.

All successful approvals remain in the raw CSV. Latency summaries include only
approvals whose start and dispatch both fall within the workload window;
outside-window successes are counted separately. Errors leave partial data
and stop collection without automatically repeating a PIN attempt. Preserve
the complete output and inspect the error before deciding the next collection.
The two priority orders are collected in separate flash blocks; three
repetitions do not remove every possible time/temperature/order effect.

The SBC gate verifies the node-bound assertion and consumes the one-use
challenge before sending `APPLY`. The MCU control command does not verify the
assertion itself. The control connection and SBC are trusted in this experiment.
