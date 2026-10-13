"""
Phase 9B-7A backup image build step (first used as Phase 9B-6G test tooling, unchanged in behavior): fetch the PostgreSQL
Global Development Group (PGDG) apt signing key and refuse it unless its
OpenPGP v4 primary-key fingerprint is exactly the published one. The base image has no curl or gpg, so this uses only
the Python standard library. The verified key is written where apt's `signed-by` will use it; apt then performs its own
signature and checksum verification of the repository and packages, which is never disabled.

The v4 fingerprint is SHA-1 over 0x99, a two-byte length and the public-key packet body (RFC 4880 section 12.2).
"""
import base64
import hashlib
import sys
import urllib.request

URL = "https://www.postgresql.org/media/keys/ACCC4CF8.asc"
EXPECTED = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"


def first_public_key_body(data: bytes) -> bytes:
    i = 0
    while i < len(data):
        tag_byte = data[i]
        if not tag_byte & 0x80:
            raise SystemExit("not an OpenPGP packet stream")
        if tag_byte & 0x40:  # new-format header
            tag, i = tag_byte & 0x3F, i + 1
            first = data[i]
            if first < 192:
                length, i = first, i + 1
            elif first < 224:
                length, i = ((first - 192) << 8) + data[i + 1] + 192, i + 2
            else:
                length, i = int.from_bytes(data[i + 1:i + 5], "big"), i + 5
        else:  # old-format header
            tag, length_type, i = (tag_byte >> 2) & 0x0F, tag_byte & 0x03, i + 1
            size = {0: 1, 1: 2, 2: 4}[length_type]
            length, i = int.from_bytes(data[i:i + size], "big"), i + size
        body = data[i:i + length]
        if tag == 6:
            return body
        i += length
    raise SystemExit("no public-key packet")


def main(out_path: str) -> None:
    armored = urllib.request.urlopen(URL, timeout=30).read()
    text = armored.decode("ascii")
    lines = text.split("-----BEGIN PGP PUBLIC KEY BLOCK-----", 1)[1].split("-----END PGP PUBLIC KEY BLOCK-----", 1)[0].strip().splitlines()
    payload = "".join(line for line in lines if line and ":" not in line and not line.startswith("="))
    body = first_public_key_body(base64.b64decode(payload))
    if body[0] != 4:
        raise SystemExit("not a version 4 key")
    fingerprint = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
    if fingerprint != EXPECTED:
        raise SystemExit(f"PGDG key fingerprint mismatch: {fingerprint}")
    with open(out_path, "wb") as handle:
        handle.write(armored)
    print(f"PGDG key verified: {fingerprint} sha256={hashlib.sha256(armored).hexdigest()}")


if __name__ == "__main__":
    main(sys.argv[1])
