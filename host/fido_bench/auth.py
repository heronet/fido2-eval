"""Direct CTAP communication using python-fido2."""

from __future__ import annotations
import hashlib
import secrets
import threading
import time
from fido2 import cbor
from fido2.ctap2 import Ctap2
from fido2.ctap2.pin import ClientPin
from fido2.hid import CtapHidDevice
from .gate import Binding, Evidence


class Presence:
    def __init__(
        self, gpio_pin: int | None = None, gpio_chip: int = 0, pulse_ms: int = 80, prompt=True
    ):
        self.pin, self.worker, self.fired = None, None, False
        self.prompt = prompt
        self.pulse = pulse_ms / 1000
        if gpio_pin is not None:
            from gpiozero import OutputDevice
            from gpiozero.pins.lgpio import LGPIOFactory

            self.pin = OutputDevice(
                gpio_pin,
                active_high=True,
                initial_value=False,
                pin_factory=LGPIOFactory(chip=gpio_chip),
            )

    def arm(self):
        if self.worker:
            self.worker.join()
        self.fired = False

    def on_keepalive(self, status):
        if int(status) == 2 and not self.fired:
            self.fired = True
            if self.pin is None:
                if self.prompt:
                    import sys

                    print("Touch the node's user-presence button now.", file=sys.stderr)
                return

            # Pulse outside the HID receive callback.
            def pulse():
                self.pin.on()
                try:
                    time.sleep(self.pulse)
                finally:
                    self.pin.off()

            self.worker = threading.Thread(target=pulse, daemon=True)
            self.worker.start()

    def close(self):
        if self.worker:
            self.worker.join()
        if self.pin:
            self.pin.close()


class Authenticator:
    def __init__(self, presence: Presence, hid_path: str | None = None, timeout: float = 45):
        devices = list(CtapHidDevice.list_devices())
        if hid_path:
            selected = [d for d in devices if self.path(d) == hid_path]
        else:
            selected = devices
        if len(selected) != 1:
            choices = [self.path(d) for d in devices]
            for d in devices:
                d.close()
            raise RuntimeError(f"Select exactly one authenticator with --hid-path; found {choices}")
        self.device = selected[0]
        for d in devices:
            if d is not self.device:
                d.close()
        self.ctap = Ctap2(self.device)
        self.pin = ClientPin(self.ctap)
        self.presence, self.timeout = presence, timeout

    @staticmethod
    def path(device):
        path = device.descriptor.path
        return path.decode(errors="surrogateescape") if isinstance(path, bytes) else str(path)

    def close(self):
        self.device.close()

    def set_pin(self, pin):
        self.pin.set_pin(pin)

    def token(self, pin, permission, rp_id):
        return self.pin.get_pin_token(pin, permissions=permission, permissions_rpid=rp_id)

    def _call(self, function, **kwargs):
        event = threading.Event()
        timer = threading.Timer(self.timeout, event.set)
        self.presence.arm()
        timer.start()
        try:
            return function(event=event, on_keepalive=self.presence.on_keepalive, **kwargs)
        finally:
            timer.cancel()

    def enroll(self, node_id, rp_id, pin):
        digest = hashlib.sha256(secrets.token_bytes(32)).digest()
        token = self.token(pin, ClientPin.PERMISSION.MAKE_CREDENTIAL, rp_id)
        result = self._call(
            self.ctap.make_credential,
            client_data_hash=digest,
            rp={"id": rp_id, "name": "Offline maintenance bench"},
            user={"id": secrets.token_bytes(16), "name": node_id, "displayName": node_id},
            key_params=[{"type": "public-key", "alg": -7}],
            options={"rk": True},
            pin_uv_param=self.pin.protocol.authenticate(token, digest),
            pin_uv_protocol=self.pin.protocol.VERSION,
        )
        data = result.auth_data
        if data.rp_id_hash != hashlib.sha256(rp_id.encode()).digest():
            raise RuntimeError("Enrollment RP mismatch")
        if not data.is_user_present() or not data.is_user_verified() or not data.credential_data:
            raise RuntimeError("Enrollment requires UP, UV, and credential data")
        cred = data.credential_data
        if cred.public_key.get(3) != -7:
            raise RuntimeError("This suite expects ES256")
        # Manufacturer attestation is not verified during enrollment.
        return Binding(node_id, rp_id, cred.credential_id, cbor.encode(dict(cred.public_key)))

    def assert_request(
        self, binding: Binding, digest: bytes, pin: str, require_up=True, with_pin=True
    ):
        start = time.perf_counter_ns()
        params = {}
        if with_pin:
            token = self.token(pin, ClientPin.PERMISSION.GET_ASSERTION, binding.rp_id)
            params = {
                "pin_uv_param": self.pin.protocol.authenticate(token, digest),
                "pin_uv_protocol": self.pin.protocol.VERSION,
            }
        token_done = time.perf_counter_ns()
        result = self._call(
            self.ctap.get_assertion,
            rp_id=binding.rp_id,
            client_data_hash=digest,
            allow_list=[{"type": "public-key", "id": binding.credential_id}],
            options={"up": require_up},
            **params,
        )
        assertion_done = time.perf_counter_ns()
        cid = result.credential["id"] if result.credential else binding.credential_id
        evidence = Evidence(cid, bytes(result.auth_data), result.signature)
        return evidence, {
            "pin_exchange_ns": token_done - start,
            "assertion_ns": assertion_done - token_done,
            "ctap_total_ns": assertion_done - start,
        }
