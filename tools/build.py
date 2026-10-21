#!/usr/bin/env python3
"""Build a firmware variant and save configuration and memory reports."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "weact-esp32s3-cdc": {
        "board": "weact_esp32s3_b/esp32s3/procpu",
        "file_stem": "weact_esp32s3_b_cdc",
        "required_symbols": (
            "ESP_SPIRAM",
            "SPIRAM_MODE_OCT",
            "INPUT_ESP32_TOUCH_SENSOR",
        ),
    },
    "blackpill-h523ce-cdc": {
        "board": "blackpill_h523ce/stm32h523xx",
        "file_stem": "blackpill_h523ce_cdc",
        "required_symbols": ("USE_DT_CODE_PARTITION",),
    },
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_sha(paths):
    digest = hashlib.sha256()
    for name, directory in paths:
        directory = Path(directory)
        for f in sorted(p for p in directory.rglob("*") if p.is_file()):
            digest.update(f"{name}/{f.relative_to(directory)}\0".encode())
            digest.update(f.read_bytes())
    return digest.hexdigest()


def run(args, cwd=None, log=None, report_failure=True):
    print("Running:", " ".join(str(x) for x in args), flush=True)
    result = subprocess.run(
        args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    if log:
        Path(log).write_text(result.stdout)
    if result.returncode:
        if report_failure:
            print(result.stdout, file=sys.stderr)
        raise RuntimeError(f"Command failed ({result.returncode}); log: {log}")
    return result.stdout


def root_bytes(report):
    clean = re.sub(r"\x1b\[[0-9;]*m", "", report)
    matches = re.findall(r"^\s*Root\s+(\d+)\s+100(?:\.0+)?%", clean, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(
            "Cannot identify one Root total in the Zephyr size report; inspect the saved report"
        )
    return int(matches[0])


def build_inputs(variant, profile, board_conf, overlay, sensor=False):
    configs = [ROOT / "firmware/prj.conf"]
    overlays = [Path(overlay)]
    if variant != "baseline":
        configs.append(ROOT / "firmware/configs/fido.conf")
        overlays.append(ROOT / "firmware/overlays/hid.overlay")
    configs.append(ROOT / f"firmware/configs/{variant}.conf")
    if profile in PROFILES:
        configs.append(ROOT / f"firmware/configs/{PROFILES[profile]['file_stem']}.conf")
    configs.append(Path(board_conf))
    if sensor:
        configs.append(ROOT / "firmware/configs/sensor.conf")
    return configs, overlays


def check_usb_ids(configs, usb_required):
    if not usb_required:
        return
    values = {}
    for file in configs:
        for line in Path(file).read_text().splitlines():
            match = re.fullmatch(
                r"\s*(CONFIG_BENCH_USB_(?:VID|PID))\s*=\s*(0x[0-9a-fA-F]+|[0-9]+)\s*(?:#.*)?", line
            )
            if match:
                values[match[1]] = int(match[2], 0)
    if any(
        not 0 < values.get(k, 0) <= 0xFFFF for k in ("CONFIG_BENCH_USB_VID", "CONFIG_BENCH_USB_PID")
    ):
        raise ValueError(
            "Set valid CONFIG_BENCH_USB_VID/PID values in board.conf. "
            "'fido-bench list' displays connected HID devices."
        )


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", choices=("generic", *PROFILES), default="generic")
    p.add_argument("--board", help="Board target; board profiles supply a default target")
    p.add_argument("--variant", required=True, choices=("baseline", "settings", "psa"))
    p.add_argument(
        "--board-overlay", type=Path, help="Optional replacement for the profile's common overlay"
    )
    p.add_argument(
        "--board-conf", required=True, type=Path, help="Common board parameters and USB IDs"
    )
    p.add_argument("--sensor", action="store_true")
    p.add_argument("--fido-priority", type=int, choices=range(15), default=7)
    p.add_argument(
        "--build-dir", required=True, type=Path, help="A new directory, never an existing build"
    )
    p.add_argument("--zephyr-base", type=Path, default=os.environ.get("ZEPHYR_BASE"))
    a = p.parse_args(argv)
    profile = PROFILES.get(a.profile)
    if profile:
        if not a.board:
            a.board = profile["board"]
        if not a.board_overlay:
            a.board_overlay = ROOT / f"firmware/overlays/{profile['file_stem']}.overlay"
    if not a.board or not a.board_overlay:
        p.error("The generic profile requires --board and --board-overlay")
    if not shutil.which("west"):
        p.error("west is unavailable. Activate a Zephyr build environment.")
    if not a.zephyr_base:
        p.error("Set ZEPHYR_BASE or --zephyr-base to a Zephyr checkout containing FIDO2")
    zephyr = a.zephyr_base.resolve()
    board_conf, overlay = a.board_conf.resolve(), a.board_overlay.resolve()
    for f in (board_conf, overlay, zephyr / "include/zephyr/authentication/fido2/fido2.h"):
        if not f.is_file():
            p.error(f"Required input not found: {f}")
    configs, overlays = build_inputs(a.variant, a.profile, board_conf, overlay, a.sensor)
    check_usb_ids(configs, a.variant != "baseline" or profile is not None)
    if a.variant == "psa":
        storage_kconfig = zephyr / "subsys/authentication/fido2/storage/Kconfig"
        if not storage_kconfig.is_file() or not re.search(
            r"^\s*config\s+FIDO2_STORAGE_PSA_PROTECTED_STORAGE\s*$",
            storage_kconfig.read_text(),
            re.MULTILINE,
        ):
            p.error(
                "This Zephyr revision lacks CONFIG_FIDO2_STORAGE_PSA_PROTECTED_STORAGE. "
                "Use a revision containing the native FIDO2 PSA Protected Storage backend."
            )
    build = a.build_dir.resolve()
    if build.exists():
        p.error("Use a new --build-dir to preserve previous configuration and measurements")
    build.mkdir(parents=True)
    archive = build / "bench-artifacts"
    archive.mkdir()
    build_id = secrets.token_hex(16)
    overrides = archive / "measurement.conf"
    overrides.write_text(
        f'CONFIG_BENCH_BUILD_ID="{build_id}"\n'
        + (f"CONFIG_FIDO2_THREAD_PRIORITY={a.fido_priority}\n" if a.variant != "baseline" else "")
    )
    configs.append(overrides)
    shutil.copyfile(board_conf, archive / "board.conf")
    shutil.copyfile(overlay, archive / "board.overlay")
    command = [
        "west",
        "build",
        "-b",
        a.board,
        "-d",
        str(build),
        str(ROOT / "firmware"),
        "--",
        f"-DZEPHYR_BASE={zephyr}",
        "-DCONF_FILE=" + ";".join(str(f) for f in configs),
        "-DDTC_OVERLAY_FILE=" + ";".join(str(f) for f in overlays),
    ]
    run(command, cwd=zephyr, log=archive / "build.log")
    config = (build / "zephyr/.config").read_text()
    if "CONFIG_BUILD_WITH_TFM=y" in config:
        raise RuntimeError(
            "TF-M builds require multi-image memory accounting and are not supported by this script."
        )
    if (
        a.variant == "psa"
        and "CONFIG_FIDO2_STORAGE_PSA_PROTECTED_STORAGE=y" not in config.splitlines()
    ):
        raise RuntimeError(
            "The PSA variant did not select Zephyr's native FIDO2 PSA Protected Storage backend"
        )
    if a.variant != "baseline" and "CONFIG_FIDO2_UP_CUSTOM=y" not in config.splitlines():
        raise RuntimeError("The measurement build requires the instrumented custom UP backend")
    if profile:
        for symbol in (
            "BENCH_CONTROL_CDC_ACM",
            "USB_DEVICE_STACK_NEXT",
            "USBD_CDC_ACM_CLASS",
            *profile["required_symbols"],
        ):
            expected = f"CONFIG_{symbol}=y"
            if expected not in config.splitlines():
                raise RuntimeError(f"Profile setting missing from actual .config: {expected}")
    rom = run(
        ["west", "build", "-d", str(build), "-t", "rom_report"],
        cwd=zephyr,
        log=archive / "rom_report.txt",
    )
    ram = run(
        ["west", "build", "-d", str(build), "-t", "ram_report"],
        cwd=zephyr,
        log=archive / "ram_report.txt",
    )
    # Archive the resolved build configuration and link map.
    for name in (".config", "zephyr.dts", "zephyr.map"):
        source = build / "zephyr" / name
        if source.exists():
            shutil.copyfile(source, archive / name.lstrip("."))
    version = run(["git", "rev-parse", "HEAD"], cwd=zephyr).strip()
    git_diff = run(["git", "diff", "HEAD", "--binary"], cwd=zephyr)
    (archive / "zephyr.patch").write_text(git_diff)
    core = zephyr / "subsys/authentication/fido2"
    headers = zephyr / "include/zephyr/authentication/fido2"
    manifest = {
        "schema": 1,
        "build_id": build_id,
        "measurement_schema": 3,
        "variant": "psa-ps" if a.variant == "psa" else a.variant,
        "profile": a.profile,
        "board": a.board,
        "zephyr_commit": version,
        "zephyr_diff_sha256": hashlib.sha256(git_diff.encode()).hexdigest(),
        "subsystem_sha256": tree_sha([("core", core), ("headers", headers)]),
        "harness_firmware_sha256": tree_sha([("firmware", ROOT / "firmware")]),
        "board_conf_sha256": sha(board_conf),
        "board_overlay_sha256": sha(overlay),
        "workload": "sensor" if a.sensor else "synthetic",
        "build_command": command,
        "west_version": run(["west", "--version"]).strip(),
        "config_sha256": sha(build / "zephyr/.config"),
        "elf_sha256": sha(build / "zephyr/zephyr.elf"),
        "rom_bytes": root_bytes(rom),
        "static_ram_bytes": root_bytes(ram),
        "psa_provider": "zephyr_secure_storage" if "CONFIG_SECURE_STORAGE=y" in config else "none",
        "control_transport": (
            "usb_cdc_acm" if "CONFIG_BENCH_CONTROL_CDC_ACM=y" in config else "physical_uart"
        ),
        "spiram_enabled": "CONFIG_ESP_SPIRAM=y" in config,
        "touch_enabled": "CONFIG_INPUT_ESP32_TOUCH_SENSOR=y" in config,
        "accounting": "Zephyr ROM and static RAM totals; persistent partition reservations excluded",
    }
    for key, symbol in (
        ("work_priority", "BENCH_WORK_PRIORITY"),
        ("default_work_rounds", "BENCH_WORK_ROUNDS"),
        ("period_ms", "BENCH_PERIOD_MS"),
        ("deadline_ms", "BENCH_DEADLINE_MS"),
        ("fido_priority", "FIDO2_THREAD_PRIORITY"),
    ):
        match = re.search(rf"^CONFIG_{symbol}=(\d+)$", config, re.MULTILINE)
        manifest[key] = int(match[1]) if match else None
    if a.variant != "baseline" and manifest["fido_priority"] != a.fido_priority:
        raise RuntimeError("Resolved FIDO2 priority differs from requested priority")
    if f'CONFIG_BENCH_BUILD_ID="{build_id}"' not in config.splitlines():
        raise RuntimeError("Build identity is missing from the resolved configuration")
    cache = build / "CMakeCache.txt"
    if cache.exists():
        (archive / "toolchain-cache.txt").write_text(
            "\n".join(
                line
                for line in cache.read_text().splitlines()
                if any(
                    k in line
                    for k in ("CMAKE_C_COMPILER:", "ZEPHYR_SDK", "TOOLCHAIN_VARIANT", "BOARD:")
                )
            )
        )
    try:
        frozen = run(
            ["west", "manifest", "--freeze", "--active-only"],
            cwd=zephyr,
            log=archive / "west-manifest.log",
            report_failure=False,
        )
        (archive / "west-manifest.yml").write_text(frozen)
    except RuntimeError:
        manifest["west_manifest_warning"] = (
            "Module revision snapshot unavailable; see west-manifest.log"
        )
        print(
            f"Warning: build succeeded, but module revisions could not be saved; "
            f"see {archive / 'west-manifest.log'}",
            file=sys.stderr,
        )
    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
