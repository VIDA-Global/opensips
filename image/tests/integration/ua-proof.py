"""Exercise real upstream UAS/UAC renegotiation without external SIP or media."""

import select
import socket
import subprocess
import time
import argparse
import json
import os
import signal
import sqlite3
from pathlib import Path


def sdp(port, version=1):
    return (f"v=0\r\no=peer 1 {version} IN IP4 127.0.0.1\r\ns=proof\r\n"
            f"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {port} RTP/AVP 0\r\na=sendrecv\r\n")


def parse(data):
    header, separator, body = data.decode("ascii").partition("\r\n\r\n")
    if not separator:
        raise AssertionError("incomplete SIP framing")
    lines = header.split("\r\n")
    fields = {}
    for line in lines[1:]:
        name, value = line.split(":", 1)
        fields.setdefault(name.lower(), []).append(value.strip())
    if int(fields["content-length"][0]) != len(body):
        raise AssertionError("incorrect SIP content length")
    return lines[0], fields, body


def encode(start, headers, body=""):
    return (start + "\r\n" + "\r\n".join(headers)
            + f"\r\nContent-Length: {len(body)}\r\n\r\n" + body).encode("ascii")


def reply(fields, port, body):
    to = fields["to"][0]
    if ";tag=" not in to:
        to += ";tag=backend-proof"
    return encode("SIP/2.0 200 OK", [
        *("Via: " + via for via in fields["via"]),
        "From: " + fields["from"][0], "To: " + to,
        "Call-ID: " + fields["call-id"][0], "CSeq: " + fields["cseq"][0],
        f"Contact: <sip:peer@127.0.0.1:{port}>", "Content-Type: application/sdp",
    ], body)


def run(verify_persistence=False):
    log = Path("/tmp/ua-proof.log")
    config = Path("/tests/ua-proof.cfg")
    if verify_persistence:
        with sqlite3.connect("/tmp/ua.sqlite") as database:
            database.executescript("CREATE TABLE version(table_name TEXT, table_version INTEGER);")
            database.executescript(Path("/source/scripts/sqlite/b2b-create.sql").read_text())
        contents = config.read_text().replace('modparam("b2b_entities", "db_mode", 0)',
            'modparam("b2b_entities", "db_mode", 1)\n'
            'modparam("b2b_entities", "db_url", "sqlite:///tmp/ua.sqlite")')
        contents = contents.replace('loadmodule "b2b_entities.so"',
                                    'loadmodule "db_sqlite.so"\nloadmodule "b2b_entities.so"')
        config = Path("/tmp/ua-proof.cfg")
        config.write_text(contents)
    with log.open("wb") as stream:
        process = subprocess.Popen(["opensips", "-F", "-f", str(config),
                                    "-P", "/tmp/ua-proof.pid"], stdout=stream, stderr=stream,
                                    start_new_session=True)
    caller = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    backend = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    caller.bind(("127.0.0.1", 15062))
    backend.bind(("127.0.0.1", 15064))
    caller.settimeout(2)
    backend.settimeout(2)
    destination = ("127.0.0.1", 15060)
    try:
        options = encode("OPTIONS sip:ready@127.0.0.1:15060 SIP/2.0", [
            "Via: SIP/2.0/UDP 127.0.0.1:15062;branch=z9hG4bKready",
            "From: <sip:caller@localhost>;tag=source-proof", "To: <sip:ready@localhost>",
            "Call-ID: readiness-proof", "CSeq: 1 OPTIONS", "Max-Forwards: 10",
        ])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("OpenSIPS failed startup: " + log.read_text()[-4000:])
            caller.sendto(options, destination)
            if select.select([caller], [], [], 0.1)[0]:
                caller.recvfrom(65535)
                break
        else:
            raise AssertionError("OpenSIPS did not become ready")
        caller.sendto(encode("INVITE sip:record@127.0.0.1:15060 SIP/2.0", [
            "Via: SIP/2.0/UDP 127.0.0.1:15062;branch=z9hG4bKinitial",
            "From: <sip:caller@localhost>;tag=source-proof", "To: <sip:record@localhost>",
            "Call-ID: source-proof", "CSeq: 1 INVITE", "Max-Forwards: 10",
            "Contact: <sip:caller@127.0.0.1:15062>", "Content-Type: application/sdp",
        ], sdp(40000)), destination)
        backend_call = None
        backend_sequence = None
        caller_to = None
        recovered = set()
        expected_acks = {}
        acknowledged = set()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for sock in select.select([caller, backend], [], [], 0.1)[0]:
                packet, address = sock.recvfrom(65535)
                start, fields, body = parse(packet)
                if start.startswith("INVITE"):
                    if sock is backend:
                        if backend_call is None:
                            backend_call = fields["call-id"][0]
                            backend_sequence = int(fields["cseq"][0].split()[0])
                        else:
                            assert fields["call-id"][0] == backend_call
                            assert int(fields["cseq"][0].split()[0]) > backend_sequence
                            assert "m=audio 30004 " in body
                            recovered.add("backend")
                            expected_acks["backend"] = fields["cseq"][0].split()[0]
                        sock.sendto(reply(fields, 15064, sdp(40004)), address)
                    else:
                        assert fields["call-id"][0] == "source-proof"
                        assert fields["to"][0].endswith(";tag=source-proof")
                        assert "m=audio 30002 " in body
                        recovered.add("caller")
                        expected_acks["caller"] = fields["cseq"][0].split()[0]
                        sock.sendto(reply(fields, 15062, sdp(40000, 2)), address)
                elif start.startswith("ACK"):
                    side = "caller" if sock is caller else "backend"
                    if fields["cseq"][0].split()[0] == expected_acks.get(side):
                        acknowledged.add(side)
                elif sock is caller and start.startswith("SIP/2.0 200"):
                    if fields["cseq"][0] != "1 INVITE":
                        continue
                    caller_to = fields["to"][0]
                    caller.sendto(encode("ACK sip:record@127.0.0.1:15060 SIP/2.0", [
                        "Via: SIP/2.0/UDP 127.0.0.1:15062;branch=z9hG4bKack",
                        "From: <sip:caller@localhost>;tag=source-proof", "To: " + caller_to,
                        "Call-ID: source-proof", "CSeq: 1 ACK", "Max-Forwards: 10",
                    ]), destination)
            if recovered == acknowledged == {"caller", "backend"}:
                assert caller_to is not None and backend_call != "source-proof"
                print("Upstream UA API preserved both dialog Call-IDs through acknowledged re-INVITEs")
                if verify_persistence:
                    caller.sendto(options, destination)
                    packet, _ = caller.recvfrom(65535)
                    _, before, _ = parse(packet)
                    before_count = len(json.loads(before["x-proof-ua"][0]))
                    assert before_count == 2
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                    with log.open("ab") as stream:
                        process = subprocess.Popen(["opensips", "-F", "-f", str(config),
                            "-P", "/tmp/ua-proof.pid"], stdout=stream, stderr=stream,
                            start_new_session=True)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        caller.sendto(options, destination)
                        if select.select([caller], [], [], 0.1)[0]:
                            packet, _ = caller.recvfrom(65535)
                            _, after, _ = parse(packet)
                            restored = len(json.loads(after["x-proof-ua"][0]))
                            entities = len(json.loads(after["x-proof-entities"][0]))
                            print(f"Database restore: entities={entities}, UA sessions={restored}")
                            assert restored == before_count, "UA identity was not restored"
                            break
                    else:
                        raise AssertionError("restart failed: " + log.read_text()[-4000:])
                return
        raise AssertionError("B2B renegotiation did not complete: " + log.read_text()[-4000:])
    finally:
        caller.close()
        backend.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-persistence", action="store_true")
    run(parser.parse_args().verify_persistence)
