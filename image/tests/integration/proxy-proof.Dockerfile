# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM ubuntu:24.04@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bison flex build-essential ca-certificates libssl-dev libpcre2-dev libjson-c-dev libcurl4-openssl-dev \
    pkg-config python3 python3-asyncpg python3-boto3 libpq-dev postgresql-client && rm -rf /var/lib/apt/lists/*
COPY build/sources/opensips-3.6.8.tar.gz /source.tar.gz
# Compile only the verified upstream archive. No local C sources or patch inputs.
RUN printf '%s\n' 'b3e1ab4d82dce763bbd51c99a1733f133465fda8fe2591f86aec9c3eefababf0  /source.tar.gz' | sha256sum -c - \
    && mkdir /source && tar -xzf /source.tar.gz --strip-components=1 -C /source \
    && cd /source && make Makefile.conf \
    && make -j2 CC_EXTRA_OPTS=-Werror include_modules='sl tm signaling sipmsgops db_postgres rest_client json cfgutils clusterer dialog maxfwd proto_bin rtpengine rtp_relay rr mi_fifo' all \
    && mkdir /modules \
    && for module in sl tm signaling sipmsgops db_postgres rest_client json cfgutils clusterer dialog maxfwd proto_bin rtpengine rtp_relay rr mi_fifo mi_datagram tracer; do \
        cp "modules/$module/$module.so" /modules/; done \
    && cp opensips /usr/local/bin/opensips
COPY scripts/gateway_load_polling.py scripts/gateway_load_selection.py scripts/placement_store.py scripts/placement_service.py scripts/placement_observer.py scripts/placement_secret.py /tests/
COPY assets/placement-schema.sql assets/placement_config.py assets/opensips-runtime-config.py assets/opensips.cfg.template /tests/
COPY tests/integration/proxy-proof.cfg.template tests/integration/native-edge-stack.py tests/integration/native-edge-health.py tests/integration/gateway-load-proxy.py /tests/
COPY tests/integration/proxy-reanchor-proof.py /tests/
COPY tests/integration/demux-proof.py /tests/
COPY tests/integration/node-demux.py tests/integration/node-demux.cfg.template /tests/
CMD ["python3", "/tests/native-edge-stack.py"]
