from mfrc522 import MFRC522
import RPi.GPIO as GPIO
import time
import logging
from collections import namedtuple
from config.constants import CARD_POLL_INTERVAL, READER_CHECK_SECONDS
from rfid.card_io import uid_hex

logger = logging.getLogger("rfid")

AUTH_KEY = [0x4A, 0x1E, 0xD9, 0x40, 0xF4, 0x4B]  # CSU card sector key
SECTOR = 1  # Sector containing CSU ID

class CardScan(namedtuple("CardScan", "uid uid_hex uid_num csu_id")):
    __slots__ = ()


class RFIDReader:
    def __init__(self, leds=None):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)
        self.leds = leds
        # The driver logs every refused key as an error. Non-CSU cards and temp card writes get refused keys on purpose.
        self.reader = MFRC522(pin_rst=22, debugLevel="CRITICAL")
        self._next_check = time.monotonic() + READER_CHECK_SECONDS
        self._down = False

    def check_chip(self):
        """Set the chip up again if it has reset itself. A spike when the relay switches can do that. A reset chip
        has its antenna off, so it sees no cards and raises no error."""
        if time.monotonic() < self._next_check:
            return
        self._next_check = time.monotonic() + READER_CHECK_SECONDS
        r = self.reader
        tx, tmode = r.Read_MFRC522(r.TxControlReg), r.Read_MFRC522(r.TModeReg)
        if tx & 0x03 == 0x03 and tmode == 0x8D:
            if self._down:
                logger.info("[RFID] Reader answers again")
                self._down = False
            return
        if not self._down:
            logger.warning(f"[RFID] Reader had reset (TxControl {tx:#04x}, TMode {tmode:#04x}); setting it up again")
        r.MFRC522_Init()
        # Logged once while a dead or unplugged chip keeps failing.
        self._down = r.Read_MFRC522(r.TModeReg) != 0x8D

    def uid_to_number(self, uid):
        num = 0
        for byte in uid:
            num = num * 256 + byte
        return num

    def read_card_ex(self):
        """Detect a card and return a CardScan, or None if nothing is on the reader.

        `csu_id` is None when the card is present but is not a student card (sector 1 does not open
        with the CSU key): a blank, a temporary card or anything else. The UID is still returned so
        the caller can look the card up.
        """
        (status, uid) = self.reader.MFRC522_Request(self.reader.PICC_REQIDL)
        if status != self.reader.MI_OK:
            self.check_chip()
            return None

        (status, uid) = self.reader.MFRC522_Anticoll()
        if status != self.reader.MI_OK:
            return None

        # Before the auth attempt, so a rejected card still lights D1 and
        # "the reader never saw it" is distinguishable from "it was refused".
        if self.leds:
            self.leds.reader_blink()

        uid_num = self.uid_to_number(uid)
        scan = CardScan(list(uid), uid_hex(uid), uid_num, None)

        self.reader.MFRC522_SelectTag(uid)
        block_addr = SECTOR * 4

        status = self.reader.MFRC522_Auth(self.reader.PICC_AUTHENT1A, block_addr, AUTH_KEY, uid)
        if status != self.reader.MI_OK:
            # Every poll of a resting non-student card lands here, so this is not a warning.
            logger.debug("[RFID] CSU authentication failed (not a student card)")
            return scan

        data = self.reader.MFRC522_Read(block_addr)
        self.reader.MFRC522_StopCrypto1()

        if not data:
            logger.warning("[RFID] Failed to read data block")
            return scan

        trimmed = data[3:8]
        csu_id = int.from_bytes(trimmed, byteorder='big') // 10

        logger.info(f"[RFID] Card scanned - UID: {uid_num}, CSU ID: {csu_id}")
        return CardScan(scan.uid, scan.uid_hex, uid_num, csu_id)

    def wait_until_removed(self, poll=CARD_POLL_INTERVAL, max_seconds=None, recheck=None, recheck_every=2.0):
        """Block until no card has been seen for 3 polls in a row (a single missed read is common), leaving the LCD
        alone. Gives up after `max_seconds` if set. If `recheck` is given it is called every `recheck_every` seconds
        while the card is there; when it returns True, stop. Returns True if it stopped for `recheck`."""
        end = time.time() + max_seconds if max_seconds else None
        misses = 0
        last = time.monotonic()
        while misses < 3 and (end is None or time.time() < end):
            misses = misses + 1 if self.read_card_ex() is None else 0
            time.sleep(poll)
            if recheck and misses == 0 and time.monotonic() - last >= recheck_every:
                last = time.monotonic()
                if recheck():
                    return True
        return False

    def cleanup(self):
        GPIO.cleanup()
