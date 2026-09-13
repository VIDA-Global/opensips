"""Stock rtp_relay re-INVITEs and real bidirectional RTP, on isolated loopback."""

import json
import os
from pathlib import Path
import re
import select
import socket
import struct
import subprocess
import sys
import signal
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
loadmodule "rr.so"
loadmodule "dialog.so"
loadmodule "rtpengine.so"
loadmodule "rtp_relay.so"
loadmodule "mi_datagram.so"
modparam("mi_datagram", "socket_name", "/tmp/reanchor-mi.sock")
modparam("rtpengine", "rtpengine_sock", "1 == udp:127.0.0.1:2223")
modparam("rtpengine", "rtpengine_sock", "2 == udp:127.0.0.1:2224")
route {
    if (is_method("OPTIONS")) { sl_send_reply(200, "OK"); exit; }
    if (has_totag()) {
        if (!loose_route()) { sl_send_reply(404, "No Route"); exit; }
        t_relay(); exit;
    }
    if (!is_method("INVITE")) { sl_send_reply(405, "No Method"); exit; }
    record_route();
    create_dialog("B");
    rtp_relay_engage("rtpengine", 1);
    $du = "sip:backend@127.0.0.1:15062";
    t_relay();
}
'''


def fields(packet, name):
    return re.findall(r"^" + re.escape(name) + r":\s*(.*)$",
                      packet.split("\r\n\r\n", 1)[0], re.M | re.I)


def sdp(port):
    return ("v=0\r\no=proof 1 1 IN IP4 127.0.0.1\r\ns=proof\r\n"
            f"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {port} RTP/AVP 8\r\n"
            "a=rtpmap:8 PCMA/8000\r\na=sendrecv\r\n")


def media_target(packet):
    body = packet.split("\r\n\r\n", 1)[1]
    return (re.search(r"c=IN IP4 ([0-9.]+)", body)[1],
            int(re.search(r"m=audio (\d+)", body)[1]))


def reply(peer, packet, address, port):
    headers = "".join(f"{key}: {value.strip()}\r\n"
                      for key in ("Via", "From", "To", "Call-ID", "CSeq", "Record-Route")
                      for value in fields(packet, key))
    if ";tag=" not in fields(packet, "To")[0]:
        headers = headers.replace(fields(packet, "To")[0].strip() + "\r\n",
                                  fields(packet, "To")[0].strip() + ";tag=backend\r\n")
    body = sdp(port)
    peer.sendto(("SIP/2.0 200 OK\r\n" + headers +
                 f"Contact: <sip:peer@127.0.0.1:{peer.getsockname()[1]}>\r\n"
                 f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode(), address)


def receive(peer, prefix):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            packet, address = peer.recvfrom(65535)
        except TimeoutError:
            continue
        text = packet.decode()
        if text.startswith(prefix):
            return text, address
    raise AssertionError(f"peer did not receive {prefix}")


def audio_proof(peers, targets, marker):
    # Real PCMA RTP passes through the selected relay in both directions.
    received = [False, False]
    for sequence in range(60):
        for index, peer in enumerate(peers):
            packet = struct.pack("!BBHII", 0x80, 8, sequence, sequence * 160, 100 + index)
            packet += bytes([marker + index]) * 160
            peer.sendto(packet, targets[index])
        for index, peer in enumerate(peers):
            try:
                packet = peer.recv(4096)
                received[index] |= packet[12:] == bytes([marker + 1 - index]) * 160
            except TimeoutError:
                pass
        if all(received):
            return
    raise AssertionError("bidirectional RTP payload did not traverse the relay")


def rpc(path, method, params=None):
    address = "/tmp/control.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as peer:
            peer.bind(address)
            peer.settimeout(2)
            request = {"jsonrpc": "2.0", "id": 2, "method": method}
            if params is not None:
                request["params"] = params
            peer.sendto(json.dumps(request).encode(), path)
            result = json.loads(peer.recv(65535))
            assert "error" not in result, result
            return result
    finally:
        Path(address).unlink(missing_ok=True)


def cluster_config(node):
    other = 3 - node
    settings = f'''
socket=bin:127.0.0.1:{15565 + node}
alias=udp:127.0.0.1:15060
loadmodule "proto_bin.so"
loadmodule "clusterer.so"
modparam("clusterer", "db_mode", 0)
modparam("clusterer", "my_node_id", {node})
modparam("clusterer", "my_node_info", "cluster_id=10,url=bin:127.0.0.1:{15565 + node},flags=seed")
modparam("clusterer", "neighbor_node_info", "cluster_id=10,node_id={other},url=bin:127.0.0.1:{15565 + other}")
modparam("clusterer", "sharing_tag", "owner/10={'active' if node == 1 else 'backup'}")
modparam("dialog", "dialog_replication_cluster", 10)
'''
    return CONFIG.replace("socket=udp:127.0.0.1:15060", f"socket=udp:127.0.0.1:{15050 + node * 10} tag sip")\
        .replace("/tmp/reanchor-mi.sock", f"/tmp/reanchor-mi-{node}.sock")\
        .replace("route {", settings + "\nroute {", 1)\
        .replace('create_dialog("B");', 'create_dialog("B"); set_dlg_sharing_tag("owner");')


def main():
    takeover = sys.argv[1:] == ["--takeover"]
    assert not sys.argv[1:] or takeover, "unsupported proof arguments"
    config = Path("/tmp/reanchor.cfg")
    config.write_text(cluster_config(1) if takeover else CONFIG)
    peers = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(4)]
    process = None
    backup = None
    try:
        for peer, port in zip(peers, (15061, 15062, 30000, 30002)):
            peer.bind(("127.0.0.1", port))
            peer.settimeout(0.1)
        source, backend, source_audio, backend_audio = peers
        with Path("/tmp/reanchor.log").open("wb") as log:
            process = subprocess.Popen(["opensips", "-F", "-f", str(config)], stdout=log, stderr=log,
                                       start_new_session=True)
        mi_path = "/tmp/reanchor-mi-1.sock" if takeover else "/tmp/reanchor-mi.sock"
        if takeover:
            backup_config = Path("/tmp/reanchor-backup.cfg")
            backup_config.write_text(cluster_config(2))
            with Path("/tmp/reanchor-backup.log").open("wb") as log:
                backup = subprocess.Popen(["opensips", "-F", "-f", str(backup_config)], stdout=log,
                                          stderr=log, start_new_session=True)
        deadline = time.monotonic() + 5
        while not Path(mi_path).exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("stock proxy startup failed; retained reanchor.log")
            time.sleep(0.05)
        if takeover:
            deadline = time.monotonic() + 15
            paths = ("/tmp/reanchor-mi-1.sock", "/tmp/reanchor-mi-2.sock")
            while time.monotonic() < deadline:
                if all(Path(path).exists() for path in paths) and all(
                    '"link_state": "Up"' in json.dumps(rpc(path, "clusterer_list"))
                    and "not synced" not in json.dumps(rpc(path, "clusterer_list_cap"))
                    for path in paths
                ):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("replication peers did not become ready before admission")
        body = sdp(30000)
        source.sendto(("INVITE sip:backend@127.0.0.1:15060 SIP/2.0\r\n"
                       "Via: SIP/2.0/UDP 127.0.0.1:15061;branch=z9hG4bK-initial\r\n"
                       "From: <sip:source@localhost>;tag=source\r\nTo: <sip:backend@localhost>\r\n"
                       "Call-ID: stock-reanchor\r\nCSeq: 1 INVITE\r\nMax-Forwards: 16\r\n"
                       "Contact: <sip:source@127.0.0.1:15061>\r\n"
                       f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode(),
                      ("127.0.0.1", 15060))
        offered, address = receive(backend, "INVITE ")
        reply(backend, offered, address, 30002)
        answered, _ = receive(source, "SIP/2.0 200")
        routes = "".join("Route: " + value.strip() + "\r\n" for value in fields(answered, "Record-Route"))
        source.sendto(("ACK sip:peer@127.0.0.1:15062 SIP/2.0\r\n"
                       "Via: SIP/2.0/UDP 127.0.0.1:15061;branch=z9hG4bK-ack\r\n" + routes +
                       "From: <sip:source@localhost>;tag=source\r\nTo: <sip:backend@localhost>;tag=backend\r\n"
                       "Call-ID: stock-reanchor\r\nCSeq: 1 ACK\r\nMax-Forwards: 16\r\nContent-Length: 0\r\n\r\n").encode(),
                      ("127.0.0.1", 15060))
        receive(backend, "ACK ")
        audio_proof((source_audio, backend_audio), (media_target(answered), media_target(offered)), 41)
        if takeover:
            mi_path = "/tmp/reanchor-mi-2.sock"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if Path(mi_path).exists() and "stock-reanchor" in json.dumps(rpc(mi_path, "dlg_list")):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("backup did not reconstruct the replicated dialog")
            # Explicit process-group death is the fence in this mechanism proof.
            # It does not simulate automatic partition-safe promotion or AWS steering.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            rpc(mi_path, "clusterer_shtag_set_active", {"tag": "owner/10"})
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as mi:
            mi.bind("/tmp/reanchor-client.sock")
            mi.settimeout(5)
            mi.sendto(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "rtp_relay_update_callid",
                                 "params": {"callid": "stock-reanchor", "engine": "rtpengine", "set": 2,
                                            "node": "udp:127.0.0.1:2224"}}).encode(), mi_path)
            targets = {}
            deadline = time.monotonic() + 10
            while len(targets) < 2 and time.monotonic() < deadline:
                ready, _, _ = select.select([source, backend], [], [], 0.2)
                for peer in ready:
                    packet, address = peer.recvfrom(65535)
                    text = packet.decode()
                    if text.startswith("INVITE "):
                        index = 0 if peer is source else 1
                        targets[index] = media_target(text)
                        reply(peer, text, address, 30000 + index * 2)
            assert len(targets) == 2, "stock module did not renegotiate both peers"
            result = json.loads(mi.recv(65535))
            assert "error" not in result, result
            assert all(12000 <= target[1] < 13000 for target in targets.values()), targets
            audio_proof((source_audio, backend_audio), (targets[0], targets[1]), 73)
        print("stock rtp_relay re-anchoring passed: two re-INVITEs and bidirectional RTP on replacement"
              + (" after fenced proxy loss and backup promotion" if takeover else ""))
    finally:
        for peer in peers:
            peer.close()
        for child in (process, backup):
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)


if __name__ == "__main__":
    main()
