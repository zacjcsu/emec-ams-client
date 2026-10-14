"""Reads a Yaskawa V1000 drive over RS-485 Modbus RTU and logs it to logs/machine_data.log.

The drive answers on terminals R+ R- S+ S- (jumpered to S+ and S- for 2-wire RS-485) with its factory settings:
address H5-01 = 1F, 9600 baud (H5-02 = 3), no parity (H5-03 = 0). Set H5-05 to 0, or the drive shows a CE alarm when
the Pi stops asking. Registers and codes are from the V1000 Technical Manual (SIEP C710606 18F), Appendix C.

`running` is what utils/power_monitor.py records as the spindle being on. A drive that stops answering counts as off,
which is also what a drive without power looks like.

Set it up in config.json:
    "machine_data": {"control": "yaskawa-v1000", "device": "/dev/serial/by-id/usb-FTDI_...", "address": 31}
It also takes "baud" and "parity" ("none", "even" or "odd"), which must match H5-02 and H5-03.
"""
import logging
import threading
import time

from utils.haas_mdc import SNAPSHOT_SECONDS, SerialClient, data_log

logger = logging.getLogger("vfd")

ANSWER_SECONDS = 0.5
RETRY_SECONDS = 10
MISSES = 3   # rounds without an answer before the drive counts as gone

# Table C.4, the fault codes in U2-01 (0080H) and U2-02 (0081H)
FAULTS = {
    0x02: "Uv1", 0x03: "Uv2", 0x04: "Uv3", 0x05: "SC", 0x06: "GF", 0x07: "oC", 0x08: "ov", 0x09: "oH", 0x0A: "oH1",
    0x0B: "oL1", 0x0C: "oL2", 0x0D: "oL3", 0x0E: "oL4", 0x0F: "rr", 0x10: "rH", 0x11: "EF3", 0x12: "EF4", 0x13: "EF5",
    0x14: "EF6", 0x15: "EF7", 0x18: "oS", 0x19: "dEv", 0x1A: "PGo", 0x1B: "PF", 0x1C: "LF", 0x1D: "oH3", 0x1E: "oPr",
    0x1F: "Err", 0x20: "oH4", 0x21: "CE", 0x22: "bUS", 0x25: "CF", 0x27: "EF0", 0x28: "FbL", 0x29: "UL3", 0x2A: "UL4",
    0x2B: "oL7", 0x30: "oFx", 0x36: "LF2", 0x37: "STo", 0x38: "PGo", 0x39: "E5", 0x3B: "SEr", 0x41: "FbH", 0x42: "EF1",
    0x43: "EF2", 0x44: "oL5", 0x45: "UL5", 0x46: "CoF", 0x49: "dWFL", 0x83: "CPF02", 0x84: "CPF03", 0x87: "CPF06",
    0x88: "CPF07", 0x89: "CPF08", 0x8C: "CPF11", 0x8D: "CPF12", 0x8E: "CPF13", 0x8F: "CPF14", 0x91: "CPF16",
    0x92: "CPF17", 0x93: "CPF18", 0x94: "CPF19", 0x95: "CPF20", 0x96: "CPF21", 0x97: "CPF22", 0x98: "CPF23",
    0x99: "CPF24", 0x101: "oFA00", 0x102: "oFA01", 0x104: "oFA03", 0x105: "oFA04",
}
# Table C.5, the alarm codes in 007FH
ALARMS = {
    0x01: "Uv", 0x02: "ov", 0x03: "oH", 0x04: "oH2", 0x05: "oL3", 0x06: "oL4", 0x07: "EF", 0x08: "bb", 0x09: "EF3",
    0x0A: "EF4", 0x0B: "EF5", 0x0C: "EF6", 0x0D: "EF7", 0x10: "oS", 0x11: "dEv", 0x12: "PGo", 0x13: "oPr", 0x14: "CE",
    0x15: "bUS", 0x16: "CALL", 0x17: "oL1", 0x18: "oL2", 0x1A: "EF0", 0x1B: "rUn", 0x1D: "CALL", 0x1E: "UL3",
    0x1F: "UL4", 0x20: "SE", 0x22: "oH3", 0x27: "FbL", 0x28: "FbH", 0x2A: "dnE", 0x2B: "PGo", 0x31: "E5", 0x32: "AEr",
    0x33: "CyC", 0x34: "HCA", 0x39: "EF1", 0x3A: "EF2", 0x3B: "HbbF", 0x3C: "Hbb", 0x3D: "oL5", 0x3E: "UL5",
    0x49: "dWAL",
}


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def code(table, value):
    return "none" if value == 0 else table.get(value, f"{value:04X}H")


class ModbusPort:
    """Function 03 over a serial port. Not thread safe."""

    def __init__(self, client, address):
        self.client = client
        self.address = int(address)
        self.name = f"{client.name} address {self.address}"

    def read(self, start, count):
        """`count` registers from `start`. Raises OSError without a good answer."""
        frame = bytes([self.address, 3, start >> 8, start & 0xFF, 0, count])
        self.client._drain()
        self.client._send(frame + crc16(frame).to_bytes(2, "little"))
        want = 5 + 2 * count
        raw = b""
        deadline = time.monotonic() + ANSWER_SECONDS
        while len(raw) < want and time.monotonic() < deadline:
            raw += self.client._recv(max(0.01, deadline - time.monotonic()))
            if len(raw) >= 5 and raw[1] == 0x83:   # an error answer is 5 bytes
                break
        if not raw:
            raise ConnectionError("no answer")
        if len(raw) >= 5 and raw[1] == 0x83 and crc16(raw[:5]) == 0:
            raise OSError(f"error code {raw[2]:02X}H reading {start:04X}H")
        if len(raw) < want or raw[0] != self.address or raw[1] != 3 or raw[2] != 2 * count or crc16(raw[:want]):
            raise OSError(f"bad answer {raw.hex()}")
        return [int.from_bytes(raw[3 + 2 * i:5 + 2 * i], "big") for i in range(count)]


class V1000Monitor:
    """Polls the drive in its own thread. `values` holds the last reading of each value, as logged."""

    def __init__(self, device, address=0x1F, baud=9600, parity="none", poll_seconds=1, log=None):
        self.port = ModbusPort(SerialClient(device, baud, parity, 8, 1, xonxoff=False), address)
        self.poll_seconds = float(poll_seconds)
        self.log = log or data_log()
        self.values = {}
        self.connected = False
        self.running = False
        self._failing = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vfd", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _fast(self):
        r = self.port.read(0x0020, 13)          # 0020H to 002CH
        alarm, fault = self.port.read(0x007F, 2)
        status = r[0]
        return {
            "run": "on" if status & 1 else "off",
            "direction": "reverse" if status & 2 else "forward",
            "ready": "yes" if status & 4 else "no",
            "fault": code(FAULTS, fault) if status & 8 else "none",
            "alarm": code(ALARMS, alarm),
            "reference Hz": f"{r[3] / 100:.1f}",
            "output Hz": f"{r[4] / 100:.1f}",
            "voltage V": f"{r[5] / 10:.0f}",
            "current A": f"{r[6] / 100:.1f}",   # 0.01 A on 2A0001 to 2A0040, 0.1 A on larger drives
            "power": str(r[7]),
            "inputs": " ".join(f"S{i + 1}" for i in range(7) if r[11] >> i & 1) or "none",
        }

    def _slow(self):
        dc_bus, = self.port.read(0x0031, 1)
        heatsink, = self.port.read(0x0068, 1)
        tens, ones = self.port.read(0x0098, 2)
        kwh, mwh = self.port.read(0x005C, 2)
        previous, = self.port.read(0x0081, 1)
        return {"DC bus V": str(dc_bus), "heatsink C": str(heatsink), "run hours": str(tens * 10 + ones),
                "kWh": f"{mwh * 1000 + kwh / 10:.1f}", "previous fault": code(FAULTS, previous)}

    def _record(self, values, always=False):
        for name, value in values.items():
            if always or self.values.get(name) != value:
                self.log.info(f"{name} = {value!r}" + ("  (snapshot)" if always else ""))
            if name == "fault" and value != "none" and self.values.get(name) != value:
                logger.warning(f"[VFD] Drive fault {value}.")
            elif name == "alarm" and value != "none" and self.values.get(name) != value:
                logger.info(f"[VFD] Drive alarm {value}.")
            self.values[name] = value
        self.running = self.values.get("run") == "on"

    def _run(self):
        # Connection changes go to sync.log at INFO: a drive whose power the relay cuts is off whenever nobody's on.
        snapshot_at, misses = 0.0, 0
        while not self._stop.is_set():
            try:
                if self.port.client.fd is None:
                    self.port.client.connect()
                values = self._fast()
                if not self.connected:
                    self.connected, self._failing = True, False
                    logger.info(f"[VFD] Connected to {self.port.name}.")
                    self.log.info(f"connected to {self.port.name}")
                    snapshot_at = 0.0
                misses = 0
                if time.monotonic() >= snapshot_at:
                    values.update(self._slow())
                    self._record(values, always=True)
                    snapshot_at = time.monotonic() + SNAPSHOT_SECONDS
                else:
                    self._record(values)
                self._stop.wait(self.poll_seconds)
            except OSError as e:
                misses += 1
                if self.connected and misses < MISSES:
                    self._stop.wait(self.poll_seconds)
                    continue
                if self.connected:
                    logger.info(f"[VFD] Lost the drive: {e}.")
                    self.log.info(f"disconnected: {e}")
                elif not self._failing:
                    logger.info(f"[VFD] Cannot reach {self.port.name}: {e}.")
                self.connected, self._failing, self.running = False, True, False
                self.values = {}
                # A drive without power keeps being asked, so it's seen as soon as it's back. A missing adapter is
                # opened again later.
                if e.errno is not None or "adapter" in str(e):
                    self.port.client.close()
                self._stop.wait(RETRY_SECONDS if self.port.client.fd is None else self.poll_seconds)
