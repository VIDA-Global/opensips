#!/usr/bin/env bash
# Runs only on the disposable Packer build instance; no runtime secrets are inputs.
set -Eeuo pipefail
export LC_ALL=C
umask 022
root=/opt/opensips-image-build
source_directory=/usr/local/src/opensips
module_directory=/usr/lib/aarch64-linux-gnu/opensips/modules
build_packages=(bison build-essential flex libjson-c-dev libncurses-dev libpcre2-dev
    libpq-dev libssl-dev libxml2-dev pkg-config)
runtime_packages=(ca-certificates libjson-c5 libpcre2-8-0 libpq5 libssl3t64 libxml2
    openssl python3 python3-boto3 tini file)
test "$EUID" -eq 0
test "$#" -eq 1
exec 9>/run/opensips-image-build.lock
flock -n 9
trap 'printf "OpenSIPS image phase failed at line %s\n" "$LINENO" >&2' ERR

case "$1" in
    preflight)
        test "$(uname -m)" = aarch64
        test "$(dpkg --print-architecture)" = arm64
        # shellcheck disable=SC1091
        . /etc/os-release
        test "$ID" = ubuntu && test "$VERSION_ID" = 24.04
        python3 "$root/provision/inputs.py" check
        ;;
    dependencies)
        timeout 300 cloud-init status --wait
        timeout 300 apt-get -o Acquire::Retries=3 update
        DEBIAN_FRONTEND=noninteractive timeout 900 apt-get install -y --no-install-recommends \
            "${build_packages[@]}" "${runtime_packages[@]}"
        apt-mark manual "${runtime_packages[@]}"
        ;;
    build)
        python3 "$root/provision/inputs.py" check
        rm -rf -- "$source_directory"
        install -d -m 0755 "$source_directory"
        tar -xzf "$root/source.tar.gz" --strip-components=1 -C "$source_directory"
        selected=$(python3 "$root/provision/inputs.py" modules)
        printf '%s\n' "$selected" | sort > "$root/selected-modules"
        for module in $selected; do test -d "$source_directory/modules/$module"; done
        find "$source_directory/modules" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
            | sort > "$root/all-modules"
        excluded=$(comm -23 "$root/all-modules" "$root/selected-modules" | tr '\n' ' ')
        make -C "$source_directory" Makefile.conf
        timeout 3600 make -C "$source_directory" -j"$(nproc)" \
            CC_EXTRA_OPTS='-Werror -fstack-protector-strong' exclude_modules="$excluded" all
        timeout 600 make -C "$source_directory" PREFIX=/ bin_dir=usr/sbin \
            LIBDIR=usr/lib/aarch64-linux-gnu exclude_modules="$excluded" install
        rm -rf -- /etc/opensips /share
        ;;
    configure)
        getent group opensips >/dev/null || groupadd --system opensips
        id opensips >/dev/null 2>&1 || useradd --system --gid opensips \
            --home-dir /var/lib/opensips --no-create-home --shell /usr/sbin/nologin opensips
        install -d -o root -g opensips -m 0750 /etc/opensips
        install -d -o opensips -g opensips -m 0750 /var/lib/opensips /var/log/opensips
        install -o root -g root -m 0755 "$root/assets/opensips-runtime-config.py" \
            /usr/local/sbin/opensips-runtime-config
        install -o root -g opensips -m 0640 "$root/assets/opensips.cfg.template" \
            /etc/opensips/opensips.cfg.template
        install -o root -g root -m 0644 "$root/assets/opensips.service" \
            "$root/assets/opensips-runtime-config.service" /etc/systemd/system/
        systemctl daemon-reload
        systemctl enable opensips-runtime-config.service opensips.service
        ;;
    cleanup)
        DEBIAN_FRONTEND=noninteractive timeout 600 apt-get purge -y --auto-remove "${build_packages[@]}"
        apt-get clean
        rm -rf -- "$source_directory" /var/lib/apt/lists/*
        ;;
    verify)
        file /usr/sbin/opensips | grep -q 'ARM aarch64'
        /usr/sbin/opensips -V
        for binary in /usr/sbin/opensips "$module_directory"/*.so; do
            dependencies=$(ldd "$binary")
            if grep -q 'not found' <<< "$dependencies"; then exit 1; fi
        done
        systemd-analyze verify /etc/systemd/system/opensips.service \
            /etc/systemd/system/opensips-runtime-config.service
        systemctl is-enabled opensips.service opensips-runtime-config.service
        python3 "$root/provision/inputs.py" verify
        ;;
    sanitize)
        cloud-init clean --logs --machine-id
        rm -f /etc/ssh/ssh_host_* /root/.bash_history
        truncate -s 0 /var/log/lastlog /var/log/wtmp /var/log/btmp
        rm -rf -- "$root" /tmp/opensips-image-upload
        ;;
    *) printf '%s\n' 'unknown image provisioning phase' >&2; exit 64 ;;
esac
