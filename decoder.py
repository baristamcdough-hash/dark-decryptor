"""DarkTunnel `.dark` decoder — pure logic, fully in-memory, no prints.

Refactored from the original script so the same engine can power:
  - the Telegram bot (bot.py)
  - a plain CLI (python decoder.py file.dark)

Give it bytes (file content) or a string (darktunnel:// URI / pasted base64)
and get back the decoded config dict. All errors raise DecodeError with a
human-friendly message.
"""
import base64
import json
import re
import struct
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

import pyaes


class DecodeError(Exception):
    """Raised when a payload cannot be decoded as a darktunnel config."""


# --------------------------------------------------------------------------
# Low-level crypto / msgpack (same as the original script)
# --------------------------------------------------------------------------

def openssl_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    aes = pyaes.AES(key)
    plaintext = bytearray()
    prev_block = iv

    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        keystream = aes.encrypt(prev_block)
        decrypted_block = bytearray(b1 ^ b2 for b1, b2 in zip(block, keystream))
        plaintext.extend(decrypted_block)

        if len(block) == 16:
            prev_block = block
        else:
            prev_block = block + prev_block[len(block):]

    return bytes(plaintext)


class MsgpackDecoder:
    def __init__(self):
        self.data = b""
        self.offset = 0
        self.length = 0

    def unpack(self, data: bytes):
        self.data = data
        self.offset = 0
        self.length = len(data)
        value = self._read_value()
        if self.offset != self.length:
            raise RuntimeError(f"Extra unread bytes at offset {self.offset}")
        return value

    def _read_value(self):
        byte = self._read_byte()
        if byte <= 0x7F: return byte
        if (byte & 0xF0) == 0x80: return self._read_map(byte & 0x0F)
        if (byte & 0xF0) == 0x90: return self._read_array(byte & 0x0F)
        if (byte & 0xE0) == 0xA0: return self._read_string(byte & 0x1F)
        if byte >= 0xE0: return byte - 256

        if byte == 0xC0: return None
        elif byte == 0xC2: return False
        elif byte == 0xC3: return True
        elif byte == 0xC4: return self._read_binary(self._read_uint8())
        elif byte == 0xC5: return self._read_binary(self._read_uint16())
        elif byte == 0xC6: return self._read_binary(self._read_uint32())
        elif byte == 0xCA: return self._read_float32()
        elif byte == 0xCB: return self._read_float64()
        elif byte == 0xCC: return self._read_uint8()
        elif byte == 0xCD: return self._read_uint16()
        elif byte == 0xCE: return self._read_uint32()
        elif byte == 0xCF: return self._read_uint64()
        elif byte == 0xD0: return self._read_int8()
        elif byte == 0xD1: return self._read_int16()
        elif byte == 0xD2: return self._read_int32()
        elif byte == 0xD3: return self._read_int64()
        elif byte == 0xD9: return self._read_string(self._read_uint8())
        elif byte == 0xDA: return self._read_string(self._read_uint16())
        elif byte == 0xDB: return self._read_string(self._read_uint32())
        elif byte == 0xD4: return self._read_fixext(1)
        elif byte == 0xD5: return self._read_fixext(2)
        elif byte == 0xD6: return self._read_fixext(4)
        elif byte == 0xD7: return self._read_fixext(8)
        elif byte == 0xD8: return self._read_fixext(16)
        elif byte == 0xDC: return self._read_array(self._read_uint16())
        elif byte == 0xDD: return self._read_array(self._read_uint32())
        elif byte == 0xDE: return self._read_map(self._read_uint16())
        elif byte == 0xDF: return self._read_map(self._read_uint32())
        raise RuntimeError(f"Unsupported MessagePack type: 0x{byte:02x}")

    def _read_array(self, length: int):
        return [self._read_value() for _ in range(length)]

    def _read_map(self, length: int):
        items = {}
        for _ in range(length):
            key = self._read_value()
            if isinstance(key, bytes):
                try: key = key.decode('utf-8')
                except UnicodeDecodeError: key = key.decode('latin-1')
            elif not isinstance(key, (int, str)):
                key = json.dumps(key, ensure_ascii=False, separators=(',', ':'))
            items[key] = self._read_value()
        return items

    def _read_string(self, length: int) -> str:
        b = self._read_bytes(length)
        try: return b.decode('utf-8')
        except UnicodeDecodeError: return b.decode('latin-1')

    def _read_binary(self, length: int) -> bytes:
        return self._read_bytes(length)

    def _read_fixext(self, length: int) -> bytes:
        self._read_byte()
        return self._read_bytes(length)

    def _read_float32(self) -> float: return struct.unpack('>f', self._read_bytes(4))[0]
    def _read_float64(self) -> float: return struct.unpack('>d', self._read_bytes(8))[0]
    def _read_uint8(self) -> int: return self._read_byte()
    def _read_uint16(self) -> int: return struct.unpack('>H', self._read_bytes(2))[0]
    def _read_uint32(self) -> int: return struct.unpack('>I', self._read_bytes(4))[0]
    def _read_uint64(self) -> int: return struct.unpack('>Q', self._read_bytes(8))[0]
    def _read_int8(self) -> int: return struct.unpack('>b', self._read_bytes(1))[0]
    def _read_int16(self) -> int: return struct.unpack('>h', self._read_bytes(2))[0]
    def _read_int32(self) -> int: return struct.unpack('>i', self._read_bytes(4))[0]
    def _read_int64(self) -> int: return struct.unpack('>q', self._read_bytes(8))[0]

    def _read_byte(self) -> int:
        if self.offset >= self.length: raise RuntimeError("Unexpected end of data")
        byte = self.data[self.offset]
        self.offset += 1
        return byte

    def _read_bytes(self, length: int) -> bytes:
        if length < 0 or (self.offset + length) > self.length: raise RuntimeError("End of data")
        chunk = self.data[self.offset:self.offset + length]
        self.offset += length
        return chunk


# --------------------------------------------------------------------------
# DarkTunnel constants
# --------------------------------------------------------------------------

_KEY2 = b'$B&E)H@McQfThWmZq4t7w!z%C*F-JaNd'
_KEY = b'F)J@NcRfUjXn2r4u7x!A%D*G'
_IV = bytes.fromhex('232e39185523184a5723586242200e05')


# --------------------------------------------------------------------------
# Cleaning helpers
# --------------------------------------------------------------------------

def base64_decode_safe(data: str) -> bytes:
    data = data.replace('-', '+').replace('_', '/')
    pad = len(data) % 4
    if pad:
        data += '=' * (4 - pad)
    return base64.b64decode(data)


def clean_encrypted(data, key: bytes, iv: bytes):
    if isinstance(data, dict):
        new_data = {}
        for k, v in data.items():
            if isinstance(k, bytes):
                try: k = k.decode('utf-8')
                except UnicodeDecodeError: k = k.decode('latin-1')

            if isinstance(v, (dict, list)):
                new_data[k] = clean_encrypted(v, key, iv)
                continue

            if isinstance(k, str) and k.startswith('Encrypted') and v:
                try:
                    v_bytes = base64_decode_safe(v) if isinstance(v, str) and not v.startswith('{') else v
                    if isinstance(v_bytes, bytes):
                        dec = openssl_decrypt(v_bytes, key, iv)
                        try:
                            dec_str = dec.decode('utf-8').strip()
                            try: new_data[k] = json.loads(dec_str)
                            except ValueError: new_data[k] = dec_str
                        except UnicodeDecodeError:
                            new_data[k] = dec.decode('latin-1')
                    else:
                        new_data[k] = v
                except Exception:
                    new_data[k] = v
            else:
                if isinstance(v, bytes):
                    try: new_data[k] = v.decode('utf-8')
                    except UnicodeDecodeError: new_data[k] = v.decode('latin-1')
                else:
                    new_data[k] = v
        return new_data

    elif isinstance(data, list):
        cleaned_list = []
        for item in data:
            if isinstance(item, bytes):
                try: cleaned_list.append(item.decode('utf-8'))
                except UnicodeDecodeError: cleaned_list.append(item.decode('latin-1'))
            else:
                cleaned_list.append(clean_encrypted(item, key, iv))
        return cleaned_list

    elif isinstance(data, bytes):
        try: return data.decode('utf-8')
        except UnicodeDecodeError: return data.decode('latin-1')

    return data


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def decode_darktunnel(data: Union[bytes, bytearray, str]) -> dict:
    """Decode a darktunnel payload in memory.

    Accepts file bytes, the file content as text, a `darktunnel://` URI,
    or the raw base64 blob. Returns the decoded config dict.
    """
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data).decode('utf-8', errors='ignore')
    else:
        raw = str(data)
    raw = raw.strip().strip('\ufeff')
    if not raw:
        raise DecodeError("The file/message is empty.")

    raw = raw.replace('darktunnel://', '').strip()

    outer = _parse_outer_json(raw)
    if not isinstance(outer, dict) or 'encryptedLockedConfig' not in outer:
        raise DecodeError(
            "Not a darktunnel config: missing the 'encryptedLockedConfig' field."
        )

    try:
        config_bytes = base64_decode_safe(outer['encryptedLockedConfig'])
        decrypted_l1 = openssl_decrypt(config_bytes, _KEY2, _IV)

        unpacked_l1 = MsgpackDecoder().unpack(decrypted_l1)
        nested_input = unpacked_l1['EncryptedLockedConfig']

        if isinstance(nested_input, str):
            nested_input = nested_input.encode('latin-1')

        decrypted_l2 = openssl_decrypt(nested_input, _KEY, _IV)
        final_payload = MsgpackDecoder().unpack(decrypted_l2)
    except DecodeError:
        raise
    except Exception as exc:
        raise DecodeError(
            f"Decryption failed ({exc}). Is this really a .dark config?"
        ) from exc

    return clean_encrypted(final_payload, _KEY, _IV)


def _parse_outer_json(raw: str):
    """The outer layer is JSON, sometimes itself base64-encoded."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    try:
        return json.loads(base64_decode_safe(raw).decode('utf-8', errors='ignore'))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Human-readable summary (what the bot shows in chat)
# --------------------------------------------------------------------------

_FIELDS = [
    ("name",       "📛 Name",       ("name", "remark", "comment", "title", "ps", "remarks")),
    ("type",       "🔌 Type",       ("config_type", "type", "protocol", "proxy_type", "sub_type")),
    ("address",    "🌐 Address",    ("address", "server", "hostname", "domain", "server_address")),
    ("port",       "🔢 Port",       ("port",)),
    ("uuid",       "🔑 UUID",       ("id", "uuid", "client_id", "clientId")),
    ("password",   "🔑 Password",   ("password", "pass", "psk")),
    ("public_key", "🔓 Public key", ("public_key", "publicKey", "pbk")),
    ("security",   "🛡️ Security",   ("security", "tls", "tls_security", "encryption_security")),
    ("sni",        "🎭 SNI",        ("sni", "server_name", "serverName", "servername")),
    ("fingerprint","🖐️ Fingerprint", ("fingerprint", "fp")),
    ("alpn",       "🧩 ALPN",       ("alpn",)),
    ("network",    "🌍 Network",    ("network", "transport", "net")),
    ("host",       "🏠 Host",       ("host", "ws_host", "websocket_host", "http_host", "grpc_host")),
    ("path",       "🛤️ Path",       ("path", "ws_path", "websocket_path", "service_name", "serviceName", "grpc_service_name")),
    ("flow",       "✈️ Flow",       ("flow", "flow_control")),
    ("short_id",   "🆔 Short ID",   ("short_id", "shortId", "sid")),
    ("method",     "🧮 Method",     ("method", "cipher", "encryption")),
    ("email",      "📧 Email",      ("email",)),
]


def _find_value(cfg: dict, names: tuple):
    lower = {str(k).lower(): v for k, v in cfg.items()}
    for n in names:
        if n in cfg and cfg[n] not in (None, ""):
            return cfg[n]
    for n in names:
        if n.lower() in lower and lower[n.lower()] not in (None, ""):
            return lower[n.lower()]
    return None


def summarize(cfg: Any) -> str:
    """Build a short human-readable card from a decoded config."""
    if not isinstance(cfg, dict):
        snippet = json.dumps(cfg, ensure_ascii=False)[:200]
        return f"✅ Decoded successfully!\n\n(Unusual structure: {snippet}…)\n\n📦 Full JSON sent as file."

    lines = ["✅ Decoded successfully!\n"]
    for _, label, names in _FIELDS:
        value = _find_value(cfg, names)
        if value is None:
            continue
        text = str(value)
        if len(text) > 120:
            text = text[:117] + "…"
        lines.append(f"{label}: {text}")

    lines.append(f"\n📦 {len(cfg)} top-level keys — full JSON sent as file.")
    return "\n".join(lines)


def suggest_filename(cfg: Any) -> str:
    """Pick a nice output filename from the config name, if present."""
    if isinstance(cfg, dict):
        name = _find_value(cfg, ("name", "remark", "comment", "title", "ps"))
        if name:
            clean = re.sub(r'[^\w\-\. ]+', '', str(name)).strip()
            if clean:
                return clean[:60] + ".json"
    return "decoded_config.json"


def sign_result(result: Any, by: str = "@Poriot_ke · PoriotCloud Vault") -> dict:
    """Stamp the decoded config with credits before it's stored/sent.

    The signature block is placed at the top of the JSON so it's the first
    thing anyone sees. Idempotent — re-signing overwrites the same keys.
    """
    if not isinstance(result, dict):
        result = {"value": result}
    stamped = dict(result)
    stamped.pop("_decoded_by", None)
    stamped.pop("_decoded_at", None)
    stamped.pop("_vault", None)
    return {
        "_decoded_by": by,
        "_decoded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **stamped,
    }


# --------------------------------------------------------------------------
# CLI (keeps the original script's use case alive)
# --------------------------------------------------------------------------

def _cli_main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("Usage: python decoder.py <file.dark> [output.json]")
        return 0 if argv and argv[0] in ("-h", "--help") else 1

    file_path = argv[0]
    try:
        with open(file_path, 'rb') as f:
            payload = f.read()
    except OSError as exc:
        print(f"❌ Cannot read {file_path}: {exc}")
        return 1

    try:
        result = decode_darktunnel(payload)
    except DecodeError as exc:
        print(f"❌ {exc}")
        return 1

    out_path = argv[1] if len(argv) > 1 else file_path.replace('.dark', '.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=4, ensure_ascii=False)

    print(summarize(result))
    print(f"\n✅ Decoded file saved to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
