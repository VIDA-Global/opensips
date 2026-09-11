# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM ubuntu:24.04@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bison flex build-essential ca-certificates libssl-dev libpcre2-dev libjson-c-dev \
    pkg-config python3 libsqlite3-dev && rm -rf /var/lib/apt/lists/*
COPY build/sources/opensips-3.6.8.tar.gz /source.tar.gz
RUN printf '%s\n' 'b3e1ab4d82dce763bbd51c99a1733f133465fda8fe2591f86aec9c3eefababf0  /source.tar.gz' | sha256sum -c - \
    && mkdir /source && tar -xzf /source.tar.gz --strip-components=1 -C /source \
    && cd /source && make Makefile.conf \
    && make -j2 include_modules='b2b_entities mi_script cachedb_local sl tm signaling uac_auth sipmsgops db_sqlite' all \
    && mkdir -p /modules \
    && for module in b2b_entities mi_script cachedb_local sl tm signaling uac_auth sipmsgops db_sqlite; do \
        cp "modules/$module/$module.so" /modules/; done \
    && cp opensips /usr/local/bin/opensips
COPY tests/integration/ua-proof.cfg tests/integration/ua-proof.py /tests/
CMD ["python3", "/tests/ua-proof.py"]
