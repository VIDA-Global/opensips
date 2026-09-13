"""Render and exec the node-local SIPREC demux only in an explicit test topology."""

import ipaddress
import os
from pathlib import Path
import sys

if os.environ.get("SAGE_STOCK_DEMUX_TEST") != "1":
    raise RuntimeError("node demux adapter requires its test overlay")
if sys.argv[1:] not in ([], ["--check"]):
    raise ValueError("unsupported node demux arguments")
cell = str(ipaddress.IPv4Address(os.environ["NATIVE_CELL_IP"]))
proxy = str(ipaddress.IPv4Address(os.environ["NATIVE_PROXY_IP"]))
config = Path("/tests/node-demux.cfg.template").read_text()
config = config.replace("@@CELL_IP@@", cell).replace("@@PROXY_IP@@", proxy)
path = Path("/tmp/node-demux.cfg")
path.write_text(config)
path.chmod(0o600)
options = ["-C"] if sys.argv[1:] else []
os.execv("/usr/local/bin/opensips", ["opensips", "-F", *options, "-f", str(path)])
