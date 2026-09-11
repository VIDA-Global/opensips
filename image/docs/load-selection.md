# Gateway Load Selection Policy

`scripts/gateway_load_selection.py` implements a bounded, single-owner placement
policy. `make -C image validate` runs its deterministic tests. It is not installed
by Packer and the production OpenSIPS routes do not call it yet.

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
- Score physical channels plus this owner's pending allocations relative to each
  configured channel ceiling. A stable hashed tie-break distributes equal scores.
  The caller supplies a physical-channel cost per operation; do not equate one
  SIPREC session with one physical FreeSWITCH channel.
- Allocation IDs are stable retries with immutable cost and a maximum 30-second
  deadline. Release leaves a terminal retry tombstone until that deadline. The
  default maximum is 4096 retained reservations/tombstones. Retrying an existing
  allocation preserves its original destination and deadline; it must reuse the
  original SIP transaction, not create a new call. Withdrawal only excludes new
  allocation IDs. Existing dialog routing remains the SIP owner's responsibility.

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

This module is not yet installed as a supervised production poller or connected to
SIP reservation routes. Dynamic secret resolution, PostgreSQL-backed reservation
authority and complete real gateway/SIP integration remain required.

### Remaining Runtime Composition

The caller must serialize access to the policy object and perform authenticated,
byte/deadline-bounded HTTPS outside SIP processing. This file does not implement
the HTTP loop, credential resolution, SIP dialogs, cleanup callbacks or
an OpenSIPS-wide shared service. It must not receive ESL credentials or Sage's
administrative inventory token.

Local pending counts are conservative hints, not atomic global reservations.
Multiple OpenSIPS instances, delayed physical observations and reservation expiry
can still oversubscribe aggregate capacity. Keep FreeSWITCH and Sage hard limits,
and qualify pending-allocation reconciliation with real SIP/media before promotion.
The [UA recovery gate](ua-feasibility.md) remains independently unresolved.
