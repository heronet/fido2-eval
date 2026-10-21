from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path

from .auth import Authenticator, Presence
from .gate import Binding, Gate
from .node import Node
from . import experiments


def parser():
    p = argparse.ArgumentParser(description="Zephyr FIDO2 measurement and offline approval suite")
    p.add_argument("--port", help="CDC ACM or physical UART path, e.g. /dev/ttyACM0")
    p.add_argument(
        "--serial-kind",
        choices=("cdc", "uart"),
        default="cdc",
        help="cdc asserts DTR; uart leaves DTR/RTS low for a physical adapter",
    )
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument(
        "--hid-path", help="Path printed by 'list'; required with multiple authenticators"
    )
    p.add_argument("--binding", default="binding.json")
    p.add_argument("--gate-db", default="approvals.sqlite")
    p.add_argument(
        "--gpio-pin", type=int, help="Optional SBC GPIO line driving an isolated button switch"
    )
    p.add_argument("--gpio-chip", type=int, default=0)
    p.add_argument(
        "--timeout", type=float, default=45, help="CTAP assertion cancellation timeout, seconds"
    )
    p.add_argument("--build-manifest", help="Manifest from tools/build.py for this flashed image")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List USB HID authenticator paths")
    sub.add_parser("doctor", help="Check UART identity and CTAP capabilities")
    sub.add_parser("status", help="Read workload and maintenance state")
    sub.add_parser("stop", help="Stop a workload left running by an interrupted host")
    sub.add_parser("set-pin", help="Set an initial PIN; never changes an existing PIN")
    for name in ("enroll", "commission"):
        en = sub.add_parser(name, help="Commission one resident ES256 credential")
        en.add_argument("--rp-id", default="maintenance.invalid")
    ap = sub.add_parser("approve", help="Approve and apply a temperature-alarm threshold")
    ap.add_argument("--threshold", type=int, default=30000, help="Millidegrees Celsius")
    for name in ("latency", "coexist"):
        q = sub.add_parser(name)
        q.add_argument(
            "--out", required=True, help="New output directory; existing data is never overwritten"
        )
        q.add_argument("--threshold", type=int, default=30000)
        q.add_argument("--work-rounds", type=int, default=1000)
        q.add_argument(
            "--rate", type=float, default=0, help="Maximum approvals/s; 0 = sequential saturation"
        )
        if name == "latency":
            q.add_argument("--trials", type=int, default=20)
            q.add_argument("--warmup", type=int, default=5)
        else:
            q.add_argument("--mode", choices=("idle", "active"), required=True)
            q.add_argument("--duration", type=float, default=60, help="Workload window in seconds")
    ch = sub.add_parser(
        "checks", help="Functional approval and rejection checks, separate from benchmarks"
    )
    ch.add_argument("--out", required=True)
    ch.add_argument(
        "--wrong-pin", action="store_true", help="One wrong-PIN attempt, then a correct PIN"
    )
    ch.add_argument(
        "--no-presence",
        action="store_true",
        help="Check rejection without a button press",
    )
    rep = sub.add_parser(
        "report", help="Summarize saved hardware runs and plot lateness distributions"
    )
    rep.add_argument("runs", nargs="+")
    rep.add_argument("--out", required=True)
    cal = sub.add_parser("calibrate", help="Calibrate the fixed Settings scheduling loads")
    cal.add_argument("--out", required=True)
    schedule = sub.add_parser("scheduling", help="Run three repetitions at each calibrated load")
    schedule.add_argument("--calibration", required=True)
    schedule.add_argument("--out", required=True)
    summary = sub.add_parser("scheduling-report", help="Compare both completed priority orders")
    summary.add_argument("runs", nargs=2)
    summary.add_argument("--out", required=True)
    return p


def show(value):
    print(json.dumps(value, indent=2))


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    if a.timeout <= 0 or a.timeout > 3600:
        p.error("--timeout must be in (0,3600]")
    if hasattr(a, "threshold") and not -40000 <= a.threshold <= 125000:
        p.error("--threshold must be in [-40000,125000]")
    if hasattr(a, "rate") and a.rate < 0:
        p.error("--rate cannot be negative")
    if a.command == "latency" and (a.trials < 1 or a.warmup < 0):
        p.error("--trials must be positive and --warmup nonnegative")
    if a.command == "coexist" and not 0.1 <= a.duration <= 3600:
        p.error("--duration must be in [0.1,3600]")
    node = auth = presence = None
    try:
        if a.command == "list":
            from fido2.hid import CtapHidDevice

            devices = list(CtapHidDevice.list_devices())
            try:
                show(
                    [
                        {
                            "path": Authenticator.path(d),
                            "vid": f"0x{d.descriptor.vid:04x}",
                            "pid": f"0x{d.descriptor.pid:04x}",
                        }
                        for d in devices
                    ]
                )
            finally:
                for d in devices:
                    d.close()
            return 0
        if a.command == "scheduling-report":
            from .scheduling import report

            show(report(a.runs, a.out))
            return 0
        if a.command == "report":
            from .report import report

            show(report(a.runs, a.out))
            return 0
        if not a.port:
            p.error("--port is required for hardware commands")
        node = Node(a.port, a.baud, dtr=a.serial_kind == "cdc")
        hello = node.hello()
        if hello.get("schema") != 1:
            raise RuntimeError("Unexpected firmware protocol")
        if a.command in ("status", "stop"):
            show(node.status() if a.command == "status" else node.stop())
            return 0
        if hello.get("sensor_error") or hello.get("fido_error") or hello.get("usb_error"):
            raise RuntimeError(f"Firmware initialization failed: {hello}")
        if a.command == "calibrate":
            from .scheduling import calibrate

            show(calibrate(node, a.build_manifest, a.out))
            return 0
        idle = a.command == "coexist" and a.mode == "idle"
        if not idle and (a.command != "doctor" or hello["fido_enabled"]):
            if not hello["fido_enabled"]:
                raise RuntimeError("This command needs a FIDO2 firmware variant")
            presence = Presence(a.gpio_pin, a.gpio_chip)
            auth = Authenticator(presence, a.hid_path, a.timeout)
        if a.command == "doctor":
            result = {"firmware": hello, "state": node.status()}
            if auth:
                info = auth.ctap.info
                result["ctap"] = {
                    "path": auth.path(auth.device),
                    "versions": info.versions,
                    "options": info.options,
                    "pin_uv_protocols": info.pin_uv_protocols,
                }
                if not info.options.get("rk") or not info.options.get("up"):
                    raise RuntimeError("The benchmark needs resident credentials and user presence")
            show(result)
            return 0
        if a.command == "set-pin":
            if auth.ctap.info.options.get("clientPin"):
                raise RuntimeError("PIN already configured; this command will not change it")
            pin = getpass.getpass("New node PIN: ")
            if pin != getpass.getpass("Repeat new PIN: "):
                raise ValueError("PINs do not match")
            auth.set_pin(pin)
            print("PIN configured.")
            return 0
        if a.command in ("enroll", "commission"):
            if Path(a.binding).exists():
                raise FileExistsError("Binding already exists; choose another --binding path")
            if a.command == "commission" and not auth.ctap.info.options.get("clientPin"):
                pin = getpass.getpass("New node PIN: ")
                if pin != getpass.getpass("Repeat new PIN: "):
                    raise ValueError("PINs do not match")
                auth.set_pin(pin)
            else:
                pin = getpass.getpass("Node PIN: ")
            print("Commissioning: press the node button when requested.", file=sys.stderr)
            binding = auth.enroll(hello["node_id"], a.rp_id, pin)
            binding.save(a.binding)
            print(f"Commissioned {binding.node_id}; saved {a.binding}.")
            return 0
        binding = None if idle else Binding.load(a.binding)
        if binding and hello["node_id"] != binding.node_id:
            raise RuntimeError("UART node and commissioning binding differ")
        pin = None if idle else getpass.getpass("Node PIN: ")
        if a.command == "scheduling":
            from .scheduling import run

            path = run(
                node,
                auth,
                binding,
                pin,
                a.gate_db,
                a.build_manifest,
                a.calibration,
                a.out,
                "electrical_button_pulse" if a.gpio_pin is not None else "manual",
            )
            print(f"Saved {path}")
        elif a.command == "approve":
            owned = not node.status()["running"]
            if owned:
                node.start(60000)
            gate = Gate(a.gate_db, binding)
            try:
                result = experiments.approval(node, auth, gate, binding, pin, a.threshold)
                time.sleep(2 * hello["period_ticks"] / hello["tick_hz"])
                show({"timing": result, "state": node.status()})
            finally:
                failed = sys.exc_info()[0] is not None
                gate.close()
                if owned:
                    try:
                        node.stop()
                    except Exception as e:
                        if not failed:
                            raise
                        print(f"Workload cleanup failed: {e}", file=sys.stderr)
        elif a.command == "checks":
            node.configure(1000)
            result = experiments.checks(
                node,
                auth,
                binding,
                pin,
                a.gate_db,
                a.out,
                wrong_pin=a.wrong_pin,
                no_presence=a.no_presence,
                build_manifest=a.build_manifest,
                presence_mode="electrical_button_pulse" if a.gpio_pin is not None else "manual",
            )
            show(result)
            return int(any(r["status"] in ("FAIL", "ERROR") for r in result))
        else:
            path = experiments.collect(
                node,
                auth,
                binding,
                pin,
                a.gate_db,
                a.out,
                mode="latency" if a.command == "latency" else a.mode,
                trials=getattr(a, "trials", 0),
                warmup=getattr(a, "warmup", 0),
                duration=getattr(a, "duration", 60),
                rate=a.rate,
                work_rounds=a.work_rounds,
                threshold=a.threshold,
                build_manifest=a.build_manifest,
                presence_mode="electrical_button_pulse" if a.gpio_pin is not None else "manual",
            )
            print(f"Saved {path}")
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Run 'stop' if firmware remains active.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        if auth:
            auth.close()
        if presence:
            presence.close()
        if node:
            node.close()


if __name__ == "__main__":
    raise SystemExit(main())
