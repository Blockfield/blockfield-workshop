"""Minimal SLP status ping to read the proxy MOTD/description: ping.py [host] [port]"""

import socket
import struct
import json
import sys


def vi(n):
    b = b""
    while True:
        x = n & 0x7F
        n >>= 7
        b += bytes([x | (0x80 if n else 0)])
        if not n:
            return b


def rvi(s):
    n = sh = 0
    while True:
        x = s.recv(1)[0]
        n |= (x & 0x7F) << sh
        sh += 7
        if not x & 0x80:
            return n


host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 25598
s = socket.create_connection((host, port))
h = b"\x00" + vi(767) + vi(len(host)) + host.encode() + struct.pack(">H", port) + vi(1)
s.send(vi(len(h)) + h + b"\x01\x00")
rvi(s)
rvi(s)
packet_length = rvi(s)
d = b""
while len(d) < packet_length:
    d += s.recv(packet_length - len(d))
print(json.loads(d)["description"])
