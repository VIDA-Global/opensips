"""Test readiness probes actual SIP response and placement PostgreSQL access."""

import os
import socket
import urllib.request
from uuid import uuid4

host = os.environ["NATIVE_MEDIA_IP"]
sip_port = int(os.environ.get("NATIVE_SIP_PORT", "5060"))
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
    peer.settimeout(1)
    peer.bind((host, 0))
    port = peer.getsockname()[1]
    message = (f"OPTIONS sip:health@{host}:{sip_port} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {host}:{port};branch=z9hG4bK{uuid4().hex};rport\r\n"
               f"From: <sip:health@{host}>;tag=probe\r\nTo: <sip:health@{host}>\r\n"
               f"Call-ID: native-health-{uuid4().hex}\r\nCSeq: 1 OPTIONS\r\nMax-Forwards: 10\r\nContent-Length: 0\r\n\r\n")
    peer.sendto(message.encode(), (host, sip_port))
    assert peer.recv(4096).startswith(b"SIP/2.0 200 ")
if os.environ.get("SAGE_STOCK_DEMUX_NODE") != "1":
    request = urllib.request.Request("http://127.0.0.1:8095/readyz", headers={"Authorization": "Bearer " + "1"*64})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=1) as response:
        assert response.status == 200
