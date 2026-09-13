"""Verify stock SIPREC demultiplexing into independent single-active-stream calls."""

from contextlib import ExitStack
import json
from pathlib import Path
import re
import runpy
import select
import socket
import struct
import subprocess
import sys
import time


CONFIG = '''log_level=2
stderror_enabled=yes
syslog_enabled=no
socket=udp:127.0.0.1:15060
mpath="/modules/"
loadmodule "proto_udp.so"
loadmodule "signaling.so"
loadmodule "sl.so"
loadmodule "tm.so"
loadmodule "sipmsgops.so"
loadmodule "/source/modules/b2b_entities/b2b_entities.so"
loadmodule "/source/modules/b2b_sdp_demux/b2b_sdp_demux.so"
modparam("b2b_entities", "db_mode", 0)
modparam("b2b_sdp_demux", "client_bye_mode", "terminate")
route {
    if (is_method("OPTIONS")) { sl_send_reply(200, "OK"); exit; }
    if (is_method("INVITE") && !has_totag()) {
        b2b_sdp_demux("sip:record@127.0.0.1:15062");
        exit;
    }
    sl_send_reply(405, "No Method");
}
'''


def answer_request(peer, packet, address, body, tag, fields):
    headers = "".join(f"{key}: {value.strip()}\r\n"
                      for key in ("Via", "From", "Call-ID", "CSeq", "Record-Route")
                      for value in fields(packet, key))
    to = fields(packet, "To")[0].strip()
    if ";tag=" not in to:
        to += f";tag={tag}"
    peer.sendto(("SIP/2.0 200 OK\r\n" + headers + f"To: {to}\r\n"
                 f"Contact: <sip:peer@127.0.0.1:{peer.getsockname()[1]}>\r\n"
                 f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode(), address)


def two_stream_media(sources, receivers, sdp, marker, start_sequence=0):
    targets = [int(port) for port in re.findall(r"m=audio (\d+)", sdp)]
    assert len(targets) == 2
    received = [False, False]
    for sequence in range(start_sequence, start_sequence + 60):
        for index, peer in enumerate(sources):
            packet = struct.pack("!BBHII", 0x80, 8, sequence, sequence * 160, 100 + index)
            peer.sendto(packet + bytes([marker + index]) * 160, ("127.0.0.1", targets[index]))
        for index, peer in enumerate(receivers):
            try:
                received[index] |= peer.recv(4096)[12:] == bytes([marker + index]) * 160
            except TimeoutError:
                pass
        if receivers and all(received):
            return
        time.sleep(0.02)
    if not receivers:
        return
    raise AssertionError("participant-separated media did not reach both downstream receivers")


def media_update(source, backend, answer, original_sdp, fields, freeswitch=False):
    routes = "".join("Route: " + value.strip() + "\r\n" for value in fields(answer, "Record-Route"))
    contact = fields(answer, "Contact")[0].strip().strip("<>")
    headers = "".join(f"{key}: {fields(answer, key)[0].strip()}\r\n"
                      for key in ("From", "To", "Call-ID"))
    source.sendto((f"ACK {contact} SIP/2.0\r\n"
                   "Via: SIP/2.0/UDP 127.0.0.1:15061;branch=z9hG4bK-demux-ack\r\n" +
                   routes + headers + "CSeq: 1 ACK\r\nMax-Forwards: 16\r\nContent-Length: 0\r\n\r\n").encode(),
                  ("127.0.0.1", 15060))
    with ExitStack() as stack:
        ports = (30000, 30002) if freeswitch else (30000, 30002, 40000, 40002)
        peers = [stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) for _ in ports]
        for peer, port in zip(peers, ports):
            peer.bind(("127.0.0.1", port))
            peer.settimeout(0.1)
        two_stream_media(peers[:2], peers[2:], answer, 41)
        mi = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
        mi.bind("/tmp/demux-media-client.sock")
        mi.settimeout(5)
        mi.sendto(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "rtp_relay_update_callid",
                             "params": {"callid": "demux-proof", "engine": "rtpengine", "set": 2,
                                        "node": "udp:127.0.0.1:2224"}}).encode(), "/tmp/reanchor-mi.sock")
        updated_source = None
        updated_backends = set()
        deadline = time.monotonic() + 10
        expected_backends = 0 if freeswitch else 2
        while time.monotonic() < deadline and (updated_source is None or len(updated_backends) < expected_backends):
            ready, _, _ = select.select([source, backend], [], [], 0.2)
            for peer in ready:
                raw, address = peer.recvfrom(65535)
                packet = raw.decode()
                if not packet.startswith("INVITE "):
                    continue
                if peer is source:
                    updated_source = packet
                    answer_request(peer, packet, address, original_sdp, "source", fields)
                else:
                    body = packet.split("\r\n\r\n", 1)[1]
                    label = int(re.search(r"a=label:(\d+)", body)[1])
                    assert label in (1, 2)
                    updated_backends.add(fields(packet, "Call-ID")[0].strip())
                    body = re.sub(r"m=audio ([1-9][0-9]*)", f"m=audio {39998 + label * 2}", body)
                    answer_request(peer, packet, address, body.replace("a=sendonly", "a=recvonly"),
                                   f"backend-{label}", fields)
        assert updated_source is not None and len(updated_backends) == expected_backends
        result = json.loads(mi.recv(65535))
        assert result == {"jsonrpc": "2.0", "id": 1, "result": "Sessions: 1"}, result
        assert all(12000 <= int(port) < 13000 for port in re.findall(r"m=audio (\d+)", updated_source))
        two_stream_media(peers[:2], peers[2:], updated_source, 73, start_sequence=60)
        if freeswitch:
            source.sendto((f"BYE {contact} SIP/2.0\r\n"
                           "Via: SIP/2.0/UDP 127.0.0.1:15061;branch=z9hG4bK-demux-bye\r\n" +
                           routes + headers + "CSeq: 9 BYE\r\nMax-Forwards: 16\r\nContent-Length: 0\r\n\r\n").encode(),
                          ("127.0.0.1", 15060))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    packet = source.recv(65535).decode()
                except TimeoutError:
                    continue
                if packet.startswith("SIP/2.0 200") and fields(packet, "CSeq")[0].strip() == "9 BYE":
                    break
            else:
                raise AssertionError("demux teardown did not complete")
            print("stock proxy + demux renegotiated real FreeSWITCH legs; recording validation follows")
        else:
            print("stock proxy + demux passed: two separate RTP streams before and after relay replacement")


def main():
    freeswitch = sys.argv[1:] == ["--freeswitch"]
    media = freeswitch or sys.argv[1:] == ["--media"]
    assert not sys.argv[1:] or media, "unsupported proof arguments"
    helpers = runpy.run_path("/tests/proxy-reanchor-proof.py")
    fields, receive = helpers["fields"], helpers["receive"]
    config = Path("/tmp/demux.cfg")
    config.write_text(CONFIG.replace("socket=udp:127.0.0.1:15060", "socket=udp:127.0.0.1:15070")
                      if media else CONFIG)
    process = None
    proxy = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as source, socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        ) as backend:
            source.bind(("127.0.0.1", 15061))
            backend.bind(("127.0.0.1", 0 if freeswitch else 15062))
            source.settimeout(0.1)
            backend.settimeout(0.1)
            with Path("/tmp/demux.log").open("wb") as log:
                process = subprocess.Popen(["opensips", "-F", "-f", str(config)], stdout=log, stderr=log)
            if media:
                proxy_config = Path("/tmp/demux-proxy.cfg")
                proxy_config.write_text(helpers["CONFIG"].replace("sip:backend@127.0.0.1:15062",
                                                                 "sip:demux@127.0.0.1:15070"))
                with Path("/tmp/demux-proxy.log").open("wb") as log:
                    proxy = subprocess.Popen(["opensips", "-F", "-f", str(proxy_config)],
                                             stdout=log, stderr=log)
            ready = ("OPTIONS sip:edge@127.0.0.1 SIP/2.0\r\n"
                     "Via: SIP/2.0/UDP 127.0.0.1:15061;branch=z9hG4bK-ready\r\n"
                     "From: <sip:source@localhost>;tag=ready\r\nTo: <sip:edge@localhost>\r\n"
                     "Call-ID: ready\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n")
            for _ in range(50):
                source.sendto(ready.encode(), ("127.0.0.1", 15060))
                try:
                    if source.recv(4096).startswith(b"SIP/2.0 200"):
                        break
                except TimeoutError:
                    if process.poll() is not None:
                        raise AssertionError(Path("/tmp/demux.log").read_text()[-4096:])
                    time.sleep(0.02)
            else:
                raise AssertionError("stock demux startup timeout")
            sdp = ("v=0\r\no=source 1 1 IN IP4 127.0.0.1\r\ns=demux\r\n"
                   "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                   "m=audio 30000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=label:1\r\na=sendonly\r\n"
                   "m=audio 30002 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=label:2\r\na=sendonly\r\n")
            body = ("--proof\r\nContent-Type: application/sdp\r\n\r\n" + sdp +
                    "--proof\r\nContent-Type: application/rs-metadata+xml\r\n\r\n"
                    '<recording xmlns="urn:ietf:params:xml:ns:recording:1"/>\r\n--proof--\r\n')
            source.sendto(("INVITE sip:record@127.0.0.1:15060 SIP/2.0\r\n"
                           "Via: SIP/2.0/UDP 127.0.0.1:15061;branch=z9hG4bK-demux\r\n"
                           "From: <sip:source@localhost>;tag=source\r\nTo: <sip:record@localhost>\r\n"
                           "Call-ID: demux-proof\r\nCSeq: 1 INVITE\r\nMax-Forwards: 16\r\n"
                           "Contact: <sip:source@127.0.0.1:15061>\r\nRequire: siprec\r\n"
                           "Content-Type: multipart/mixed;boundary=proof\r\n"
                           f"Content-Length: {len(body)}\r\n\r\n{body}").encode(), ("127.0.0.1", 15060))
            identities, active_ports = set(), set()
            for index in range(0 if freeswitch else 2):
                packet, address = receive(backend, "INVITE ")
                identities.add(fields(packet, "Call-ID")[0].strip())
                assert fields(packet, "Content-Type")[0].strip() == "application/sdp"
                offered = packet.split("\r\n\r\n", 1)[1]
                ports = re.findall(r"m=audio (\d+)", offered)
                assert len(ports) == 1 and ports[0] != "0", ports
                active_ports.add(ports[0])
                label = int(re.search(r"a=label:(\d+)", offered)[1])
                assert label in (1, 2)
                answer = re.sub(r"m=audio ([1-9][0-9]*)", f"m=audio {39998 + label * 2}", offered)
                answer = answer.replace("a=sendonly", "a=recvonly")
                headers = "".join(f"{key}: {value.strip()}\r\n"
                                  for key in ("Via", "From", "Call-ID", "CSeq")
                                  for value in fields(packet, key))
                backend.sendto(("SIP/2.0 200 OK\r\n" + headers +
                                f"To: {fields(packet, 'To')[0].strip()};tag=backend-{index}\r\n"
                                "Contact: <sip:record@127.0.0.1:15062>\r\nContent-Type: application/sdp\r\n"
                                f"Content-Length: {len(answer)}\r\n\r\n{answer}").encode(), address)
            if not freeswitch:
                assert len(identities) == 2 and len(active_ports) == 2
            if not media:
                assert active_ports == {"30000", "30002"}
            answer, _ = receive(source, "SIP/2.0 200")
            assert fields(answer, "Call-ID")[0].strip() == "demux-proof"
            if media:
                media_update(source, backend, answer, sdp, fields, freeswitch)
            else:
                assert sorted(re.findall(r"m=audio (\d+)", answer)) == ["40000", "40002"]
            print("stock b2b_sdp_demux passed: multipart SIPREC split into two single-active-stream calls and aggregated answer")
    finally:
        for child in (process, proxy):
            if child is not None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    main()
