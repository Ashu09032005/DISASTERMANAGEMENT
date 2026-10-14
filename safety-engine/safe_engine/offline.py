"""Offline / low-bandwidth support.

* Region pack: everything a client needs to keep evaluating routes when the
  network drops — hazard slices for the next hours, shelters, SOP text and (for
  the built-in engine) the road-graph partition — plus a TTL so the client can
  apply a staleness penalty.
* Compact route code: a route is re-creatable from origin, via-points and
  destination on the same engine, so only those points are sent
  (zig-zag varint deltas at 1e-5 deg, base64url) — typically < 40 characters,
  small enough for SMS or a BLE / mesh message.
"""
from __future__ import annotations

import base64
import hashlib


def _zz(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _unzz(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def encode_points(points_lonlat: list, vehicle_code: int = 0) -> str:
    buf = bytearray([1, vehicle_code & 0xFF])  # version, vehicle
    plon = plat = 0
    for lon, lat in points_lonlat:
        ilon, ilat = int(round(lon * 1e5)), int(round(lat * 1e5))
        buf += _varint(_zz(ilon - plon)) + _varint(_zz(ilat - plat))
        plon, plat = ilon, ilat
    chk = hashlib.sha1(bytes(buf)).digest()[:2]
    return base64.urlsafe_b64encode(bytes(buf) + chk).decode().rstrip("=")


def decode_points(code: str) -> tuple[list, int]:
    raw = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
    body, chk = raw[:-2], raw[-2:]
    if hashlib.sha1(body).digest()[:2] != chk:
        raise ValueError("route code checksum mismatch")
    if body[0] != 1:
        raise ValueError("unsupported route code version")
    vehicle = body[1]
    pts, i, vals = [], 2, []
    while i < len(body):
        n, shift = 0, 0
        while True:
            b = body[i]; i += 1
            n |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        vals.append(_unzz(n))
    plon = plat = 0
    for k in range(0, len(vals), 2):
        plon += vals[k]; plat += vals[k + 1]
        pts.append((plon / 1e5, plat / 1e5))
    return pts, vehicle
