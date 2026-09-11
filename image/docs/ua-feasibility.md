# PostgreSQL UA Recovery Proof

`make -C image test-ua-proof` compiles checksum-pinned OpenSIPS 3.6.8 on a
digest-pinned Ubuntu ARM64 test image with the explicitly approved narrow UA-state
source correction. Compilation contacts Ubuntu repositories;
the runtime test has no external network, no capabilities, a read-only root and
an unprivileged user. It runs OpenSIPS and two synthetic SIP peers over loopback.
The Docker context is deny-by-default through the fixture-specific ignore file.

The fixture uses documented `b2b_entities` UA APIs plus `mi_script` to create a
source UAS and a backend UAC. After initial offer/answer it issues re-INVITEs on
both existing legs, checks that the two distinct Call-IDs are preserved, checks
changed SDP media ports, and waits for ACKs on both renegotiations.

The original unmodified-upstream run proved that a live owner can originate
in-dialog updates. The current fixture also tests the source correction below.
It is not production B2BUA configuration: peer mapping is volatile,
there is no production admission, teardown/glare policy, cluster takeover,
RTPengine, DTLS, FreeSWITCH, SIPREC metadata or media-byte verification.

## Remaining Recovery Architecture Gate

PostgreSQL is the authoritative durable state store for the maintained solution.
The SQLite diagnostic below is an isolated backend comparison, not a production
persistence or HA design. OpenSIPS must reconstruct its session behavior from
PostgreSQL; local memory or replication cannot become a competing durable authority.

### PostgreSQL Crash and In-Dialog Control Probe

Run `make -C image test-ua-postgres-proof` to build the checksum-pinned upstream
3.6.8-based ARM64 fixture, first verify live-owner renegotiation, then create an isolated
PostgreSQL 18 database using upstream schema and synchronous `db_mode=1` writes.
The wrapper creates a private internal Docker network, publishes no ports, uses
synthetic database credentials, and removes its two containers, temporary database
and network on exit. Build/pull needs registry/Ubuntu access; the probe needs no
AWS access or existing database. PostgreSQL data resides in a bounded tmpfs.

The driver confirms two committed SIP-leg records, kills only the OpenSIPS process
group, restarts against the same PostgreSQL server, and requests re-INVITEs on both
restored legs. It checks preserved Call-IDs, advancing CSeq, changed SDP, peer replies
and actual ACKs. The driver retains the leg keys solely to isolate entity restoration;
this does not prove durable production peer pairing or controller recovery.

The unmodified PostgreSQL probe on 2026-09-11 restored two B2B entities but zero UA
sessions. Both update APIs returned success and both peers received re-INVITEs, but
neither received an ACK after replying. That unmodified run exited nonzero:
`restored legs did not complete acknowledged re-INVITEs`. This is runtime evidence
of failed restored control, rather than an inference from an empty session list.
It does not establish a PostgreSQL failure, cluster takeover or media continuity.

### Approved UA-State Correction

`modules/b2b_entities/ua_storage.c` uses the module's existing storage callbacks to
persist versioned UA flags and an absolute session expiry in the existing `storage`
column. It reconstructs the UA timer after PostgreSQL load and does not renew an
existing timer on replay. Module initialization rejects missing or unsupported
recovery data rather than treating old incomplete records as restored sessions.
The startup orphan check now retains valid UA entities without a `b2b_logic` callback.

The corrected PostgreSQL crash test restored both UA sessions, preserved both
Call-IDs and advancing CSeq, completed re-INVITEs/ACKs on both legs, and delivered
both reply events to the script. Negative cases exercise absent, truncated and
unsupported state; expired sessions terminate both legs without a new lifetime.
This closes the observed single-process PostgreSQL restoration defect. It does
not prove cross-instance fencing, replicated takeover, durable leg pairing,
interrupted offer/answer recovery, RTPengine renegotiation or real media continuity.

Packer and the local proof use the same baseline-checking source installer. Only
four explicitly named source files are overlaid; the three replaced upstream files
must match their reviewed checksums. Packer records all four resulting digests in
its input and installed source manifest, plus a digest of that map in the build
manifest. No unreviewed upstream version may silently receive these replacements.
The existing table schema is unchanged, but the UA storage payload is version 1.
Do not mix corrected and uncorrected UA owners or assume old rows are recoverable;
drain before replacing an uncorrected owner. No live AMI rollout is qualified.

### Earlier SQLite Comparison

The proof must not be transplanted into the production configuration as an HA
solution. A crash/restart probe with upstream SQLite schema and synchronous
database writes restored two B2B entities but **zero UA sessions**. Before the
crash, `ua_session_list` returned both sessions. After restarting the same binary
and configuration against the same database, `b2be_list` returned two entities
while `ua_session_list` returned none. This fails the UA identity restoration gate;
it does not test cluster takeover or prove that every upstream arrangement fails.

The original diagnostic used `--verify-persistence` on the unmodified image. The
current image includes the approved correction; this command now checks the
SQLite comparison path rather than reproducing the original failure:

```sh
docker run --rm --network none --cap-drop ALL \
  --security-opt no-new-privileges --user 65532:65532 --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m sage-opensips:ua-proof \
  python3 /tests/ua-proof.py --verify-persistence
```

This diagnostic exits nonzero if UA identity or in-dialog behavior is not restored.
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

The source-unmodified configurations above did not meet the recovery requirement.
The approved correction now provides local PostgreSQL restart evidence; the wider
multi-owner/media recovery gate remains open while integration proceeds.

Do not bypass documented API ownership or add a second ESL path to obtain a
passing recovery test. Preserve Sage's call/generation fences for any native
FreeSWITCH media changes that the eventual recovery sequence requires.

The live fixture also caught an existing image-unit startup error: OpenSIPS 3.6
rejects deprecated `-E`. The image unit now uses `-F` and configuration-owned
logging. See [operations](operations.md) and [media recovery](media-recovery.md).
