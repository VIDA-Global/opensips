# Configuration-Only UA Renegotiation Proof

`make -C image test-ua-proof` compiles checksum-pinned OpenSIPS 3.6.8 on a
digest-pinned Ubuntu ARM64 test image. Compilation contacts Ubuntu repositories;
the runtime test has no external network, no capabilities, a read-only root and
an unprivileged user. It runs OpenSIPS and two synthetic SIP peers over loopback.
The Docker context is deny-by-default through the fixture-specific ignore file.

The fixture uses documented `b2b_entities` UA APIs plus `mi_script` to create a
source UAS and a backend UAC. After initial offer/answer it issues re-INVITEs on
both existing legs, checks that the two distinct Call-IDs are preserved, checks
changed SDP media ports, and waits for ACKs on both renegotiations.

This proves that a live owner can originate in-dialog updates using unmodified
upstream code. It is not production B2BUA configuration: peer mapping is volatile,
there is no production admission, teardown/glare policy, cluster takeover,
RTPengine, DTLS, FreeSWITCH, SIPREC metadata or media-byte verification.

## Remaining Recovery Architecture Gate

The proof must not be transplanted into the production configuration as an HA
solution. A crash/restart probe with upstream SQLite schema and synchronous
database writes restored two B2B entities but **zero UA sessions**. Before the
crash, `ua_session_list` returned both sessions. After restarting the same binary
and configuration against the same database, `b2be_list` returned two entities
while `ua_session_list` returned none. This fails the UA identity restoration gate;
it does not test cluster takeover or prove that every upstream arrangement fails.

Reproduce after building the image with `make -C image test-ua-proof`:

```sh
docker run --rm --network none --cap-drop ALL \
  --security-opt no-new-privileges --user 65532:65532 --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:ua-proof \
  python3 /tests/ua-proof.py --verify-persistence
```

This diagnostic deliberately exits nonzero when UA identity is not restored.
It kills only the fixture-created OpenSIPS process group inside its disposable
container; its temporary database is removed with the container. The ordinary
single-owner renegotiation check remains a separate passing check.

Source inspection found that UA event/ACK flags are assigned at entity creation
but are not explicitly serialized by the inspected database/cluster paths. They
govern event delivery and automatic ACK behavior. The observed identity loss is
consistent with that omission; cluster takeover still requires its own live test.

OpenSIPS 3.6.8 `rtp_relay` supports B2B engagement, but its media re-anchoring path
looks up `dialog` records and calls the dialog module's `send_indialog_request`.
This is not evidence that `rtp_relay_update` works for arbitrary `b2b_logic`
sessions. The inspected official 4.0.0 source at
`acf45c08e7a169b18f7b53f02d42c19c035c1c43` retains this distinction.

Further inspection of that immutable 4.0.0 revision's
[`bin_pack_entity` and `receive_entity_create`](https://github.com/OpenSIPS/opensips/blob/acf45c08e7a169b18f7b53f02d42c19c035c1c43/modules/b2b_entities/b2be_clustering.c)
found no UA flag serialization or restoration. Changing from SQLite persistence
to cluster replication therefore has no source-backed justification as a fix.
This is source evidence, not a completed 4.0.0 cluster test.

The inspected `b2b_logic` script/MI APIs can pass endpoint-originated requests and
bridge to new client entities, but expose no arbitrary existing-leg request
originator equivalent to the UA API. `restore_logic_info` is a C module callback
binding API, not a script facility for restoring UA flags. Sending fabricated
in-dialog requests through a separate UAC would bypass the dialog owner's CSeq,
offer/answer and transaction state and is not an accepted workaround.

No supported configuration meeting both owner replacement and autonomous
existing-leg renegotiation has been demonstrated among these candidates. Keep
this gate blocked while independent inventory and load-selection work proceeds.

Do not bypass documented API ownership or add a second ESL path to obtain a
passing recovery test. Preserve Sage's call/generation fences for any native
FreeSWITCH media changes that the eventual recovery sequence requires.

The live fixture also caught an existing image-unit startup error: OpenSIPS 3.6
rejects deprecated `-E`. The image unit now uses `-F` and configuration-owned
logging. See [operations](operations.md) and [media recovery](media-recovery.md).
