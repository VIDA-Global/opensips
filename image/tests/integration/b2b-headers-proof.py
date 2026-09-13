"""Real async-route B2BUA header and multipart regression, using loopback only."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import subprocess
import threading
import time


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args):
        pass


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
loadmodule "uac_auth.so"
loadmodule "b2b_entities.so"
loadmodule "b2b_logic.so"
loadmodule "rest_client.so"
modparam("b2b_logic", "custom_headers_regexp", "/^x-sage-/i")
modparam("b2b_logic", "server_address", "sip:edge@127.0.0.1:15060")
modparam("b2b_logic", "script_reply_route", "REPLY")
route {
    if (is_method("OPTIONS")) { sl_send_reply(200, "OK"); exit; }
    list_hdr_remove_option("Require", "siprec");
    remove_hf_glob("[xX]-[sS][aA][gG][eE]-*");
    codec_delete("PCMA");
    async(rest_get("http://127.0.0.1:18095/", $avp(reply)), SETUP);
    exit;
}
route[SETUP] {
    $avp(names) = "X-SAGE-Source-IP";
    $avp(values) = $si;
    b2b_server_new("source");
    b2b_client_new("backend", "sip:record@127.0.0.1:15062", , , , $avp(names), $avp(values));
    b2b_init_request("header-proof");
    exit;
}
route[REPLY] {
    codec_delete("PCMA");
    b2b_handle_reply();
    exit;
}
'''


def main():
    server = HTTPServer(("127.0.0.1", 18095), Handler)
    server.timeout = 2
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = Path("/tmp/b2b-headers-proof.cfg")
    path.write_text(CONFIG)
    process = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as backend, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as source:
            backend.bind(("127.0.0.1", 15062))
            backend.settimeout(5)
            source.bind(("127.0.0.1", 15061))
            source.settimeout(0.1)
            with Path("/tmp/b2b-headers-proof.log").open("wb") as log:
                process = subprocess.Popen(["opensips", "-F", "-f", str(path)], stdout=log, stderr=log)
            body = ("--proof\r\nContent-Type: application/sdp\r\n\r\n"
                    "v=0\r\no=proof 1 1 IN IP4 127.0.0.1\r\ns=proof\r\nc=IN IP4 127.0.0.1\r\n"
                    "t=0 0\r\nm=audio 30000 RTP/AVP 0 8\r\na=rtpmap:8 PCMA/8000\r\na=sendonly\r\n"
                    "--proof\r\nContent-Type: application/rs-metadata+xml\r\n\r\n"
                    '<recording xmlns="urn:ietf:params:xml:ns:recording:1"/>\r\n--proof--\r\n')
            for _ in range(50):
                if process.poll() is not None:
                    raise AssertionError("OpenSIPS startup failed; inspect private proof log")
                source.sendto(b"OPTIONS sip:edge@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:15061;branch=z9hG4bK-ready\r\nFrom: <sip:source@localhost>;tag=ready\r\nTo: <sip:edge@localhost>\r\nCall-ID: ready\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n", ("127.0.0.1", 15060))
                try:
                    if source.recv(4096).startswith(b"SIP/2.0 200"):
                        break
                except TimeoutError:
                    time.sleep(0.02)
            else:
                raise AssertionError("OpenSIPS readiness timeout")
            for index, require in enumerate(("siprec", "siprec, timer", "timer, siprec")):
                message = (f"INVITE sip:record@127.0.0.1:15060 SIP/2.0\r\n"
                           f"Via: SIP/2.0/UDP 127.0.0.1:15061;branch=z9hG4bK-proof-{index}\r\n"
                           f"From: <sip:source@localhost>;tag=proof-{index}\r\nTo: <sip:record@localhost>\r\n"
                           f"Call-ID: proof-{index}\r\nCSeq: 1 INVITE\r\nMax-Forwards: 10\r\n"
                           f"Contact: <sip:source@127.0.0.1:15061>\r\nRequire: {require}\r\n"
                           "X-SAGE-Source-IP: forged\r\nx-sage-edge-call-id: forged\r\n"
                           f"Content-Type: multipart/mixed; boundary=proof\r\nContent-Length: {len(body)}\r\n\r\n{body}")
                source.sendto(message.encode(), ("127.0.0.1", 15060))
                packet, address = backend.recvfrom(65535)
                headers, _, forwarded_body = packet.decode().partition("\r\n\r\n")
                fields = [line.lower() for line in headers.split("\r\n")[1:]]
                assert not any("siprec" in line for line in fields if line.startswith("require:")), "removed Require option survived async B2BUA forwarding"
                assert not any("forged" in line for line in fields), "caller trusted-header spoof survived"
                assert fields.count("x-sage-source-ip: 127.0.0.1") == 1
                expected_body = body.replace("RTP/AVP 0 8", "RTP/AVP 0").replace("a=rtpmap:8 PCMA/8000\r\n", "")
                assert forwarded_body == expected_body, "SDP edit or multipart bytes lost"
                assert "content-type: multipart/mixed; boundary=proof" in fields
                if index:
                    assert any(line.startswith("require:") and "timer" in line for line in fields)
                values = dict(line.split(":", 1) for line in headers.split("\r\n")[1:])
                response_headers = "".join(f"{name}:{values[name]}\r\n" for name in ("Via", "From", "Call-ID", "CSeq"))
                backend.sendto(("SIP/2.0 200 OK\r\n" + response_headers +
                                f"To:{values['To']};tag=backend-{index}\r\n"
                                "Contact: <sip:record@127.0.0.1:15062>\r\n"
                                f"Content-Type: multipart/mixed; boundary=proof\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode(), address)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        reply = source.recv(65535).decode()
                    except TimeoutError:
                        continue
                    if reply.startswith("SIP/2.0 200") and f"Call-ID: proof-{index}\r\n" in reply:
                        assert reply.partition("\r\n\r\n")[2] == expected_body, "reply route SDP edit was lost"
                        break
                else:
                    raise AssertionError("B2BUA answer timeout")
        print("async B2BUA header removal, trusted context and multipart preservation passed")
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
