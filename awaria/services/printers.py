"""Connectivity + discovery: ping sweep, reverse mDNS, the ONLINE map."""
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from awaria.config import SUBNET_PREFIX, FARM_HOST_RE
from awaria.db import db_lock, open_db
from awaria.services import bus
from awaria.services.telemetry import live_lock, IP2HOST


# live connectivity of the printers, maintained by ping_worker()
online_lock = threading.Lock()


ONLINE = {}  # hostname -> bool


def ping_ip(ip):
    return subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def resolve_mdns(ips):
    """ip -> hostname via reverse mDNS (printers announce '<host>.local';
    the LAN has no regular DNS). Unanswered addresses are simply absent."""
    result = {}
    for i in range(0, len(ips), 24):
        chunk = ips[i:i + 24]
        try:
            out = subprocess.run(["avahi-resolve", "--address", *chunk],
                                 capture_output=True,
                                 text=True,
                                 timeout=30).stdout
        except (subprocess.TimeoutExpired, OSError):
            continue
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1]:
                result[parts[0]] = parts[1].removesuffix(".local")
    return result


def ping_worker():
    """Connectivity + discovery daemon. Every 30 s pings the known printer
    IPs for the live map; every ~4 min additionally sweeps the whole subnet
    and reverse-mDNS-resolves new responders, so printers get their IPs
    registered without any firmware support or manual entry."""
    cycle = 0
    while True:
        if cycle % 8 == 0:
            candidates = [f"{SUBNET_PREFIX}{n}" for n in range(1, 255)]
            with ThreadPoolExecutor(max_workers=48) as pool:
                alive = [
                    ip for ip, ok in zip(candidates,
                                         pool.map(ping_ip, candidates)) if ok
                ]
            with db_lock, open_db() as db:
                known_hosts = {
                    r["hostname"]
                    for r in db.execute("SELECT hostname FROM printers")
                }
                known_ips = {
                    r["last_ip"]
                    for r in db.execute(
                        "SELECT last_ip FROM printers WHERE last_ip IS NOT NULL"
                    )
                }
                unknown = [ip for ip in alive if ip not in known_ips]
                for ip, host in resolve_mdns(unknown).items():
                    # only farm-scheme hostnames (or already-known ones) - the
                    # subnet also has PCs, phones, the NAS itself...
                    if FARM_HOST_RE.match(host) or host in known_hosts:
                        db.execute(
                            "INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                            (host, ))
                        db.execute(
                            "UPDATE printers SET last_ip=? WHERE hostname=?",
                            (ip, host))
                db.commit()

        with db_lock, open_db() as db:
            targets = [(r["hostname"], r["last_ip"]) for r in db.execute(
                "SELECT hostname, last_ip FROM printers WHERE last_ip IS NOT NULL"
            )]
        with live_lock:
            IP2HOST.clear()
            IP2HOST.update({ip: host for host, ip in targets})
        results = {}
        if targets:
            with ThreadPoolExecutor(max_workers=16) as pool:
                for (host,
                     _), ok in zip(targets,
                                   pool.map(lambda t: ping_ip(t[1]), targets)):
                    results[host] = ok
        with online_lock:
            changed = results != ONLINE
            ONLINE.clear()
            ONLINE.update(results)
        if changed:
            bus.publish("printers")
        cycle += 1
        time.sleep(30)


def is_online(host):
    with online_lock:
        return ONLINE.get(host, False)
