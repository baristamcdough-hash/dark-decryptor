"""Fixture encoder — builds a structurally real darktunnel:// payload.

CFB-128 with ciphertext feedback: decoder.openssl_decrypt is the decryptor;
this is the matching encryptor (they are NOT mutual inverses by symmetry).
Test-only; the app itself never encrypts.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import msgpack  # noqa: E402
import pyaes  # noqa: E402

KEY2 = b'$B&E)H@McQfThWmZq4t7w!z%C*F-JaNd'
KEY = b'F)J@NcRfUjXn2r4u7x!A%D*G'
IV = bytes.fromhex('232e39185523184a5723586242200e05')


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii')


def openssl_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    aes = pyaes.AES(key)
    out = bytearray()
    prev_block = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        keystream = aes.encrypt(prev_block)
        enc_block = bytearray(b1 ^ b2 for b1, b2 in zip(block, keystream))
        out.extend(enc_block)
        if len(block) == 16:
            prev_block = bytes(enc_block)
        else:
            prev_block = bytes(enc_block) + prev_block[len(enc_block):]
    return bytes(out)


def encode_darktunnel(config: dict, prefix: bool = True) -> str:
    final = msgpack.packb(config, use_bin_type=True)
    encrypted_l2 = openssl_encrypt(final, KEY, IV)
    level1 = msgpack.packb({"EncryptedLockedConfig": encrypted_l2}, use_bin_type=True)
    encrypted_l1 = openssl_encrypt(level1, KEY2, IV)
    outer = {"encryptedLockedConfig": b64url(encrypted_l1)}
    blob = b64url(json.dumps(outer).encode('utf-8'))
    return ("darktunnel://" if prefix else "") + blob
