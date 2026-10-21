"""Serial control over USB CDC ACM or UART."""

from __future__ import annotations
import json
import threading
import time
import serial


class Node:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 5, dtr: bool = True):
        self.serial = serial.Serial(port=None, baudrate=baud, timeout=0.1, write_timeout=timeout)
        # CDC requires DTR; physical UART defaults to deasserted control lines.
        self.serial.dtr = dtr
        self.serial.rts = False
        self.serial.port = port
        self.serial.open()
        self.timeout, self.sequence = timeout, 0
        self.lock = threading.Lock()

    def close(self):
        self.serial.close()

    def command(self, command: str) -> dict:
        if "\n" in command or "\r" in command:
            raise ValueError("Command must be one line")
        with self.lock:
            self.sequence += 1
            prefix = f"@{self.sequence} "
            try:
                self.serial.write((prefix + command + "\n").encode("ascii"))
                self.serial.flush()
            except serial.SerialTimeoutException as e:
                raise serial.SerialTimeoutException(
                    f"Write timed out sending {command.split()[0]} on {self.serial.port}; "
                    "command was not retried"
                ) from e
            deadline = time.monotonic() + self.timeout
            # Buffer partial serial reads until a complete line arrives.
            buffer = bytearray()
            while time.monotonic() < deadline:
                buffer.extend(self.serial.read(self.serial.in_waiting or 1))
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    line = raw.decode("ascii", errors="replace").strip()
                    if not line.startswith(prefix):
                        continue
                    result = json.loads(line[len(prefix) :])
                    if not result.get("ok"):
                        raise RuntimeError(f"Node rejected {command.split()[0]}: {result}")
                    return result
                if len(buffer) > 32768:
                    raise RuntimeError("Unframed/oversized serial response")
            raise TimeoutError(
                f"No matching reply to {command.split()[0]}; command was not retried"
            )

    def hello(self):
        return self.command("HELLO")

    def status(self):
        return self.command("STATUS")

    def up(self):
        return self.command("UP")

    def apply(self, nonce: str, threshold: int):
        if len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
            raise ValueError("Invalid request nonce")
        return self.command(f"APPLY {nonce} {threshold}")

    def start(self, duration_ms: int):
        return self.command(f"START {duration_ms}")

    def stop(self):
        return self.command("STOP")

    def stats(self):
        return self.command("STATS")

    def configure(self, rounds: int):
        if not 0 <= rounds <= 10000000:
            raise ValueError("Work rounds must be in [0,10000000]")
        return self.command(f"WORK {rounds}")

    def samples(self, stats):
        rows = []
        count = stats["captured_samples"]
        while len(rows) < count:
            page = self.command(f"SAMPLES {len(rows)}")
            if page["run_id"] != stats["run_id"] or page["offset"] != len(rows):
                raise RuntimeError("Sample buffer belongs to a different run")
            if not page["samples"]:
                raise RuntimeError("Incomplete sample transfer")
            rows.extend(page["samples"])
        if len(rows) != count:
            raise RuntimeError("Sample count mismatch")
        return rows
