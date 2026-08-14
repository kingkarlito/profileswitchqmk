"""
QMK Profile Switcher
--------------------
Cycles through up to 5 VIA layout JSON profiles via a global hotkey.
Runs as a system-tray app on Windows.
Supports any QMK-compatible keyboard with VIA.

Dependencies:
    pip install hidapi pynput pystray pillow

Usage:
    python profileswitchqmk.py
    -- or compile with PyInstaller --
"""

import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import hid
import time
import threading
import os
import sys
import struct
import copy
from pynput import keyboard
from PIL import Image, ImageDraw
from pystray import MenuItem as item, Icon

# ── VIA raw-HID protocol ─────────────────────────────────────────────────────
# VIA standard = 32-byte reports, but some firmwares use 64-byte reports.
# We auto-detect at connection time by probing both sizes.

# VIA protocol v12 command IDs
VIA_GET_PROTOCOL_VERSION         = 0x01
VIA_GET_KEYBOARD_VALUE           = 0x02
VIA_SET_KEYBOARD_VALUE           = 0x03
VIA_DYNAMIC_KEYMAP_GET_KEYCODE   = 0x04
VIA_DYNAMIC_KEYMAP_SET_KEYCODE   = 0x05
VIA_DYNAMIC_KEYMAP_RESET         = 0x06
VIA_DYNAMIC_KEYMAP_GET_LAYER_COUNT = 0x11
VIA_DYNAMIC_KEYMAP_GET_BUFFER    = 0x12
VIA_DYNAMIC_KEYMAP_SET_BUFFER    = 0x13

# Report sizes to probe (payload bytes, not counting the leading 0x00 report-id)
_PROBE_SIZES = [32, 64]

# ── Config file ───────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(sys.argv[0]), "switcher_config.json")

DEFAULT_CONFIG = {
    "hardware": {
        "vendor_id": None,
        "product_id": None,
        "usage_page": None,
        "usage": None,
        "matrix_rows": None,
        "matrix_cols": None,
        "keyboard_name": "QMK Keyboard"
    },
    "hotkey": "<ctrl>+<alt>+<shift>+p",
    "profiles": ["", "", "", "", ""],
    "current_index": -1,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def resource_path(relative):
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.abspath(".")
    return os.path.join(base, relative)


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # fill missing keys
            for k, v in DEFAULT_CONFIG.items():
                if isinstance(v, dict):
                    cfg.setdefault(k, {})
                    for k2, v2 in v.items():
                        cfg[k].setdefault(k2, v2)
                else:
                    cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[config] save error: {e}")


def make_tray_icon_image(color=(80, 160, 255)):
    """Generate a simple coloured keyboard icon for the system tray."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # body
    d.rounded_rectangle([4, 16, 60, 48], radius=6, fill=color)
    # keys (3×2 grid)
    key_color = (255, 255, 255, 200)
    for row in range(2):
        for col in range(3):
            x = 10 + col * 17
            y = 22 + row * 11
            d.rectangle([x, y, x+11, y+7], fill=key_color)
    return img


# ── HID communication ─────────────────────────────────────────────────────────

def open_via_device(vendor_id, product_id, usage_page, usage):
    """Return an open hid.device() for the specified VIA interface, or None."""
    infos = hid.enumerate(vendor_id, product_id)
    path = next(
        (d["path"] for d in infos
         if d["usage_page"] == usage_page and d["usage"] == usage),
        None
    )
    if path is None:
        return None
    dev = hid.device()
    dev.open_path(path)
    dev.set_nonblocking(True)
    return dev


def _via_send_sized(dev, payload: list[int], report_size: int):
    """Send one VIA HID report of exactly report_size payload bytes."""
    buf = [0x00] + payload[:report_size] + [0x00] * (report_size - len(payload[:report_size]))
    dev.write(bytes(buf))


def _via_read_sized(dev, report_size: int, timeout_ms: int = 500):
    """Read one VIA response of report_size bytes. Returns list or None."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        data = dev.read(report_size + 1)   # +1 for report-id byte
        if data:
            return list(data)[:report_size]
        time.sleep(0.005)
    return None


def detect_report_size(dev) -> tuple[int, int | None]:
    """
    Auto-detect the correct HID report size (32 or 64 bytes) by probing
    VIA_GET_PROTOCOL_VERSION and checking which size gets a valid response.
    Returns (report_size, protocol_version).
    """
    for size in _PROBE_SIZES:
        # Flush any stale data first
        for _ in range(4):
            dev.read(65)

        _via_send_sized(dev, [VIA_GET_PROTOCOL_VERSION], size)
        time.sleep(0.05)
        resp = _via_read_sized(dev, size, timeout_ms=300)

        if resp and resp[0] == VIA_GET_PROTOCOL_VERSION:
            ver = (resp[1] << 8) | resp[2]
            print(f"[VIA] report_size={size}  protocol_version={ver:#06x}")
            return size, ver

    # Fallback: assume 32 (standard), version unknown
    print("[VIA] WARNING: could not confirm report size — defaulting to 32")
    return 32, None


# Module-level cache so we don't re-probe on every packet within one upload
_report_size_cache: int | None = None


def via_probe_and_open(vendor_id, product_id, usage_page, usage) -> tuple:
    """Open device and detect report size. Returns (dev, report_size, protocol_ver)."""
    global _report_size_cache
    dev = open_via_device(vendor_id, product_id, usage_page, usage)
    if dev is None:
        return None, 32, None
    size, ver = detect_report_size(dev)
    _report_size_cache = size
    return dev, size, ver


def via_send(dev, payload: list[int], report_size: int = 32):
    _via_send_sized(dev, payload, report_size)


def via_read(dev, report_size: int = 32, timeout_ms: int = 500):
    return _via_read_sized(dev, report_size, timeout_ms)


def via_read_one_keycode(dev, report_size: int, layer: int, row: int, col: int) -> int | None:
    """Read back a single keycode. Protocol v12: cmd=0x04, args=[layer, row, col]."""
    _via_send_sized(dev, [VIA_DYNAMIC_KEYMAP_GET_KEYCODE, layer, row, col], report_size)
    resp = _via_read_sized(dev, report_size, timeout_ms=300)
    if resp and resp[0] == VIA_DYNAMIC_KEYMAP_GET_KEYCODE:
        return (resp[4] << 8) | resp[5]
    return None


def via_set_buffer(dev, report_size: int, offset: int, data: list[int], status_cb=None) -> bool:
    """
    Bulk-write keymap data using id_dynamic_keymap_set_buffer (0x13).
    Faster than key-by-key and the correct path for VIA protocol v12.
    Format: [0x13, offset_hi, offset_lo, size, data...]
    Max data per packet = report_size - 4 bytes of header.
    """
    chunk_size = report_size - 4
    total = len(data)
    sent = 0
    while sent < total:
        chunk = data[sent:sent + chunk_size]
        cur_offset = offset + sent
        payload = [
            VIA_DYNAMIC_KEYMAP_SET_BUFFER,
            (cur_offset >> 8) & 0xFF,
            cur_offset & 0xFF,
            len(chunk),
        ] + chunk
        _via_send_sized(dev, payload, report_size)
        time.sleep(0.002)
        sent += len(chunk)
        if status_cb and sent % (chunk_size * 10) == 0:
            pct = int(sent / total * 100)
            status_cb(f"Uploading... {pct}%")
    return True


def via_get_protocol_version(dev, report_size: int = 32) -> int | None:
    via_send(dev, [VIA_GET_PROTOCOL_VERSION], report_size)
    resp = via_read(dev, report_size)
    if resp and resp[0] == VIA_GET_PROTOCOL_VERSION:
        return (resp[1] << 8) | resp[2]
    return None


def detect_json_format(layout_json: dict) -> str:
    """
    Return 'manufacturer' if JSON has a 'keymap' key with {row,col,val} objects,
    or 'via' if it has a 'layers' key with string arrays.
    """
    if "keymap" in layout_json:
        return "manufacturer"
    if "layers" in layout_json:
        return "via"
    return "unknown"


def upload_manufacturer_json(dev, layout_json: dict, report_size: int, status_cb=None):
    """
    Upload a manufacturer-format JSON.
    Format: {"keymap": [ [{row, col, val}, ...], ... ], "knob": [...]}
    """
    keymap = layout_json.get("keymap", [])
    if not keymap:
        if status_cb:
            status_cb("Error: no 'keymap' key in manufacturer JSON.")
        return False

    total = sum(len(layer) for layer in keymap)
    done = 0

    for layer_idx, layer in enumerate(keymap):
        for key in layer:
            row  = key["row"]
            col  = key["col"]
            code = key["val"]
            payload = [
                VIA_DYNAMIC_KEYMAP_SET_KEYCODE,
                layer_idx,
                row,
                col,
                (code >> 8) & 0xFF,
                code & 0xFF,
            ]
            via_send(dev, payload, report_size)
            time.sleep(0.002)
            done += 1
            if status_cb and done % 30 == 0:
                pct = int(done / total * 100)
                status_cb(f"Uploading... {pct}%")

    # Spot-check: read back first non-zero key of layer 0
    for key in keymap[0]:
        if key["val"] != 0:
            actual = via_read_one_keycode(dev, report_size, 0, key["row"], key["col"])
            if actual is not None:
                match = actual == key["val"]
                print(f"[verify] {'OK' if match else 'MISMATCH'}  "
                      f"layer=0 row={key['row']} col={key['col']}  "
                      f"expected={key['val']:#06x} got={actual:#06x}")
                if not match and status_cb:
                    status_cb(f"WARNING: readback mismatch {key['val']:#06x} vs {actual:#06x}")
            break

    if status_cb:
        status_cb("Upload complete.")
    return True


def upload_layout_json(dev, layout_json: dict, report_size: int, matrix_rows: int, matrix_cols: int, status_cb=None):
    """
    Push a VIA layout JSON using the bulk SET_BUFFER path (protocol v12 cmd 0x13).
    """
    layers = layout_json.get("layers", [])
    if not layers:
        if status_cb:
            status_cb("Error: no 'layers' key in JSON.")
        return False

    # Build the flat binary buffer
    unknown = []
    buf = []
    for layer in layers:
        if not isinstance(layer, list):
            continue
        for kc_str in layer:
            code, was_unknown = via_encode_keycode(kc_str)
            if was_unknown:
                unknown.append(kc_str)
            buf.append((code >> 8) & 0xFF)
            buf.append(code & 0xFF)

    if unknown:
        print(f"[keycode] {len(set(unknown))} unknown (KC_TRNS): "
              + ", ".join(sorted(set(unknown))[:8]))

    if status_cb:
        status_cb(f"Uploading {len(layers)} layers, {len(buf)} bytes...")

    # Send via bulk SET_BUFFER (0x13)
    via_set_buffer(dev, report_size, 0, buf, status_cb=status_cb)

    # Spot-check: read back the first non-KC_NO key of layer 0
    first_layer = layers[0] if layers else []
    for flat_idx, kc_str in enumerate(first_layer):
        if kc_str in ("KC_NO", "KC_TRNS"):
            continue
        expected, _ = via_encode_keycode(kc_str)
        row = flat_idx // matrix_cols
        col = flat_idx % matrix_cols
        actual = via_read_one_keycode(dev, report_size, 0, row, col)
        if actual is not None:
            match = actual == expected
            print(f"[verify] {'OK' if match else 'MISMATCH'}  "
                  f"r={row} c={col}  expected={expected:#06x} got={actual:#06x}  ({kc_str})")
            if match:
                if status_cb:
                    status_cb("Upload complete and verified.")
            else:
                if status_cb:
                    status_cb(f"WARNING: readback mismatch — expected {expected:#06x}, got {actual:#06x}")
        else:
            if status_cb:
                status_cb("Upload sent (readback timed out).")
        break

    return True


# ── VIA keycode encoder ───────────────────────────────────────────────────────

def _build_kc_table() -> dict[str, int]:
    t: dict[str, int] = {}

    t["KC_NO"]   = 0x0000
    t["KC_TRNS"] = 0x0001
    t["XXXXXXX"] = 0x0000
    t["_______"] = 0x0001

    # Letters  a=0x04 … z=0x1D
    for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        t[f"KC_{c}"] = 0x0004 + i

    # Numbers on main row
    for i, n in enumerate("1234567890"):
        t[f"KC_{n}"] = 0x001E + i

    # Punctuation / symbols
    t.update({
        "KC_ENT":  0x0028, "KC_ENTER": 0x0028,
        "KC_ESC":  0x0029, "KC_ESCAPE": 0x0029,
        "KC_BSPC": 0x002A, "KC_BACKSPACE": 0x002A,
        "KC_TAB":  0x002B,
        "KC_SPC":  0x002C, "KC_SPACE": 0x002C,
        "KC_MINS": 0x002D, "KC_MINUS": 0x002D,
        "KC_EQL":  0x002E, "KC_EQUAL": 0x002E,
        "KC_LBRC": 0x002F, "KC_LEFT_BRACKET": 0x002F,
        "KC_RBRC": 0x0030, "KC_RIGHT_BRACKET": 0x0030,
        "KC_BSLS": 0x0031, "KC_BACKSLASH": 0x0031,
        "KC_NUHS": 0x0032,
        "KC_SCLN": 0x0033, "KC_SEMICOLON": 0x0033,
        "KC_QUOT": 0x0034, "KC_QUOTE": 0x0034,
        "KC_GRV":  0x0035, "KC_GRAVE": 0x0035, "KC_GESC": 0x7C16,
        "KC_COMM": 0x0036, "KC_COMMA": 0x0036,
        "KC_DOT":  0x0037,
        "KC_SLSH": 0x0038, "KC_SLASH": 0x0038,
        "KC_CAPS": 0x0039, "KC_CAPSLOCK": 0x0039,
    })

    # F-keys F1–F24
    for n in range(1, 13):
        t[f"KC_F{n}"] = 0x003A + n - 1
    t["KC_F13"] = 0x0068
    t["KC_F14"] = 0x0069
    t["KC_F15"] = 0x006A
    t["KC_F16"] = 0x006B
    t["KC_F17"] = 0x006C
    t["KC_F18"] = 0x006D
    t["KC_F19"] = 0x006E
    t["KC_F20"] = 0x006F
    t["KC_F21"] = 0x0070
    t["KC_F22"] = 0x0071
    t["KC_F23"] = 0x0072
    t["KC_F24"] = 0x0073

    # Navigation cluster
    t.update({
        "KC_PSCR": 0x0046, "KC_PRINT_SCREEN": 0x0046,
        "KC_SCRL": 0x0047, "KC_LSCR": 0x0084, "KC_SCROLL_LOCK": 0x0047,
        "KC_PAUS": 0x0048, "KC_PAUSE": 0x0048,
        "KC_INS":  0x0049, "KC_INSERT": 0x0049,
        "KC_HOME": 0x004A,
        "KC_PGUP": 0x004B, "KC_PAGE_UP": 0x004B,
        "KC_DEL":  0x004C, "KC_DELETE": 0x004C,
        "KC_END":  0x004D,
        "KC_PGDN": 0x004E, "KC_PAGE_DOWN": 0x004E,
        "KC_RGHT": 0x004F, "KC_RIGHT": 0x004F,
        "KC_LEFT": 0x0050,
        "KC_DOWN": 0x0051,
        "KC_UP":   0x0052,
        "KC_NLCK": 0x0053, "KC_NUM_LOCK": 0x0053, "KC_LNUM": 0x0083,
    })

    # Numpad
    t.update({
        "KC_PSLS": 0x0054, "KC_KP_SLASH": 0x0054,
        "KC_PAST": 0x0055, "KC_KP_ASTERISK": 0x0055,
        "KC_PMNS": 0x0056, "KC_KP_MINUS": 0x0056,
        "KC_PPLS": 0x0057, "KC_KP_PLUS": 0x0057,
        "KC_PENT": 0x0058, "KC_KP_ENTER": 0x0058,
        "KC_P1":   0x0059, "KC_KP_1": 0x0059,
        "KC_P2":   0x005A, "KC_KP_2": 0x005A,
        "KC_P3":   0x005B, "KC_KP_3": 0x005B,
        "KC_P4":   0x005C, "KC_KP_4": 0x005C,
        "KC_P5":   0x005D, "KC_KP_5": 0x005D,
        "KC_P6":   0x005E, "KC_KP_6": 0x005E,
        "KC_P7":   0x005F, "KC_KP_7": 0x005F,
        "KC_P8":   0x0060, "KC_KP_8": 0x0060,
        "KC_P9":   0x0061, "KC_KP_9": 0x0061,
        "KC_P0":   0x0062, "KC_KP_0": 0x0062,
        "KC_PDOT": 0x0063, "KC_KP_DOT": 0x0063,
    })

    # Modifiers
    t.update({
        "KC_LCTL": 0x00E0, "KC_LEFT_CTRL":  0x00E0,
        "KC_LSFT": 0x00E1, "KC_LEFT_SHIFT": 0x00E1,
        "KC_LALT": 0x00E2, "KC_LEFT_ALT":   0x00E2,
        "KC_LGUI": 0x00E3, "KC_LEFT_GUI":   0x00E3, "KC_LWIN": 0x00E3,
        "KC_RCTL": 0x00E4, "KC_RIGHT_CTRL":  0x00E4,
        "KC_RSFT": 0x00E5, "KC_RIGHT_SHIFT": 0x00E5,
        "KC_RALT": 0x00E6, "KC_RIGHT_ALT":   0x00E6,
        "KC_RGUI": 0x00E7, "KC_RIGHT_GUI":   0x00E7, "KC_RWIN": 0x00E7,
        "KC_MENU": 0x0076,
    })

    # Media / consumer
    t.update({
        "KC_MUTE": 0x00A8, "KC_AUDIO_MUTE":      0x00A8,
        "KC_VOLU": 0x00B5, "KC_AUDIO_VOL_UP":    0x00B5,
        "KC_VOLD": 0x00B4, "KC_AUDIO_VOL_DOWN":  0x00B4,
        "KC_MPLY": 0x00B0, "KC_MEDIA_PLAY_PAUSE": 0x00B0,
        "KC_MSTP": 0x00B2, "KC_MEDIA_STOP":       0x00B2,
        "KC_MPRV": 0x00AE, "KC_MEDIA_PREV_TRACK": 0x00AE,
        "KC_MNXT": 0x00B3, "KC_MEDIA_NEXT_TRACK": 0x00B3,
        "KC_BRID": 0x00B9, "KC_BRIU": 0x00B8,
    })

    # Mouse keys
    t.update({
        "KC_MS_UP":    0x00CD, "KC_MS_DOWN":  0x00CE,
        "KC_MS_LEFT":  0x00CF, "KC_MS_RIGHT": 0x00D0,
        "KC_MS_BTN1":  0x00D1, "KC_MS_BTN2":  0x00D2,
        "KC_MS_BTN3":  0x00D3, "KC_MS_BTN4":  0x00D4,
        "KC_MS_BTN5":  0x00D5, "KC_MS_BTN6":  0x00D6,
        "KC_MS_BTN7":  0x00D7, "KC_MS_BTN8":  0x00D8,
        "KC_MS_WH_UP":    0x00D9, "KC_MS_WH_DOWN":  0x00DA,
        "KC_MS_WH_LEFT":  0x00DB, "KC_MS_WH_RIGHT": 0x00DC,
    })

    # Browser / app
    t.update({
        "KC_WWW_SEARCH":  0x00B4, "KC_WWW_BACK":    0x00B6,
        "KC_WWW_FORWARD": 0x00B7, "KC_WWW_STOP":    0x00B5,
        "KC_WWW_REFRESH": 0x00B9, "KC_WWW_HOME":    0x00BA,
        "KC_WWW_FAV":     0x00BB,
    })

    # Edit shortcuts
    t.update({
        "KC_UNDO":  0x007A, "KC_AGAIN": 0x0079,
        "KC_CUT":   0x007B, "KC_COPY":  0x007C,
        "KC_PASTE": 0x007D, "KC_FIND":  0x007E,
    })

    # Backlight control
    t.update({
        "BL_TOGG": 0x7800, "BL_STEP": 0x7801,
        "BL_ON":   0x7802, "BL_OFF":  0x7803,
        "BL_INC":  0x7805, "BL_DEC":  0x7806,
        "BL_BRTG": 0x7807,
    })

    # Magic / quantum keys
    t["MAGIC_TOGGLE_NKRO"] = 0x7C46

    # Space cadet
    t["KC_LSPO"] = 0x7C1A
    t["KC_RSPC"] = 0x7C1B

    # One-shot mod
    t["KC_LCTL_T"] = 0x00E0
    t["OSM(MOD_LSFT)"] = 0x00E1

    return t


_KC_TABLE: dict[str, int] = _build_kc_table()

# Modifier bit masks
_MOD_BITS: dict[str, int] = {
    "MOD_LCTL": 0x01, "MOD_LSFT": 0x02, "MOD_LALT": 0x04, "MOD_LGUI": 0x08,
    "MOD_RCTL": 0x11, "MOD_RSFT": 0x12, "MOD_RALT": 0x14, "MOD_RGUI": 0x18,
    "MOD_LWIN": 0x08, "MOD_RWIN": 0x18,
}


def _parse_mod_mask(expr: str) -> int:
    """Parse 'MOD_LCTL | MOD_LSFT | MOD_LGUI' → bitmask int."""
    mask = 0
    for part in expr.split("|"):
        part = part.strip()
        if part in _MOD_BITS:
            mask |= _MOD_BITS[part]
        else:
            try:
                mask |= int(part, 0)
            except ValueError:
                pass
    return mask


def via_encode_keycode(s: str) -> tuple[int, bool]:
    """
    Encode a VIA/QMK keycode string to its 16-bit integer value.
    Returns (code, was_unknown).
    """
    s = s.strip()

    # 1. Direct lookup
    if s in _KC_TABLE:
        return _KC_TABLE[s], False

    # 2. Numeric literal
    try:
        return int(s, 0), False
    except ValueError:
        pass

    # 3. TO(layer) / MO(layer) / TG(layer) / DF(layer) / OSL(layer) / TT(layer)
    m = re.fullmatch(r'(TO|MO|TG|DF|OSL|TT)\((\d+)\)', s)
    if m:
        fn, layer = m.group(1), int(m.group(2))
        bases = {"TO": 0x5200, "MO": 0x5100, "DF": 0x5200,
                 "TG": 0x5300, "OSL": 0x5400, "TT": 0x52C0}
        return bases[fn] | layer, False

    # 4. LT(layer, kc)
    m = re.fullmatch(r'LT\((\d+),\s*(.+)\)', s)
    if m:
        layer = int(m.group(1))
        kc, _ = via_encode_keycode(m.group(2))
        return 0x4000 | (layer << 8) | (kc & 0xFF), False

    # 5. MT(mods, kc)
    m = re.fullmatch(r'MT\((.+?),\s*(.+)\)', s)
    if m:
        mods = _parse_mod_mask(m.group(1))
        kc, _ = via_encode_keycode(m.group(2))
        return 0x2000 | (mods << 8) | (kc & 0xFF), False

    # 6. S(kc) / LSFT(kc)
    m = re.fullmatch(r'(?:S|LSFT)\((.+)\)', s)
    if m:
        kc, _ = via_encode_keycode(m.group(1))
        return 0x0200 | (kc & 0xFF), False

    # 7. LCTL(kc), LALT(kc), LGUI(kc), RALT(kc) etc.
    mod_fn_map = {
        "LCTL": 0x0100, "LSFT": 0x0200, "LALT": 0x0400, "LGUI": 0x0800,
        "RCTL": 0x1000, "RSFT": 0x2000, "RALT": 0x4000, "RGUI": 0x8000,
    }
    m = re.fullmatch(r'(LCTL|LSFT|LALT|LGUI|RCTL|RSFT|RALT|RGUI)\((.+)\)', s)
    if m:
        mod_code = mod_fn_map[m.group(1)]
        kc, _ = via_encode_keycode(m.group(2))
        return mod_code | (kc & 0xFF), False

    # 8. CUSTOM(n)
    m = re.fullmatch(r'CUSTOM\((\d+)\)', s)
    if m:
        return 0x7E00 | int(m.group(1)), False

    # 9. OSM(MOD_xxx)
    m = re.fullmatch(r'OSM\((.+)\)', s)
    if m:
        mods = _parse_mod_mask(m.group(1))
        return 0x5500 | mods, False

    # Unknown — fall back to KC_TRNS
    return 0x0001, True


# ── Hardware Setup Dialog ─────────────────────────────────────────────────────

class HardwareSetupDialog(tk.Toplevel):
    def __init__(self, parent, existing_config=None):
        super().__init__(parent)
        self.title("Hardware Configuration")
        self.resizable(False, False)
        self.result = None

        if existing_config:
            hw = existing_config.get("hardware", {})
        else:
            hw = {}

        # Make modal
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Configure your QMK keyboard hardware values",
                  font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 15))

        ttk.Label(frame, text="Keyboard Name:").grid(row=1, column=0, sticky="w", pady=5)
        self.name_var = tk.StringVar(value=hw.get("keyboard_name", ""))
        ttk.Entry(frame, textvariable=self.name_var, width=30).grid(row=1, column=1, pady=5, padx=(10, 0))

        ttk.Label(frame, text="Vendor ID (hex):").grid(row=2, column=0, sticky="w", pady=5)
        self.vendor_var = tk.StringVar(value=hex(hw.get("vendor_id", 0)) if hw.get("vendor_id") else "0x")
        ttk.Entry(frame, textvariable=self.vendor_var, width=30).grid(row=2, column=1, pady=5, padx=(10, 0))

        ttk.Label(frame, text="Product ID (hex):").grid(row=3, column=0, sticky="w", pady=5)
        self.product_var = tk.StringVar(value=hex(hw.get("product_id", 0)) if hw.get("product_id") else "0x")
        ttk.Entry(frame, textvariable=self.product_var, width=30).grid(row=3, column=1, pady=5, padx=(10, 0))

        ttk.Label(frame, text="Usage Page (hex):").grid(row=4, column=0, sticky="w", pady=5)
        self.usage_page_var = tk.StringVar(value=hex(hw.get("usage_page", 0)) if hw.get("usage_page") else "0xFF60")
        ttk.Entry(frame, textvariable=self.usage_page_var, width=30).grid(row=4, column=1, pady=5, padx=(10, 0))

        ttk.Label(frame, text="Usage (hex):").grid(row=5, column=0, sticky="w", pady=5)
        self.usage_var = tk.StringVar(value=hex(hw.get("usage", 0)) if hw.get("usage") else "0x61")
        ttk.Entry(frame, textvariable=self.usage_var, width=30).grid(row=5, column=1, pady=5, padx=(10, 0))

        ttk.Label(frame, text="Matrix Rows:").grid(row=6, column=0, sticky="w", pady=5)
        self.rows_var = tk.StringVar(value=str(hw.get("matrix_rows", "")))
        ttk.Entry(frame, textvariable=self.rows_var, width=30).grid(row=6, column=1, pady=5, padx=(10, 0))

        ttk.Label(frame, text="Matrix Columns:").grid(row=7, column=0, sticky="w", pady=5)
        self.cols_var = tk.StringVar(value=str(hw.get("matrix_cols", "")))
        ttk.Entry(frame, textvariable=self.cols_var, width=30).grid(row=7, column=1, pady=5, padx=(10, 0))

        # Help text
        help_frame = ttk.LabelFrame(frame, text="How to find these values", padding=10)
        help_frame.grid(row=8, column=0, columnspan=2, pady=15, sticky="ew")

        help_text = """Use the 'Scan Devices' button to find your keyboard.
Vendor/Product IDs are in the device properties.
Usage Page is typically 0xFF60 for VIA.
Usage is typically 0x61 for VIA.
Matrix rows/cols are in your keyboard's QMK config.h"""

        ttk.Label(help_frame, text=help_text, font=("Segoe UI", 8), foreground="gray").pack()

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=9, column=0, columnspan=2, pady=(10, 0))

        ttk.Button(btn_frame, text="Scan Devices", command=self._scan_devices).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=5)

    def _scan_devices(self):
        """Scan for HID devices and show them in a dialog."""
        devices = hid.enumerate()

        if not devices:
            messagebox.showinfo("Scan Results", "No HID devices found.")
            return

        # Create a new window to show devices
        scan_win = tk.Toplevel(self)
        scan_win.title("Available HID Devices")
        scan_win.geometry("700x400")

        frame = ttk.Frame(scan_win, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Double-click a device to use its values:",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 10))

        # Create treeview
        tree = ttk.Treeview(frame, columns=("vendor", "product", "usage_page", "usage", "product_string"),
                           show="headings", height=15)
        tree.heading("vendor", text="Vendor ID")
        tree.heading("product", text="Product ID")
        tree.heading("usage_page", text="Usage Page")
        tree.heading("usage", text="Usage")
        tree.heading("product_string", text="Product Name")

        tree.column("vendor", width=90)
        tree.column("product", width=90)
        tree.column("usage_page", width=90)
        tree.column("usage", width=70)
        tree.column("product_string", width=300)

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)

        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Populate with devices
        for dev in devices:
            tree.insert("", "end", values=(
                f"0x{dev['vendor_id']:04X}",
                f"0x{dev['product_id']:04X}",
                f"0x{dev['usage_page']:04X}",
                f"0x{dev['usage']:04X}",
                dev.get('product_string', 'Unknown')
            ))

        def on_double_click(event):
            selection = tree.selection()
            if selection:
                item = tree.item(selection[0])
                values = item['values']
                self.vendor_var.set(values[0])
                self.product_var.set(values[1])
                self.usage_page_var.set(values[2])
                self.usage_var.set(values[3])
                if not self.name_var.get():
                    self.name_var.set(values[4])
                scan_win.destroy()

        tree.bind("<Double-1>", on_double_click)

    def _save(self):
        try:
            vendor_id = int(self.vendor_var.get(), 16)
            product_id = int(self.product_var.get(), 16)
            usage_page = int(self.usage_page_var.get(), 16)
            usage = int(self.usage_var.get(), 16)
            matrix_rows = int(self.rows_var.get())
            matrix_cols = int(self.cols_var.get())
            keyboard_name = self.name_var.get().strip() or "QMK Keyboard"

            self.result = {
                "vendor_id": vendor_id,
                "product_id": product_id,
                "usage_page": usage_page,
                "usage": usage,
                "matrix_rows": matrix_rows,
                "matrix_cols": matrix_cols,
                "keyboard_name": keyboard_name
            }
            self.destroy()
        except ValueError as e:
            messagebox.showerror("Invalid Input",
                               f"Please enter valid values.\nError: {e}")


# ── Main application ──────────────────────────────────────────────────────────

class QMKProfileSwitcher:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("QMK Profile Switcher")
        self.root.resizable(False, False)

        self.cfg = load_config()

        # Check if hardware is configured
        if not self._is_hardware_configured():
            self._show_hardware_setup()
            if not self._is_hardware_configured():
                messagebox.showerror("Setup Required",
                                   "Hardware configuration is required to run this application.")
                sys.exit(1)

        self._hotkey_listener = None
        self._tray_icon = None
        self._busy = False

        self._build_ui()
        self._refresh_profile_list()
        self._start_hotkey_listener()

        # Start tray in background thread, then hide the window
        threading.Thread(target=self._run_tray, daemon=True).start()
        self.root.after(200, self.root.withdraw)
        self.root.protocol("WM_DELETE_WINDOW", self._hide)

    def _is_hardware_configured(self):
        hw = self.cfg.get("hardware", {})
        return all([
            hw.get("vendor_id") is not None,
            hw.get("product_id") is not None,
            hw.get("usage_page") is not None,
            hw.get("usage") is not None,
            hw.get("matrix_rows") is not None,
            hw.get("matrix_cols") is not None
        ])

    def _show_hardware_setup(self):
        dialog = HardwareSetupDialog(self.root, self.cfg)
        self.root.wait_window(dialog)
        if dialog.result:
            self.cfg["hardware"] = dialog.result
            save_config(self.cfg)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        PAD = dict(padx=12, pady=6)
        keyboard_name = self.cfg["hardware"].get("keyboard_name", "QMK Keyboard")

        # ── Header
        hdr = tk.Frame(self.root, bg="#1a1a2e")
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"{keyboard_name} Profile Switcher",
                 bg="#1a1a2e", fg="#50a0ff",
                 font=("Segoe UI", 13, "bold"),
                 pady=10).pack(side="left", padx=14)

        # ── Profiles frame
        pf = ttk.LabelFrame(self.root, text="Profiles  (1–5)", padding=10)
        pf.pack(fill="x", **PAD)

        self._profile_rows = []
        for i in range(5):
            row = tk.Frame(pf)
            row.pack(fill="x", pady=2)

            lbl = tk.Label(row, text=f"#{i+1}", width=3, anchor="w",
                           font=("Segoe UI", 10, "bold"))
            lbl.pack(side="left")

            var = tk.StringVar(value=self.cfg["profiles"][i])
            entry = ttk.Entry(row, textvariable=var, width=38, state="readonly")
            entry.pack(side="left", padx=(4, 4))

            btn_browse = ttk.Button(row, text="…", width=3,
                                    command=lambda idx=i: self._browse(idx))
            btn_browse.pack(side="left", padx=(0, 4))

            btn_clear = ttk.Button(row, text="✕", width=3,
                                   command=lambda idx=i: self._clear_slot(idx))
            btn_clear.pack(side="left")

            # Active indicator label
            active_lbl = tk.Label(row, text="", fg="#50ff80",
                                  font=("Segoe UI", 9, "bold"), width=8)
            active_lbl.pack(side="left", padx=(6, 0))

            self._profile_rows.append({"var": var, "active_lbl": active_lbl})

        # ── Hotkey config
        hkf = ttk.LabelFrame(self.root, text="Cycle hotkey", padding=10)
        hkf.pack(fill="x", **PAD)

        hk_row = tk.Frame(hkf)
        hk_row.pack(fill="x")
        tk.Label(hk_row, text="Combo:").pack(side="left")
        self._hotkey_var = tk.StringVar(value=self.cfg["hotkey"])
        hk_entry = ttk.Entry(hk_row, textvariable=self._hotkey_var, width=28)
        hk_entry.pack(side="left", padx=8)
        ttk.Button(hk_row, text="Apply", command=self._apply_hotkey).pack(side="left")

        tk.Label(hkf, text="pynput syntax, e.g.  <ctrl>+<alt>+<shift>+p",
                 font=("Segoe UI", 8), fg="gray").pack(anchor="w", pady=(4, 0))

        # ── Status bar
        self._status_var = tk.StringVar(value="Ready — keyboard not yet accessed.")
        sb = tk.Label(self.root, textvariable=self._status_var,
                      bd=1, relief="sunken", anchor="w", padx=8,
                      font=("Segoe UI", 9), fg="#444")
        sb.pack(fill="x", side="bottom")

        # ── Buttons
        bf = tk.Frame(self.root)
        bf.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Button(bf, text="Save config", command=self._save_and_apply).pack(side="left")
        ttk.Button(bf, text="Test connection", command=self._test_connection).pack(side="left", padx=8)
        ttk.Button(bf, text="Edit Hardware", command=self._edit_hardware).pack(side="left", padx=8)
        ttk.Button(bf, text="Hide to tray", command=self._hide).pack(side="right")

    # ── Profile list helpers ──────────────────────────────────────────────────

    def _refresh_profile_list(self):
        idx = self.cfg["current_index"]
        for i, row in enumerate(self._profile_rows):
            row["var"].set(self.cfg["profiles"][i])
            row["active_lbl"].config(text="▶ active" if i == idx else "")

    def _browse(self, slot: int):
        path = filedialog.askopenfilename(
            title=f"Select layout JSON for profile #{slot+1}",
            filetypes=[("VIA layout JSON", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.cfg["profiles"][slot] = path
            self._refresh_profile_list()

    def _clear_slot(self, slot: int):
        self.cfg["profiles"][slot] = ""
        self._refresh_profile_list()

    def _edit_hardware(self):
        self._show_hardware_setup()
        if self._is_hardware_configured():
            # Refresh the title
            keyboard_name = self.cfg["hardware"].get("keyboard_name", "QMK Keyboard")
            self.root.title(f"{keyboard_name} Profile Switcher")
            self._set_status("Hardware configuration updated.")

    # ── Hotkey ────────────────────────────────────────────────────────────────

    def _apply_hotkey(self):
        new_combo = self._hotkey_var.get().strip()
        self.cfg["hotkey"] = new_combo
        self._start_hotkey_listener()
        self._set_status(f"Hotkey updated → {new_combo}")

    def _start_hotkey_listener(self):
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        combo = self.cfg.get("hotkey", DEFAULT_CONFIG["hotkey"])
        try:
            self._hotkey_listener = keyboard.GlobalHotKeys(
                {combo: self._cycle_profile}
            )
            self._hotkey_listener.start()
            print(f"[hotkey] listening for {combo}")
        except Exception as e:
            self._set_status(f"Hotkey error: {e}")

    # ── Profile switching ─────────────────────────────────────────────────────

    def _cycle_profile(self):
        if self._busy:
            return
        active = [i for i, p in enumerate(self.cfg["profiles"]) if p]
        if not active:
            self._set_status("No profiles configured.")
            return
        cur = self.cfg["current_index"]
        try:
            pos = active.index(cur)
            next_pos = (pos + 1) % len(active)
        except ValueError:
            next_pos = 0
        next_idx = active[next_pos]
        self._load_profile(next_idx)

    def _load_profile(self, slot: int):
        path = self.cfg["profiles"][slot]
        if not path or not os.path.exists(path):
            self._set_status(f"Profile #{slot+1}: file not found.")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self._set_status(f"Profile #{slot+1}: JSON error — {e}")
            return

        self._busy = True
        self._set_status(f"Loading profile #{slot+1}…")
        t = threading.Thread(target=self._upload_thread, args=(slot, data), daemon=True)
        t.start()

    def _upload_thread(self, slot: int, layout_json: dict):
        dev = None
        try:
            hw = self.cfg["hardware"]
            dev, report_size, ver = via_probe_and_open(
                hw["vendor_id"], hw["product_id"],
                hw["usage_page"], hw["usage"]
            )
            if dev is None:
                self._set_status("Error: keyboard HID interface not found.")
                return

            if ver:
                print(f"[VIA] protocol version {ver:#06x}  report_size={report_size}")
            else:
                print(f"[VIA] no version response — using report_size={report_size}")

            fmt  = detect_json_format(layout_json)
            name = layout_json.get("name", f"Profile #{slot+1}")
            self._set_status(f"Uploading '{name}' [{fmt}, {report_size}-byte]...")
            print(f"[upload] format={fmt}  report_size={report_size}  name={name}")

            if fmt == "manufacturer":
                success = upload_manufacturer_json(
                    dev, layout_json, report_size, status_cb=self._set_status
                )
            else:
                success = upload_layout_json(
                    dev, layout_json, report_size,
                    hw["matrix_rows"], hw["matrix_cols"],
                    status_cb=self._set_status
                )

            if success:
                self.cfg["current_index"] = slot
                save_config(self.cfg)
                self.root.after(0, self._refresh_profile_list)
                self._set_status(f"Profile #{slot+1} '{name}' loaded.")
            else:
                self._set_status(f"Upload failed for profile #{slot+1}.")

        except Exception as e:
            self._set_status(f"HID error: {e}")
            print(f"[hid] exception: {e}")
            import traceback; traceback.print_exc()
        finally:
            if dev:
                try:
                    dev.close()
                except Exception:
                    pass
            self._busy = False

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _test_connection(self):
        dev = None
        try:
            hw = self.cfg["hardware"]
            dev, report_size, ver = via_probe_and_open(
                hw["vendor_id"], hw["product_id"],
                hw["usage_page"], hw["usage"]
            )
            if dev is None:
                self._set_status("Connection failed — HID interface not found.")
                messagebox.showerror("Connection test",
                                     f"Could not find {hw['keyboard_name']} VIA HID interface.\n"
                                     "Make sure the keyboard is plugged in.")
                return
            if ver:
                msg = (f"Connected!\n"
                       f"VIA protocol v{ver:#06x}\n"
                       f"Report size: {report_size} bytes\n"
                       f"Matrix: {hw['matrix_rows']} rows x {hw['matrix_cols']} cols")
            else:
                msg = (f"Connected (no protocol version response).\n"
                       f"Report size assumed: {report_size} bytes\n"
                       f"Try loading a profile — if keys don't change,\n"
                       f"report size detection may have failed.")
            self._set_status(msg.replace("\n", "  "))
            messagebox.showinfo("Connection test", msg)
        except Exception as e:
            self._set_status(f"Connection error: {e}")
            messagebox.showerror("Connection test", str(e))
        finally:
            if dev:
                try:
                    dev.close()
                except Exception:
                    pass

    def _save_and_apply(self):
        self.cfg["hotkey"] = self._hotkey_var.get().strip()
        save_config(self.cfg)
        self._start_hotkey_listener()
        self._set_status("Config saved.")

    def _set_status(self, msg: str):
        self.root.after(0, lambda: self._status_var.set(msg))
        print(f"[status] {msg}")

    # ── Window / tray ─────────────────────────────────────────────────────────

    def _hide(self):
        self.root.withdraw()

    def _show(self, icon=None, item_=None):
        self.root.after(0, self.root.deiconify)

    def _quit(self, icon=None, item_=None):
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        if self._tray_icon:
            self._tray_icon.stop()
        self.root.after(0, self.root.destroy)

    def _run_tray(self):
        img = make_tray_icon_image()
        keyboard_name = self.cfg["hardware"].get("keyboard_name", "QMK Keyboard")
        menu = (
            item("Show", self._show),
            item("Quit", self._quit),
        )
        self._tray_icon = Icon("QMKProfileSwitcher", img, f"{keyboard_name} Profile Switcher", menu)
        self._tray_icon.run()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = QMKProfileSwitcher(root)
    root.mainloop()
