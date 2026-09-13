#!/bin/sh
set -eu
test "$#" -le 1 || exit 2
freeswitch=0
case "${1:-}" in
    --demux-media) script=demux-proof.py; set -- --media ;;
    --demux-freeswitch) script=demux-proof.py; freeswitch=1; set -- --freeswitch ;;
    ""|--takeover) script=proxy-reanchor-proof.py ;;
    *) exit 2 ;;
esac
name="stock-reanchor-$$"
image="vidaislive/rtpengine@sha256:4fa554a5beb540246733502ca2dc5cd7f02156a97fbf0a3957fc9c224905de3d"
cleanup() {
    status=$?
    trap - EXIT
    if [ "$status" -ne 0 ]; then
        docker exec "$name" python3 -c 'from pathlib import Path; [print(p.name, p.read_text()[-8192:]) for p in (Path("/tmp/reanchor.log"), Path("/tmp/reanchor-backup.log"), Path("/tmp/demux.log"), Path("/tmp/demux-proxy.log")) if p.exists()]' || true
    fi
    if [ "$freeswitch" -eq 1 ]; then
        docker stop --timeout 3 "$name-fs" >/dev/null 2>&1 || true
        if [ "$status" -eq 0 ]; then docker rm "$name-fs" >/dev/null; fi
    fi
    docker stop --timeout 3 "$name-a" "$name-b" "$name" >/dev/null 2>&1 || true
    if [ "$status" -eq 0 ]; then
        docker rm "$name-a" "$name-b" "$name" >/dev/null
    else
        printf 'Failed proof containers retained: %s, %s-a, %s-b\n' "$name" "$name" "$name" >&2
    fi
    exit "$status"
}
trap cleanup EXIT
docker run -d --name "$name" --network none --cap-drop ALL \
    --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --user 65532:65532 sage-opensips:stock-proxy-proof sleep 180 >/dev/null
docker run -d --name "$name-a" --network "container:$name" --cap-drop ALL \
    --security-opt no-new-privileges --entrypoint rtpengine "$image" \
    --foreground --log-stderr --table=-1 \
    --interface=127.0.0.1 --listen-ng=127.0.0.1:2223 --port-min=10000 --port-max=10999 \
    --delete-delay=0 >/dev/null
docker run -d --name "$name-b" --network "container:$name" --cap-drop ALL \
    --security-opt no-new-privileges --entrypoint rtpengine "$image" \
    --foreground --log-stderr --table=-1 \
    --interface=127.0.0.1 --listen-ng=127.0.0.1:2224 --port-min=12000 --port-max=12999 \
    --delete-delay=0 >/dev/null
if [ "$freeswitch" -eq 1 ]; then
    root=$(CDPATH='' cd -- "$(dirname "$0")" && pwd)
    docker run -d --name "$name-fs" --network "container:$name" --cap-drop ALL \
        --security-opt no-new-privileges --user 10001:10001 \
        --tmpfs /tmp:rw,nosuid,nodev,size=64m \
        --mount "type=bind,source=$root/demux-freeswitch.xml,target=/proof/freeswitch.xml,readonly" \
        --entrypoint freeswitch \
        vidaislive/freeswitch@sha256:45c2e867e49cb6620fa8e5dd652900252ea92be14964048ffc68a25c3375aa2a \
        -nf -nonat -conf /proof -log /tmp -db /tmp -run /tmp -temp /tmp >/dev/null
    docker exec "$name" python3 -c '
import socket, time
message = b"OPTIONS sip:record@127.0.0.1:15062 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:15063;branch=z9hG4bK-fs-ready\r\nFrom: <sip:probe@localhost>;tag=probe\r\nTo: <sip:record@127.0.0.1:15062>\r\nCall-ID: fs-ready\r\nCSeq: 1 OPTIONS\r\nMax-Forwards: 16\r\nContent-Length: 0\r\n\r\n"
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
    peer.bind(("127.0.0.1", 15063))
    peer.settimeout(0.2)
    deadline = time.monotonic() + 15
    status = b"no response"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        probe = message.replace(b"z9hG4bK-fs-ready", f"z9hG4bK-fs-ready-{attempt}".encode())
        probe = probe.replace(b"Call-ID: fs-ready", f"Call-ID: fs-ready-{attempt}".encode())
        peer.sendto(probe, ("127.0.0.1", 15062))
        try:
            status = peer.recv(4096).split(b"\r\n", 1)[0]
            if status.startswith(b"SIP/2.0 200"):
                break
        except TimeoutError:
            pass
    else:
        raise RuntimeError("FreeSWITCH SIP endpoint did not become ready: " + status.decode())
'
fi
docker exec "$name" python3 -c '
import socket, time
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
    peer.settimeout(0.2)
    for port in (2223, 2224):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            peer.sendto(b"ready d7:command4:pinge", ("127.0.0.1", port))
            try:
                if b"4:pong" in peer.recv(1024):
                    break
            except TimeoutError:
                pass
        else:
            raise RuntimeError("RTPengine did not become ready")
'
docker exec "$name" python3 "/tests/$script" "$@"
if [ "$freeswitch" -eq 1 ]; then
    docker exec "$name-fs" python3 -c '
import audioop, struct, time, wave
from pathlib import Path
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    labels = set()
    evidence = []
    paths = list(Path("/tmp").glob("demux-*.wav"))
    for path in paths:
        with wave.open(str(path), "rb") as audio:
            assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
            frames = audio.readframes(audio.getnframes())
            samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
        found = set()
        for index in (0, 1):
            before = struct.unpack("<h", audioop.alaw2lin(bytes([41 + index]), 2))[0]
            after = struct.unpack("<h", audioop.alaw2lin(bytes([73 + index]), 2))[0]
            # FreeSWITCH/libsndfile negative PCM rounding may differ by one LSB.
            if (sum(abs(value - before) <= 1 for value in samples) >= 160
                    and sum(abs(value - after) <= 1 for value in samples) >= 160):
                found.add(index)
        evidence.append((len(samples), min(samples, default=0), max(samples, default=0), sorted(found)))
        if len(found) == 1:
            labels.update(found)
    if len(paths) == 2 and labels == {0, 1}:
        break
    time.sleep(0.1)
else:
    raise AssertionError(("both participant recordings must contain media from before and after replacement", evidence))
print("real FreeSWITCH recordings passed: both separated participants before and after RTPengine replacement")
'
fi
