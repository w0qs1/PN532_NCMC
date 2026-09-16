#!/usr/bin/env python3
"""
NCMC / EMV Explorer (Simplified Output Version)

Flow:
    1. Initialize PN532 over HSU/UART.
    2. Poll ISO14443-A card.
    3. SELECT PPSE -> Read Application Label (Card Type: RuPay Debit/Prepaid).
    4. SELECT AID -> GPO with PDOL -> Extract Tag DF33 Service Balance.
    5. READ Card Information -> PAN (Masked) & Expiry Date.
    6. READ SFI 16 (Log Records) -> Decode Common Service Area & Language.

Dependencies:
    pip install pyserial

Usage:
    python3 get_balance.py
    python3 get_balance.py --port /dev/ttyUSB0 --debug
"""

import argparse
from datetime import datetime, timedelta
import time
from typing import Any

import serial


# ---------------------------------------------------------------------------
# Language Lookup Table (5-bit: bits 7..3 of CSA Byte 1)
# ---------------------------------------------------------------------------
LANG_MAP = {
    0: "English",
    1: "Hindi",
    2: "Bengali",
    3: "Marathi",
    4: "Telugu",
    5: "Tamil",
    6: "Gujarati",
    7: "Urdu",
    8: "Kannada",
    9: "Odia",
    10: "Malayalam",
    11: "Punjabi",
    12: "Sanskrit",
    13: "Assamese",
    14: "Maithili",
    15: "Santali",
    16: "Kashmiri",
    17: "Nepali",
    18: "Sindhi",
    19: "Dogri",
    20: "Konkani",
    21: "Manipuri",
    22: "Bodo",
}


def decode_language(lang_byte: int) -> str:
    lang_code = (lang_byte >> 3) & 0x1F
    return LANG_MAP.get(lang_code, f"RFU ({lang_code})")


# ---------------------------------------------------------------------------
# Basic Helpers & BER-TLV Parser
# ---------------------------------------------------------------------------

def hx(data: bytes) -> str:
    return data.hex(" ").upper()


def build_apdu(
    cla: int,
    ins: int,
    p1: int,
    p2: int,
    data: bytes = b"",
    le: int = 0,
) -> bytes:
    """Build short APDU byte sequence."""
    apdu = bytes([cla, ins, p1, p2])

    if data:
        if len(data) > 255:
            raise ValueError("Short APDU data is limited to 255 bytes")
        apdu += bytes([len(data)]) + data

    if le is not None:
        apdu += bytes([le & 0xFF])

    return apdu


def parse_sw(response: bytes) -> int:
    if len(response) < 2:
        raise ValueError("Response shorter than SW1/SW2")
    return int.from_bytes(response[-2:], "big")


def is_constructed(tag: int) -> bool:
    first_byte = tag
    while first_byte > 0xFF:
        first_byte >>= 8
    return bool(first_byte & 0x20)


def parse_tlv(data: bytes) -> list[tuple[int, bytes]]:
    out = []
    off = 0

    while off < len(data):
        start = off

        # Tag
        tag = data[off]
        off += 1

        if (tag & 0x1F) == 0x1F:
            while True:
                if off >= len(data):
                    raise ValueError("Truncated multi-byte tag")
                b = data[off]
                off += 1
                tag = (tag << 8) | b
                if not (b & 0x80):
                    break

        if off >= len(data):
            raise ValueError("Missing TLV length")

        # Length
        length = data[off]
        off += 1

        if length & 0x80:
            n = length & 0x7F
            if n == 0:
                raise ValueError("Indefinite BER length not supported")
            if off + n > len(data):
                raise ValueError("Truncated long-form length")
            length = int.from_bytes(data[off:off + n], "big")
            off += n

        if off + length > len(data):
            raise ValueError(
                f"TLV at offset {start} extends beyond response"
            )

        value = data[off:off + length]
        off += length
        out.append((tag, value))

    return out


def find_tag(data: bytes, wanted: int) -> list[bytes]:
    found = []

    try:
        tlvs = parse_tlv(data)
    except ValueError:
        return found

    for tag, value in tlvs:
        if tag == wanted:
            found.append(value)

        if is_constructed(tag):
            found.extend(find_tag(value, wanted))

    return found


def parse_bcd_date(b: bytes) -> tuple[str, datetime | None]:
    hex_str = b.hex().upper()
    if len(hex_str) >= 6:
        yy, mm, dd = int(hex_str[0:2]), int(hex_str[2:4]), int(hex_str[4:6])
        year = 2000 + yy if yy < 80 else 1900 + yy
        dt = datetime(year, mm, dd, 0, 0, 0)
        return dt.strftime("%Y-%m-%d"), dt
    elif len(hex_str) == 4:
        yy, mm = int(hex_str[0:2]), int(hex_str[2:4])
        year = 2000 + yy if yy < 80 else 1900 + yy
        return f"{year:04d}-{mm:02d}", None
    return hex_str, None


def mask_pan(pan: str) -> str:
    """Mask PAN to keep first 6 digits and last 4 digits."""
    digits = "".join(c for c in pan if c.isdigit())
    if len(digits) >= 10:
        return digits[:6] + "*" * (len(digits) - 10) + digits[-4:]
    return pan


def parse_df33_balance(df33_data: bytes) -> float | None:
    """Extract BCD Service Balance (6 bytes, each digit = 1 paise / 0.01 ₹)."""
    if len(df33_data) < 29:
        return None
    bal_bcd_str = df33_data[23:29].hex()
    try:
        bal_paise = int(bal_bcd_str)
        return bal_paise / 100.0
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# PN532 HSU / UART Transport
# ---------------------------------------------------------------------------

class Pn532Uart:
    ACK = bytes.fromhex("00 00 FF 00 FF 00")

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        timeout: float = 1.0,
        debug: bool = False,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.debug = debug

        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
            write_timeout=1.0,
        )

        self.target: int | None = None
        self._wakeup_pending = True

    @staticmethod
    def _frame(payload: bytes) -> bytes:
        data = b"\xD4" + payload
        length = len(data)

        if length > 255:
            raise ValueError("PN532 normal frame payload too large")

        lcs = (-length) & 0xFF
        dcs = (-sum(data)) & 0xFF

        return (
            b"\x00\x00\xFF"
            + bytes([length, lcs])
            + data
            + bytes([dcs, 0x00])
        )

    def _read_exact(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        out = bytearray()

        while len(out) < n and time.monotonic() < deadline:
            chunk = self.ser.read(n - len(out))
            if chunk:
                out += chunk

        if len(out) != n:
            raise TimeoutError(
                f"PN532 UART timeout: wanted {n}, got {len(out)}"
            )

        return bytes(out)

    def _read_frame(self, timeout: float | None = None) -> bytes:
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        sync = bytearray()

        while time.monotonic() < deadline:
            b = self.ser.read(1)
            if not b:
                continue

            sync.append(b[0])
            if len(sync) > 3:
                del sync[:-3]

            if bytes(sync) == b"\x00\x00\xFF":
                break
        else:
            raise TimeoutError("PN532 UART response frame not found")

        remaining = max(0.01, deadline - time.monotonic())
        length = self._read_exact(1, remaining)[0]
        lcs = self._read_exact(
            1, max(0.01, deadline - time.monotonic())
        )[0]

        if length == 0xFF and lcs == 0xFF:
            hi = self._read_exact(
                1, max(0.01, deadline - time.monotonic())
            )[0]
            lo = self._read_exact(
                1, max(0.01, deadline - time.monotonic())
            )[0]
            ext_lcs = self._read_exact(
                1, max(0.01, deadline - time.monotonic())
            )[0]

            if ((hi + lo + ext_lcs) & 0xFF) != 0:
                raise RuntimeError("Invalid PN532 extended length checksum")

            length = (hi << 8) | lo

        elif ((length + lcs) & 0xFF) != 0:
            raise RuntimeError(
                f"Invalid PN532 LEN/LCS: {length:02X} {lcs:02X}"
            )

        if length == 0:
            dcs = self._read_exact(
                1, max(0.01, deadline - time.monotonic())
            )
            post = self._read_exact(
                1, max(0.01, deadline - time.monotonic())
            )
            return b"\x00\x00" + dcs + post

        data = self._read_exact(
            length, max(0.01, deadline - time.monotonic())
        )
        dcs = self._read_exact(
            1, max(0.01, deadline - time.monotonic())
        )[0]
        post = self._read_exact(
            1, max(0.01, deadline - time.monotonic())
        )[0]

        if ((sum(data) + dcs) & 0xFF) != 0:
            raise RuntimeError("Invalid PN532 frame data checksum")

        if post != 0:
            raise RuntimeError(
                f"Invalid PN532 postamble: {post:02X}"
            )

        return data

    def _wait_ack(self, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        buf = bytearray()

        while time.monotonic() < deadline:
            b = self.ser.read(1)
            if not b:
                continue

            buf.append(b[0])

            if len(buf) >= len(self.ACK):
                if bytes(buf[-6:]) == self.ACK:
                    return
                del buf[:-6]

        raise TimeoutError("PN532 ACK timeout")

    def _command(
        self,
        payload: bytes,
        timeout: float | None = None,
    ) -> bytes:
        frame = self._frame(payload)

        if self.debug:
            print(f"PN532 >> {frame.hex(' ').upper()}")

        self.ser.reset_input_buffer()

        if self._wakeup_pending:
            wake = bytes.fromhex(
                "55 55 00 00 00 00 00 00 "
                "00 00 00 00 00 00 00 00"
            )

            self.ser.write(wake + frame)
            self.ser.flush()
            self._wakeup_pending = False

            ack_timeout = max(1.5, timeout or self.timeout)
        else:
            self.ser.write(frame)
            self.ser.flush()
            ack_timeout = timeout or self.timeout

        self._wait_ack(ack_timeout)

        data = self._read_frame(timeout or self.timeout)

        if not data or data[0] != 0xD5:
            raise RuntimeError(
                f"Unexpected PN532 response: {hx(data)}"
            )

        if self.debug:
            print(f"PN532 << {hx(data)}")

        return data

    def get_firmware_version(self) -> tuple[int, int, int, int]:
        response = self._command(b"\x02")

        if response[:2] != b"\xD5\x03" or len(response) < 6:
            raise RuntimeError(
                f"Invalid GetFirmwareVersion: {hx(response)}"
            )

        return (
            response[2],
            response[3],
            response[4],
            response[5],
        )

    def sam_config(self) -> None:
        response = self._command(
            b"\x14\x01\x14\x00",
            timeout=1.0,
        )

        if response[:2] != b"\xD5\x15":
            raise RuntimeError(
                f"SAMConfiguration failed: {hx(response)}"
            )

    def initialize(self) -> None:
        self.get_firmware_version()
        self.sam_config()
        if self.debug:
            print("[*] PN532 initialized")

    def poll_iso14443a(self, timeout: float = 10.0) -> dict[str, Any]:
        response = self._command(
            b"\x4A\x01\x00",
            timeout=timeout,
        )

        if response[:2] != b"\xD5\x4B" or len(response) < 4:
            raise RuntimeError(
                f"Invalid InListPassiveTarget response: {hx(response)}"
            )

        if response[2] == 0:
            raise TimeoutError("No ISO14443-A card detected")

        target = response[3]
        sens_res = response[4:6]
        sak = response[6]
        uid_len = response[7]
        uid = response[8:8 + uid_len]
        ats = response[8 + uid_len:]

        self.target = target

        return {
            "target": target,
            "sens_res": sens_res,
            "sak": sak,
            "uid": uid,
            "ats": ats,
        }

    def in_data_exchange(
        self,
        apdu: bytes,
        timeout: float = 5.0,
    ) -> bytes:
        if self.target is None:
            raise RuntimeError("No PN532 target selected")

        response = self._command(
            b"\x40" + bytes([self.target]) + apdu,
            timeout=timeout,
        )

        if response[:2] != b"\xD5\x41" or len(response) < 3:
            raise RuntimeError(
                f"Invalid InDataExchange response: {hx(response)}"
            )

        status = response[2]

        if status != 0x00:
            raise RuntimeError(
                f"PN532 InDataExchange status=0x{status:02X}"
            )

        return response[3:]

    def close(self) -> None:
        if self.ser.is_open:
            self.ser.close()


# ---------------------------------------------------------------------------
# EMV Processing & Common Service Area Decoder
# ---------------------------------------------------------------------------

PPSE = b"2PAY.SYS.DDF01"
SFI = 16
DEFAULT_LOG_RECORDS = 10

NCMC_CAPTURED_PDOL = bytes.fromhex(
    "FF80F00001"       # 9F40 (5)
    "0040000000"       # DF3A (5)
    "000848"           # 9F33 (3)
    "0002"             # 9F09 (2)
    "0743"             # 9F15 (2)
    "260206"           # 9A (3)
    "060235"           # 9F21 (3)
    "CFBAFEBD"         # 9F37 (4)
    "FF01"             # DF16 (2) Service ID
    "3032343638373000" # 9F1C (8)
)


def fmt_hex(val: Any, num_bytes: int) -> str:
    """Format numerical value to Hex string prefixed with 0x."""
    if isinstance(val, int):
        return f"0x{val:0{num_bytes * 2}X}"
    return str(val)


def decode_common_service_area(
    csa_bytes: bytes,
    record_num: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    if len(csa_bytes) != 96:
        return None, [], "Unknown"

    txn_status_map = {0: "Exit", 1: "Entry", 2: "Penalty Apply", 3: "One Tap / Ticket"}

    # General Data language
    lang_byte = csa_bytes[1]
    card_language = decode_language(lang_byte)

    # Validation Data (19B)
    val = csa_bytes[2:21]
    acq_id = val[2]
    op_id = int.from_bytes(val[3:5], "big")
    term_id = int.from_bytes(val[5:8], "big")
    val_minutes = int.from_bytes(val[8:11], "big")
    max_fare_raw = int.from_bytes(val[11:13], "big")
    max_fare_rs = max_fare_raw / 10.0
    val_status_byte = val[18]
    val_txn_status = (val_status_byte >> 4) & 0x0F
    val_status_str = txn_status_map.get(val_txn_status, f"0x{val_txn_status:01X}")

    val_event = None
    if val_minutes > 0 or term_id > 0 or acq_id > 0:
        val_event = {
            "log_rec": record_num,
            "minutes": val_minutes,
            "acq_id": acq_id,
            "op_id": op_id,
            "term_id": term_id,
            "max_fare_rs": max_fare_rs,
            "status": val_status_str,
        }

    # History Data (68B -> 4 x 17B)
    hist_bytes = csa_bytes[21:89]
    history_records = []

    for idx in range(4):
        h = hist_bytes[idx * 17 : (idx + 1) * 17]
        h_acq = h[0]
        h_op = int.from_bytes(h[1:3], "big")
        h_term = int.from_bytes(h[3:6], "big")
        h_minutes = int.from_bytes(h[6:9], "big")
        h_seq = int.from_bytes(h[9:11], "big")
        h_amt_raw = int.from_bytes(h[11:13], "big")
        h_amt = h_amt_raw / 10.0

        bal_status_bytes = int.from_bytes(h[13:16], "big")
        h_bal_before_raw = (bal_status_bytes >> 4) & 0xFFFFF
        h_bal_before = h_bal_before_raw / 10.0

        h_status_code = bal_status_bytes & 0x0F
        h_status_str = txn_status_map.get(h_status_code, f"Unknown ({h_status_code})")

        if h_minutes > 0 or h_seq > 0 or h_term > 0:
            history_records.append({
                "log_rec": record_num,
                "minutes": h_minutes,
                "acq_id": h_acq,
                "op_id": h_op,
                "term_id": h_term,
                "seq_num": h_seq,
                "amount": h_amt,
                "bal_before": h_bal_before,
                "status": h_status_str,
            })

    return val_event, history_records, card_language


def process_clean_history(
    raw_val_events: list[dict[str, Any]],
    raw_hist_events: list[dict[str, Any]],
    effective_date: datetime | None = None,
    df33_balance_rs: float | None = None,
) -> list[dict[str, Any]]:
    val_map = {(v["minutes"], v["acq_id"], v["op_id"], v["term_id"]): v for v in raw_val_events}
    hist_map = {(h["minutes"], h["acq_id"], h["op_id"], h["term_id"]): h for h in raw_hist_events}

    all_keys = set(hist_map.keys()).union(set(val_map.keys()))
    merged_list = []

    for key in all_keys:
        h = hist_map.get(key)
        v = val_map.get(key)

        if h and v:
            amt = h["amount"]
            bal_before = h["bal_before"]
            bal_after = bal_before - amt
            merged_list.append({
                "seq_num": h["seq_num"],
                "minutes": h["minutes"],
                "acq_id": fmt_hex(h["acq_id"], 1),
                "op_id": fmt_hex(h["op_id"], 2),
                "term_id": fmt_hex(h["term_id"], 3),
                "max_fare": f"₹{v['max_fare_rs']:.2f}",
                "amount_val": -amt if amt > 0 else 0.0,
                "bal_before": bal_before,
                "bal_after": bal_after,
                "status": v["status"],
                "is_topup": False,
            })
        elif h:
            amt = h["amount"]
            bal_before = h["bal_before"]
            bal_after = bal_before - amt
            merged_list.append({
                "seq_num": h["seq_num"],
                "minutes": h["minutes"],
                "acq_id": fmt_hex(h["acq_id"], 1),
                "op_id": fmt_hex(h["op_id"], 2),
                "term_id": fmt_hex(h["term_id"], 3),
                "max_fare": "-",
                "amount_val": -amt if amt > 0 else 0.0,
                "bal_before": bal_before,
                "bal_after": bal_after,
                "status": h["status"],
                "is_topup": False,
            })
        elif v:
            mins, acq, op, term = key
            merged_list.append({
                "seq_num": "-",
                "minutes": mins,
                "acq_id": fmt_hex(acq, 1),
                "op_id": fmt_hex(op, 2),
                "term_id": fmt_hex(term, 3),
                "max_fare": f"₹{v['max_fare_rs']:.2f}",
                "amount_val": 0.0,
                "bal_before": None,
                "bal_after": None,
                "status": v["status"],
                "is_topup": False,
            })

    # Sort ascending chronologically to detect Top-Ups between transactions
    sorted_chrono = sorted(
        merged_list,
        key=lambda x: (x["minutes"], x["seq_num"] if isinstance(x["seq_num"], int) else 0)
    )

    final_chronological = []
    prev_bal_after = None

    for evt in sorted_chrono:
        curr_bal_before = evt["bal_before"]

        if prev_bal_after is not None and curr_bal_before is not None:
            if curr_bal_before > prev_bal_after + 0.01:
                topup_amt = curr_bal_before - prev_bal_after
                topup_evt = {
                    "seq_num": "-",
                    "minutes": evt["minutes"],
                    "acq_id": "-",
                    "op_id": "-",
                    "term_id": "-",
                    "max_fare": "-",
                    "amount_val": topup_amt,
                    "bal_before": prev_bal_after,
                    "bal_after": curr_bal_before,
                    "status": "Top-Up",
                    "is_topup": True,
                }
                final_chronological.append(topup_evt)

        final_chronological.append(evt)
        if evt["bal_after"] is not None:
            prev_bal_after = evt["bal_after"]

    # Sort descending for table display (newest first)
    final_table = sorted(
        final_chronological,
        key=lambda x: (x["minutes"], x["seq_num"] if isinstance(x["seq_num"], int) else 0),
        reverse=True
    )

    # Check DF33 real-time balance vs latest logged balance after transaction
    if df33_balance_rs is not None:
        latest_bal = None
        for r in final_table:
            if r["bal_after"] is not None:
                latest_bal = r["bal_after"]
                break

        if latest_bal is not None and df33_balance_rs > latest_bal + 0.01:
            diff = df33_balance_rs - latest_bal
            uncommitted_topup = {
                "seq_num": "-",
                "minutes": 0,
                "acq_id": "-",
                "op_id": "-",
                "term_id": "-",
                "max_fare": "-",
                "amount_val": diff,
                "bal_before": latest_bal,
                "bal_after": df33_balance_rs,
                "status": "Top-Up",
                "is_topup": True,
            }
            final_table.insert(0, uncommitted_topup)

    # Format fields
    for r in final_table:
        if r.get("is_topup", False):
            r["datetime"] = "-"
            r["amount_str"] = f"+₹{r['amount_val']:.2f}"
        else:
            mins = r["minutes"]
            if effective_date and mins > 0:
                r["datetime"] = (effective_date + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M")
            else:
                r["datetime"] = f"{mins} mins"

            if r["status"] == "Entry":
                r["amount_str"] = "-"
            else:
                if r["amount_val"] < 0:
                    r["amount_str"] = f"-₹{abs(r['amount_val']):.2f}"
                elif r["amount_val"] > 0:
                    r["amount_str"] = f"+₹{r['amount_val']:.2f}"
                else:
                    r["amount_str"] = "₹0.00"

        r["bal_after_str"] = f"₹{r['bal_after']:.2f}" if r["bal_after"] is not None else "-"

    return final_table


class EMVExplorer:
    def __init__(self, pn532: Pn532Uart):
        self.pn532 = pn532
        self.df33_balance_rs: float | None = None
        self.card_type: str = "Unknown"

    def exchange(self, apdu: bytes) -> bytes:
        if self.pn532.debug:
            print(f">> {hx(apdu)}")
        response = self.pn532.in_data_exchange(apdu)
        if self.pn532.debug:
            print(f"<< {hx(response)}")
        return response

    def select_ppse(self) -> list[bytes]:
        apdu = build_apdu(0x00, 0xA4, 0x04, 0x00, PPSE, le=0)
        response = self.exchange(apdu)
        sw = parse_sw(response)

        if sw != 0x9000:
            raise RuntimeError(f"SELECT PPSE failed: SW={sw:04X}")

        body = response[:-2]

        # Extract Card Type from Application Label (50)
        label_list = find_tag(body, 0x50)
        if label_list:
            self.card_type = label_list[0].decode("ascii", errors="ignore").strip()

        aids = find_tag(body, 0x4F)
        if not aids:
            raise RuntimeError("No AID found in PPSE response")

        return aids

    def select_aid(self, aid: bytes) -> bytes:
        apdu = build_apdu(0x00, 0xA4, 0x04, 0x00, aid, le=0)
        response = self.exchange(apdu)
        sw = parse_sw(response)

        if sw != 0x9000:
            raise RuntimeError(f"SELECT AID failed: SW={sw:04X}")

        return response[:-2]

    def get_processing_options(self, pdol_data: bytes) -> bytes:
        cmd_data = bytes([0x83, len(pdol_data)]) + pdol_data
        apdu = build_apdu(0x80, 0xA8, 0x00, 0x00, cmd_data, le=0)
        response = self.exchange(apdu)
        sw = parse_sw(response)

        if (sw & 0xFF00) == 0x6C00:
            le = response[-1]
            apdu = build_apdu(0x80, 0xA8, 0x00, 0x00, cmd_data, le=le)
            response = self.exchange(apdu)
            sw = parse_sw(response)

        if sw != 0x9000:
            raise RuntimeError(f"GPO failed: SW={sw:04X}")

        body = response[:-2]

        df33_list = find_tag(body, 0xDF33)
        if df33_list:
            self.df33_balance_rs = parse_df33_balance(df33_list[0])

        return body

    def read_card_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "pan": None,
            "effective_date_str": None,
            "effective_date_dt": None,
            "expiry_date_str": None,
        }

        for sfi in (1, 2):
            for rec in range(1, 4):
                try:
                    data = self.read_record(sfi, rec)
                except RuntimeError:
                    continue

                pan_list = find_tag(data, 0x5A)
                if pan_list and not info["pan"]:
                    raw_pan = pan_list[0].hex().upper().rstrip("F")
                    info["pan"] = raw_pan

                eff_list = find_tag(data, 0x5F25)
                if eff_list and not info["effective_date_str"]:
                    fmt_str, dt = parse_bcd_date(eff_list[0])
                    info["effective_date_str"] = fmt_str
                    info["effective_date_dt"] = dt

                exp_list = find_tag(data, 0x5F24)
                if exp_list and not info["expiry_date_str"]:
                    fmt_str, _ = parse_bcd_date(exp_list[0])
                    info["expiry_date_str"] = fmt_str

                tr2_list = find_tag(data, 0x57)
                if tr2_list:
                    tr2_hex = tr2_list[0].hex().upper()
                    if "D" in tr2_hex:
                        pan_part, rest = tr2_hex.split("D", 1)
                        if not info["pan"]:
                            info["pan"] = pan_part
                        if not info["expiry_date_str"] and len(rest) >= 4:
                            exp_yy, exp_mm = rest[0:2], rest[2:4]
                            info["expiry_date_str"] = f"20{exp_yy}-{exp_mm}"

        return info

    def read_record(self, sfi: int, record: int) -> bytes:
        p2 = (sfi << 3) | 0x04
        apdu = build_apdu(0x00, 0xB2, record, p2, le=0)
        response = self.exchange(apdu)
        sw = parse_sw(response)

        if (sw & 0xFF00) == 0x6C00:
            le = response[-1]
            apdu = build_apdu(0x00, 0xB2, record, p2, le=le)
            response = self.exchange(apdu)
            sw = parse_sw(response)

        if sw != 0x9000:
            raise RuntimeError(f"READ RECORD SFI={sfi} REC={record} failed: SW={sw:04X}")

        return response[:-2]

    def process_card(self, count: int = DEFAULT_LOG_RECORDS) -> None:
        # 1. PPSE
        aids = self.select_ppse()

        # 2. Select AID & GPO
        aid = aids[0]
        self.select_aid(aid)
        self.get_processing_options(NCMC_CAPTURED_PDOL)

        # 3. Read Card Info
        card_info = self.read_card_info()

        # 4. Read Transaction Log (SFI 16)
        raw_val_events: list[dict[str, Any]] = []
        raw_hist_events: list[dict[str, Any]] = []
        detected_language = "English"

        for record in range(1, count + 1):
            try:
                data = self.read_record(SFI, record)
            except RuntimeError:
                continue

            if len(data) >= 96:
                csa_data = data[-96:]
                val_event, history_records, lang = decode_common_service_area(
                    csa_data,
                    record_num=record,
                )
                detected_language = lang
                if val_event:
                    raw_val_events.append(val_event)
                raw_hist_events.extend(history_records)

        # Process clean history table
        history_rows = process_clean_history(
            raw_val_events,
            raw_hist_events,
            effective_date=card_info["effective_date_dt"],
            df33_balance_rs=self.df33_balance_rs,
        )

        # Clean Console Output
        masked_pan_str = mask_pan(card_info["pan"]) if card_info["pan"] else "N/A"
        expiry_str = card_info["expiry_date_str"] or "N/A"
        balance_str = f"₹{self.df33_balance_rs:.2f}" if self.df33_balance_rs is not None else "N/A"

        print(f"Masked PAN       : {masked_pan_str}")
        print(f"Expiry Date      : {expiry_str}")
        print(f"Card Type        : {self.card_type}")
        print(f"Card Language    : {detected_language}")
        print(f"Current Balance  : {balance_str}")
        print()

        # Table Header (Clean output without border lines or big title)
        header = (
            f"{'Seq #':<6}  "
            f"{'Date & Time':<16}  "
            f"{'Acq ID':<8}  "
            f"{'Op ID':<8}  "
            f"{'Term ID':<10}  "
            f"{'Max Fare':<10}  "
            f"{'Amount':<10}  "
            f"{'Balance':<10}  "
            f"{'Status':<16}"
        )
        print(header)

        for r in history_rows:
            row = (
                f"{str(r['seq_num']):<6}  "
                f"{r['datetime']:<16}  "
                f"{r['acq_id']:<8}  "
                f"{r['op_id']:<8}  "
                f"{r['term_id']:<10}  "
                f"{r['max_fare']:<10}  "
                f"{r['amount_str']:<10}  "
                f"{r['bal_after_str']:<10}  "
                f"{r['status']:<16}"
            )
            print(row)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simplified NCMC EMV Explorer"
    )
    parser.add_argument(
        "--port",
        default="/dev/ttyUSB0",
        help="PN532 HSU UART device (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print PN532 frames and APDUs",
    )
    parser.add_argument(
        "--records",
        type=int,
        default=DEFAULT_LOG_RECORDS,
        help="Number of SFI 16 records to read (default: 10)",
    )

    args = parser.parse_args()

    pn532 = Pn532Uart(
        port=args.port,
        baudrate=115200,
        debug=args.debug,
    )

    try:
        pn532.initialize()
        card = pn532.poll_iso14443a()

        if not (card["sak"] & 0x20):
            raise RuntimeError("Presented card is not ISO14443-4 capable")

        explorer = EMVExplorer(pn532)
        explorer.process_card(count=args.records)

    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if args.debug:
            print(f"ERROR: {exc}")
    finally:
        pn532.close()


if __name__ == "__main__":
    main()
