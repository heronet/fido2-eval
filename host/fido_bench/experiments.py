from __future__ import annotations
import csv
import dataclasses
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import platform
import secrets
import shutil
import time
from pathlib import Path
from fido2.ctap import CtapError
from .gate import Gate, Rejected, Request, Evidence, canonical

FIELDS = [
    "trial",
    "warmup",
    "success",
    "error",
    "start_offset_ns",
    "pin_exchange_ns",
    "assertion_ns",
    "ctap_total_ns",
    "gate_ns",
    "up_wait_ns",
    "wait_excluded_ns",
    "pre_event_ns",
    "resume_delay_ns",
    "event_excluded_ns",
    "up_sequence_before",
    "up_sequence_after",
    "dispatch_ns",
    "applied_threshold_mdegc",
    "workload_run_id",
    "workload_start_tick",
    "workload_end_tick",
    "workload_overlap",
]


class WorkloadWindowClosed(RuntimeError):
    pass


def host_snapshot():
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_governors": {
            str(p): p.read_text().strip()
            for p in sorted(
                Path("/sys/devices/system/cpu/cpufreq").glob("policy*/scaling_governor")
            )
        },
        "fido2": importlib.metadata.version("fido2"),
        "suite_sha256": hashlib.sha256(
            b"".join(
                p.name.encode() + b"\0" + p.read_bytes()
                for p in sorted(Path(__file__).parent.glob("*.py"))
            )
        ).hexdigest(),
    }


def prepare_output(path, node, condition, binding=None, build_manifest=None, **details):
    out = Path(path)
    out.mkdir(parents=True, exist_ok=False)
    hello = node.hello()
    if binding is not None and hello["node_id"] != binding.node_id:
        raise RuntimeError("UART node differs from commissioned node")
    if hello.get("sensor_error") or hello.get("fido_error") or hello.get("usb_error"):
        raise RuntimeError(f"Firmware initialization failed: {hello}")
    metadata = {
        "schema": 1,
        "data_kind": "hardware",
        "condition": condition,
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "firmware": hello,
        "host": host_snapshot(),
        **details,
    }
    if build_manifest:
        metadata["build"] = json.loads(Path(build_manifest).read_text())
        if metadata["build"].get("variant") != hello["variant"]:
            raise RuntimeError("Build manifest variant does not match running firmware")
        if hello.get("measurement_schema") == 3 and (
            not metadata["build"].get("build_id")
            or metadata["build"]["build_id"] != hello.get("build_id")
        ):
            raise RuntimeError("Build manifest identity does not match the flashed image")
        for field in (
            "control_transport",
            "spiram_enabled",
            "touch_enabled",
            "work_priority",
            "fido_priority",
        ):
            if field in metadata["build"] and metadata["build"][field] != hello.get(field):
                raise RuntimeError(f"Build manifest {field} does not match running firmware")
        shutil.copyfile(build_manifest, out / "build-manifest.json")
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2))
    return out, hello


def approval(node, auth, gate, binding, pin, threshold, ttl=60, require_workload=False):
    before = node.up()
    if require_workload and not before.get("window_active"):
        raise WorkloadWindowClosed("The workload measurement window has ended")
    request = gate.create(threshold, ttl)
    evidence, timing = auth.assert_request(binding, request.digest, pin)
    # Include verification and the SQLite commit in gate timing.
    start = time.perf_counter_ns()
    payload = gate.consume(request, evidence)
    consumed = time.perf_counter_ns()
    after = node.up()
    if after["sequence"] != before["sequence"] + 1 or after["result"] != 0:
        raise RuntimeError("UP timing cannot be paired to exactly one successful wait")
    wait = after["duration_ns"]
    elapsed = timing["ctap_total_ns"] + consumed - start
    # Host and MCU elapsed intervals use separate clock origins.
    machine = elapsed - wait
    if machine < 0:
        raise RuntimeError("Negative wait-excluded time; check measurement clocks")
    if not after.get("event_valid"):
        raise RuntimeError("Successful approval has no matching button event timestamp")
    event_excluded = elapsed - after["pre_event_ns"]
    if after["pre_event_ns"] + after["resume_delay_ns"] != wait or event_excluded < 0:
        raise RuntimeError("Inconsistent user-presence timing")
    send = time.perf_counter_ns()
    result = node.apply(payload["nonce"], threshold)
    sent = time.perf_counter_ns()
    if result.get("threshold_mdegc") != threshold:
        raise RuntimeError("Applied value differs from approved value")
    return {
        **timing,
        "gate_ns": consumed - start,
        "up_wait_ns": wait,
        "wait_excluded_ns": machine,
        "pre_event_ns": after["pre_event_ns"],
        "resume_delay_ns": after["resume_delay_ns"],
        "event_excluded_ns": event_excluded,
        "up_sequence_before": before["sequence"],
        "up_sequence_after": after["sequence"],
        "dispatch_ns": sent - send,
        "applied_threshold_mdegc": threshold,
        "workload_run_id": before.get("run_id"),
        "workload_start_tick": before.get("now_tick"),
        "workload_end_tick": result.get("now_tick"),
        "workload_overlap": (
            int(
                before["window_active"]
                and result.get("window_active", False)
                and before["run_id"] == result.get("run_id")
            )
            if "window_active" in before
            else None
        ),
    }


def wait_for_window(node, window, hz):
    deadline = time.monotonic() + (window["begin_tick"] - window["now_tick"]) / hz + 5
    state = window
    while not state["window_active"]:
        if not state.get("running", True) or time.monotonic() > deadline:
            raise RuntimeError("Workload did not enter its measurement window")
        remaining = max(0, state["begin_tick"] - state["now_tick"]) / hz
        time.sleep(max(0.001, remaining))
        state = node.status()
        if state["run_id"] != window["run_id"]:
            raise RuntimeError("Workload run changed during collection")
    return state


def wait_for_end(node, window, hz):
    state = node.status()
    remaining = max(0, window["end_tick"] - state["now_tick"]) / hz
    deadline = time.monotonic() + remaining + 5
    while state["running"]:
        if time.monotonic() > deadline:
            raise RuntimeError("Workload did not finish its measurement window")
        time.sleep(max(0.01, remaining + 0.01))
        state = node.status()
        remaining = max(0, window["end_tick"] - state["now_tick"]) / hz
    if state["run_id"] != window["run_id"] or state["now_tick"] < window["end_tick"]:
        raise RuntimeError("Workload stopped before the measurement window ended")


def collect(
    node,
    auth,
    binding,
    pin,
    database,
    out,
    mode,
    trials=20,
    duration=60,
    rate=0,
    warmup=0,
    threshold=30000,
    build_manifest=None,
    presence_mode="manual",
    work_rounds=None,
):
    if node.status()["running"]:
        raise RuntimeError("An experiment is already running; stop it before starting another")
    if work_rounds is not None:
        node.configure(work_rounds)
    path, hello = prepare_output(
        out,
        node,
        mode,
        binding if auth else None,
        build_manifest,
        requested_duration_s=duration,
        trials=trials,
        warmup=warmup,
        requested_rate_hz=rate,
        presence_mode=presence_mode,
        token_policy="fresh token per approval",
        workload_running_during_latency=True,
        latency_metric="event_excluded_ns",
        event_reference="input_callback_press_before_semaphore_give",
    )
    if mode != "idle" and not hello["fido_enabled"]:
        raise RuntimeError("Active authentication requires a FIDO2 build")
    if hello.get("measurement_schema") != 3:
        raise RuntimeError("Rebuild the firmware with measurement schema 3 before collecting")
    gate = Gate(database, binding) if auth else None
    requested_ms = 3600000 if mode == "latency" else round(duration * 1000)
    failure = None
    cleanup_failure = None
    started = False
    stats = None
    index = 0
    try:
        window = node.start(requested_ms)
        started = True
        window = wait_for_window(node, window, hello["tick_hz"])
        start = time.perf_counter_ns()
        deadline = time.monotonic() + (window["end_tick"] - window["now_tick"]) / hello["tick_hz"]
        limit = (
            trials + warmup
            if mode == "latency"
            else (math.ceil(duration * rate) if rate > 0 else math.inf)
        )
        with (path / "latency.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            if mode == "idle":
                wait_for_end(node, window, hello["tick_hz"])
            else:
                while index < limit and (mode == "latency" or time.monotonic() < deadline):
                    began = time.perf_counter_ns()
                    row = {
                        "trial": index,
                        "warmup": int(mode == "latency" and index < warmup),
                        "success": 0,
                        "start_offset_ns": began - start,
                    }
                    try:
                        value = threshold + (index % 2 if threshold < 125000 else -(index % 2))
                        row.update(
                            approval(node, auth, gate, binding, pin, value, require_workload=True)
                        )
                        row["success"] = 1
                        if mode == "latency" and not row["workload_overlap"]:
                            raise RuntimeError("Approval extended beyond the workload window")
                    except WorkloadWindowClosed as e:
                        if mode == "active":
                            break
                        row["error"] = f"{type(e).__name__}: {e}"
                        failure = e
                    except Exception as e:
                        row["error"] = f"{type(e).__name__}: {e}"
                        failure = e
                    writer.writerow(row)
                    f.flush()
                    index += 1
                    if failure:
                        # Stop on failure to avoid repeating PIN attempts or dispatch.
                        break
                    if rate > 0:
                        delay = 1 / rate - (time.perf_counter_ns() - began) / 1e9
                        if delay > 0:
                            time.sleep(
                                min(delay, max(0, deadline - time.monotonic()))
                                if mode != "latency"
                                else delay
                            )
                if mode == "active" and failure is None:
                    wait_for_end(node, window, hello["tick_hz"])
    except BaseException as e:
        failure = e
    finally:
        try:
            if started:
                node.stop()
                stats = node.stats()
                (path / "workload.json").write_text(json.dumps(stats, indent=2))
                with (path / "workload-samples.csv").open("w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(("lateness_units", "response_units", "execution_units"))
                    writer.writerows(node.samples(stats))
                (path / "final-state.json").write_text(json.dumps(node.status(), indent=2))
        except Exception as e:
            cleanup_failure = e
        finally:
            if gate:
                gate.close()
    expected_complete = (
        index == trials + warmup if mode == "latency" else bool(stats and stats["complete"])
    )
    if failure is None and cleanup_failure is None and not expected_complete:
        failure = RuntimeError("Collection ended before the requested measurement completed")
    completed = failure is None and cleanup_failure is None
    (path / "collection.json").write_text(
        json.dumps({"completed": completed, "attempts": index}, indent=2)
    )
    if failure is not None or cleanup_failure is not None:
        errors = {
            "error": f"{type(failure).__name__}: {failure}" if failure is not None else None,
            "cleanup_error": (
                f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                if cleanup_failure is not None
                else None
            ),
        }
        (path / "error.json").write_text(json.dumps(errors, indent=2))
        if failure is not None and not isinstance(failure, Exception):
            raise failure
        message = errors["error"] or errors["cleanup_error"]
        if errors["error"] and errors["cleanup_error"]:
            message += f"; cleanup also failed: {errors['cleanup_error']}"
        raise RuntimeError(f"{message}; partial data saved in {path}") from (
            failure if failure is not None else cleanup_failure
        )
    return path


def checks(
    node,
    auth,
    binding,
    pin,
    database,
    out,
    wrong_pin=False,
    no_presence=False,
    build_manifest=None,
    presence_mode="manual",
):
    path, hello = prepare_output(
        out, node, "workflow-checks", binding, build_manifest, presence_mode=presence_mode
    )
    gate = Gate(database, binding)
    results = []

    def reject_case(name, request, evidence):
        before = node.status()
        try:
            gate.execute(request, evidence, node)
        except Rejected as e:
            after = node.status()
            passed = (
                after["generation"] == before["generation"]
                and after["threshold_mdegc"] == before["threshold_mdegc"]
            )
            results.append(
                {
                    "test": name,
                    "status": "PASS" if passed else "FAIL",
                    "detail": str(e),
                    "before": before,
                    "after": after,
                }
            )
        except Exception as e:
            results.append({"test": name, "status": "ERROR", "detail": str(e)})
        else:
            results.append({"test": name, "status": "FAIL", "detail": "Unexpected execution"})

    try:
        request = gate.create(31000)
        evidence, _ = auth.assert_request(binding, request.digest, pin)
        before = node.status()
        result = gate.execute(request, evidence, node)
        after = node.status()
        passed = (
            result["threshold_mdegc"] == 31000 and after["generation"] == before["generation"] + 1
        )
        results.append(
            {
                "test": "valid_approval",
                "status": "PASS" if passed else "FAIL",
                "before": before,
                "after": after,
            }
        )
        reject_case("same_node_replay", request, evidence)

        request = gate.create(32000)
        evidence, _ = auth.assert_request(binding, request.digest, pin)
        changed = request.payload
        changed["parameters"]["threshold_mdegc"] = 33000
        reject_case("modified_parameters", Request(canonical(changed)), evidence)
        changed = request.payload
        changed["node_id"] += "-other"
        reject_case("wrong_node_id", Request(canonical(changed)), evidence)
        reject_case(
            "credential_id_mismatch",
            request,
            dataclasses.replace(evidence, credential_id=secrets.token_bytes(32)),
        )
        reject_case(
            "invalid_signature",
            request,
            dataclasses.replace(
                evidence, signature=evidence.signature[:-1] + bytes([evidence.signature[-1] ^ 1])
            ),
        )
        reject_case("no_evidence_through_sbc_gate", request, Evidence(b"", b"", b""))

        expired = gate.create(34000, ttl=0.001)
        expired_evidence, _ = auth.assert_request(binding, expired.digest, pin)
        time.sleep(0.002)
        reject_case("expired_challenge", expired, expired_evidence)

        # Omit PIN authorization for this rejection check.
        no_pin = gate.create(35000)
        try:
            ev, _ = auth.assert_request(binding, no_pin.digest, pin, with_pin=False)
        except CtapError as e:
            allowed = {0x36, 0x33, 0x27, 0x3B}
            results.append(
                {
                    "test": "no_pin",
                    "status": "PASS" if int(e.code) in allowed else "ERROR",
                    "detail": str(e),
                }
            )
        else:
            reject_case("no_pin", no_pin, ev)

        if wrong_pin:
            retries, _ = auth.pin.get_pin_retries()
            if retries < 3:
                results.append(
                    {
                        "test": "wrong_pin",
                        "status": "SKIP",
                        "detail": "Fewer than three retries remain",
                    }
                )
            else:
                bad = ("9" if pin[0] != "9" else "8") + pin[1:]
                try:
                    auth.assert_request(binding, gate.create(36000).digest, bad)
                except CtapError as e:
                    results.append(
                        {
                            "test": "wrong_pin",
                            "status": "PASS" if int(e.code) == 0x31 else "ERROR",
                            "detail": str(e),
                        }
                    )
                else:
                    results.append({"test": "wrong_pin", "status": "FAIL"})
                from fido2.ctap2.pin import ClientPin

                auth.token(pin, ClientPin.PERMISSION.GET_ASSERTION, binding.rp_id)
        else:
            results.append(
                {
                    "test": "wrong_pin",
                    "status": "SKIP",
                    "detail": "Enable --wrong-pin for one attempt",
                }
            )

        if no_presence:
            import sys

            print(
                "No-presence test now: do NOT press the node button; wait for cancellation/timeout.",
                file=sys.stderr,
            )
            from .auth import Presence

            saved = auth.presence
            saved.arm()  # Wait for the previous pulse to finish.
            auth.presence = Presence(prompt=False)
            try:
                auth.assert_request(binding, gate.create(37000).digest, pin)
            except CtapError as e:
                allowed = {0x27, 0x2D, 0x2F}
                results.append(
                    {
                        "test": "no_presence",
                        "status": "PASS" if int(e.code) in allowed else "ERROR",
                        "detail": str(e),
                    }
                )
            else:
                results.append(
                    {
                        "test": "no_presence",
                        "status": "FAIL",
                        "detail": "Presence was unexpectedly received",
                    }
                )
            finally:
                auth.presence = saved
        else:
            results.append(
                {
                    "test": "no_presence",
                    "status": "SKIP",
                    "detail": "Enable --no-presence and do not press",
                }
            )
    except Exception as e:
        results.append(
            {"test": "suite_execution", "status": "ERROR", "detail": f"{type(e).__name__}: {e}"}
        )
    finally:
        gate.close()
        (path / "checks.json").write_text(json.dumps(results, indent=2))
    return results
