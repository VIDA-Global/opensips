# PostgreSQL Placement Service

`opensips-placement.service` owns bounded observation and reservation operations
for the native OpenSIPS policy. Packer installs its Python modules and the schema
bootstrap SQL. PostgreSQL owns the shared reservation ledger and observer epochs;
Sage retains tenant/admission authority and the fixed gateway remains the sole ESL
owner. The service binds only `127.0.0.1:8095` and requires its own bearer token.

## Inputs and Decisions

- Obtain complete, authenticated Sage placement pages using the separate reader
  credential. Targets carry node ID, gateway and FreeSWITCH generations, relative
  validity, and an independently configured physical channel ceiling. Capacity
  values require measurements; the policy supplies no production capacity default.
- Track HTTP start and completion with one monotonic clock. The complete refresh
  consumes each eligibility lease; partial/out-of-order refreshes must not replace
  a complete current inventory. The policy bounds an inventory to 256 distinct nodes.
- Poll approved gateway `/v1/node-load` endpoints directly. The policy accepts only
  exact schema `1.0.0`, ready/valid replies, matching generation coordinates,
  nonnegative integer physical counts and fresh samples. It rejects duplicate JSON
  keys, oversized bodies, malformed values, stale sequences and late HTTP replies.
- Both remote sample age and elapsed request time consume the three-second load
  window. Requests taking two seconds or more are rejected. Re-reading the same
  observation cannot renew its first accepted expiry.
- Score physical channels plus the namespace's PostgreSQL pending allocations relative to each
  configured channel ceiling. A stable hashed tie-break distributes equal scores.
  The caller supplies a physical-channel cost per operation; do not equate one
  SIPREC session with one physical FreeSWITCH channel.
- Allocation IDs and request fingerprints are SHA-256 digests. PostgreSQL serializes
  allocation across helpers, with at most 4096 active pending/confirmed reservations
  per configured namespace and a maximum 30-second lifetime. Release-before-allocate
  records a tombstone; neither release nor expiration permits reuse of the identity.
  Tombstones are retained durably, without an automatic deletion/archival policy.
- A replay returns the original destination with `fresh: false`; the native initial
  SIP route rejects that result rather than creating another dialog after an uncertain
  first dispatch. Existing SIP transactions handle ordinary retransmissions.
- Confirm notifications do not immediately remove pending cost. The cost remains
  until a gateway observation's conservative sample-time lower bound is later than
  confirmation. Duplicate telemetry cannot renew its original expiry, including after
  an unavailable response. All eligibility tests run after the SQL namespace lock.
  PostgreSQL retains retired telemetry-generation history, including while nodes
  are withdrawn, so a delayed response cannot switch the observer back to an old
  gateway sampling process.

## Runtime and Database Ownership

Apply `assets/placement-schema.sql` with the placement database owner before boot.
The runtime role needs schema USAGE and table SELECT/INSERT/UPDATE/DELETE, not DDL
or Sage tenant-table access. `placement_store.py` uses one four-connection asyncpg
pool. Network/secret operations occur after SQL scopes have closed. Observer epochs
last ten seconds and renew every two seconds; stale observers cannot publish.
Eligibility refresh is bounded to five seconds and load polling uses eight concurrent
requests, with 0.8–1.0-second inter-round jitter. Failed observations withdraw load;
failed inventory refreshes allow the previous leases to expire.

The local API exposes `POST /v1/reservations`, and `POST
/v1/reservations/{allocation_id}/confirm` or `/release`. Reservation bodies contain
only `allocation_id`, `request_sha256`, `product`, `channels`, and `ttl_ms`; mutation
bodies are `{}`. Responses contain the selected SIP destination and immutable node
generation coordinates, never credentials. The service limits active requests to
32, headers to 8192 bytes, bodies to 2048 bytes, and complete requests to four seconds.
Pool and SQL operations have separate bounds. Excess accepted connections close
without queueing additional application tasks.

The native template calls the local API using `async(rest_post(...))` and sends
confirmation/release using bounded `launch(rest_post(...))` notifications. Its global
32-request guard is below rest_client's per-worker limit of 64, avoiding that module's
blocking fallback. Lost notifications conservatively retain pending cost until expiry.
The service does not control existing dialogs, implement tenant policy or decide
recording completion. Configured costs (one for Voice, three for SIPREC) and physical
ceilings still require qualification with the intended media topology.
The current native backend route uses private UDP; the observer rejects TCP/TLS
backend targets rather than silently changing their requested transport. This is
separate from carrier-facing listener transport.

## Integration Gates

### Direct HTTPS and Placement Reader

`scripts/gateway_load_polling.py` implements the direct HTTPS transport and the
schema `1.1.0` placement-page reader. Each request resolves and checks every returned
address against explicit role networks/ports, pins the connection, verifies the
certificate and logical hostname, and sends the credential only in its header.
Use separate client instances/scopes for Sage inventory and gateway load. There are
no proxies, redirects, cookies or connection reuse. The fixed JSON GET contract
requires Content-Length and rejects chunked/compressed/ambiguous responses.

Requests have a whole-operation deadline and bounded headers/body. Capacity is
immediate rather than a waiter queue. Timed-out system DNS retains its capacity slot
until the underlying lookup completes, preventing successive timeouts from creating
unbounded resolver work. TLS transport is aborted on incomplete closure/cancellation.
The inventory reader accepts at most four pages and 256 node records, advances
empty-page cursors, rejects duplicate/out-of-order records and foreign load-secret
namespaces, and returns only a complete refresh. Its monotonic refresh-start time
must be used when applying the relative eligibility leases.

`make -C image test-load-https-proof` exercises real TLS on a disposable internal
Docker network with synthetic credentials and a generated test-only certificate.
It verifies CA trust, private-IP pinning, no redirects, body limits and stalled-peer
timeout closure. It publishes no ports and removes its container/network on exit.
The build requires registry access; the probe does not contact AWS or a live gateway.

The observer resolves immutable load-secret versions in an independently bounded
SDK child using only IMDSv2 EC2-role credentials. No ambient profile or static-key
fallback is used. Versioned tokens are cached only while referenced by current
inventory. The schema-v2 runtime bundle supplies an explicitly scoped load-secret
namespace and separate Sage-reader/local-service tokens. PostgreSQL TLS always
verifies the certificate and hostname; HTTPS and database CA paths are separate.

### Remaining Runtime Composition

`make -C image test-placement-postgres-proof` runs independent PostgreSQL sessions,
the observer with injected HTTP/secret peers, a real local HTTP API, and real
OpenSIPS async selection/confirmation using the same route block as the native
template. It verifies concurrency, replay/conflict/tombstones, observer replacement,
sample non-renewal, confirmation timing and lock-wait freshness. SDK identity/version
checks are tested with synthetic responses without contacting AWS.

The old in-memory `Selector` is a reference policy used only by deterministic tests;
the installed service uses `PlacementStore` for decisions and imports only its shared
load decoder. Atomic SQL pending reservations do not eliminate races with unmanaged
physical calls or delayed external effects. Keep FreeSWITCH and Sage hard limits.
Full native SIPREC/multipart/DTLS/media integration, production capacity, instance
boot and multi-owner established-dialog recovery remain separate qualification
gates. The local SIP proof uses synthetic peers and does not run RTPengine or
FreeSWITCH. See the [UA recovery boundary](ua-feasibility.md).
