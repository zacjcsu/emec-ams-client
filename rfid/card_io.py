"""Low-level card access for temporary cards, ported from the bench-tested rc522_bench.py.

Sectors 0 and 1 hold the CSU data and are never written. The only trailer ever written is
<key A> FF 07 80 69 <factory key B>.
"""
import time

FACTORY = [0xFF] * 6
ACCESS = [0xFF, 0x07, 0x80, 0x69]   # transport-default access bytes + user byte: the ONLY ones we ever write


def uid_hex(uid):
    """The card UID as the server stores it: the first 4 bytes, upper-case hex."""
    return "".join("%02X" % b for b in uid[:4])


def data_block(sector):
    return sector * 4


def trailer_block(sector):
    return sector * 4 + 3


def make_trailer(key_a):
    """Sector trailer with the given Key A, the transport access bytes and the factory Key B."""
    t = list(key_a) + ACCESS + FACTORY
    assert len(key_a) == 6 and len(t) == 16 and t[6:10] == ACCESS, "refusing: malformed trailer"
    return t


class CardLost(Exception):
    pass


class CardIO:
    """Wraps the MFRC522 driver with the select / re-select dance the RC522 needs after any failed auth."""

    def __init__(self, mfrc522):
        self.r = mfrc522
        self.uid = None

    def select(self, wait=3):
        """Wait up to `wait` seconds for a card and select it. Returns the UID list or None."""
        end = time.time() + wait
        while True:
            status, _ = self.r.MFRC522_Request(self.r.PICC_REQIDL)
            if status == self.r.MI_OK:
                status, uid = self.r.MFRC522_Anticoll()
                if status == self.r.MI_OK:
                    self.r.MFRC522_SelectTag(uid)
                    self.uid = uid
                    return uid
            if time.time() >= end:
                return None
            time.sleep(0.05)

    def fresh(self, wait=3):
        """Drop crypto state and select again (required after every failed authentication)."""
        self.r.MFRC522_StopCrypto1()
        uid = self.select(wait)
        if uid is None:
            raise CardLost("card left the reader")
        return uid

    def auth(self, block, key):
        return self.r.MFRC522_Auth(self.r.PICC_AUTHENT1A, block, list(key), self.uid) == self.r.MI_OK

    def read(self, block):
        return self.r.MFRC522_Read(block)

    def write(self, block, data):
        assert len(data) == 16
        self.r.MFRC522_Write(block, list(data))   # the driver gives no status: always verify by reading back

    def is_blank(self):
        """True only if sectors 0 and 1 both open with the factory key. A CSU card refuses."""
        for sector in (0, 1):
            self.fresh()
            if not self.auth(trailer_block(sector), FACTORY):
                return False
        self.fresh()
        return True
