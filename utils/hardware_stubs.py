# Off Linux (Windows/macOS dev machines), registers no-op fakes in sys.modules
# for RPi.GPIO / smbus / smbus2 / mfrc522 so the app can be imported and run
# without hardware. Must be imported before any of those modules.

import logging
import os
import sys
import types

logger = logging.getLogger("hardware_stubs")


def _install_fake_gpio():
    try:
        import RPi.GPIO  # noqa: F401
        return
    except ImportError:
        pass

    gpio = types.ModuleType("RPi.GPIO")
    gpio.BOARD = "BOARD"
    gpio.BCM = "BCM"
    gpio.OUT = "OUT"
    gpio.IN = "IN"
    gpio.PUD_UP = "PUD_UP"
    gpio.HIGH = 1
    gpio.LOW = 0

    def _log_call(name):
        def fn(*args, **kwargs):
            logger.debug(f"[fake RPi.GPIO] {name}{args}")
        return fn

    for name in ("setwarnings", "setmode", "setup", "output", "cleanup"):
        setattr(gpio, name, _log_call(name))
    gpio.input = lambda *a, **k: gpio.LOW

    rpi = types.ModuleType("RPi")
    rpi.GPIO = gpio
    sys.modules["RPi"] = rpi
    sys.modules["RPi.GPIO"] = gpio
    logger.warning("RPi.GPIO not available; using no-op fake (relay/GPIO calls will do nothing).")


def _install_fake_smbus():
    for name in ("smbus", "smbus2"):
        try:
            __import__(name)
            continue
        except ImportError:
            pass

        mod = types.ModuleType(name)

        class FakeSMBus:
            def __init__(self, *args, **kwargs):
                pass

            def write_byte_data(self, addr, reg, data):
                logger.debug(f"[fake {name}] write_byte_data(0x{addr:02x}, 0x{reg:02x}, 0x{data:02x})")

            def close(self):
                pass

        mod.SMBus = FakeSMBus
        sys.modules[name] = mod
        logger.warning(f"{name} not available; using no-op fake (LCD writes will do nothing).")


def _install_fake_mfrc522():
    try:
        import mfrc522  # noqa: F401
        return
    except ImportError:
        pass

    mod = types.ModuleType("mfrc522")

    class FakeMFRC522:
        def __init__(self, *args, **kwargs):
            pass

        MI_OK = 0
        MI_NOTAGERR = 1
        MI_ERR = 2
        PICC_REQIDL = 0x26
        PICC_AUTHENT1A = 0x60

        def MFRC522_Request(self, req_mode):
            return self.MI_NOTAGERR, None

        def MFRC522_Anticoll(self):
            return self.MI_NOTAGERR, None

        def MFRC522_SelectTag(self, uid):
            return self.MI_OK

        def MFRC522_Auth(self, mode, addr, key, uid):
            return self.MI_ERR

        def MFRC522_Read(self, addr):
            return None

        def MFRC522_StopCrypto1(self):
            pass

    mod.MFRC522 = FakeMFRC522
    sys.modules["mfrc522"] = mod
    logger.warning("mfrc522 not available; using no-op fake (no card will ever be detected).")


def install():
    if sys.platform.startswith("linux") and os.getenv("EMEC_HARDWARE_STUBS") != "1":
        logger.debug(
            "Linux detected; not installing hardware stubs. A missing hardware "
            "module will raise ImportError, which is intended."
        )
        return
    _install_fake_gpio()
    _install_fake_smbus()
    _install_fake_mfrc522()


install()
