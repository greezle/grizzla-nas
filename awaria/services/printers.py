"""Connectivity + discovery: ping sweep, reverse mDNS, the ONLINE map."""
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from awaria.config import SUBNET_PREFIX, FARM_HOST_RE
from awaria.db import db_lock, net_log, open_db
from awaria.services import bus
from awaria.services.telemetry import live_lock, live_of, IP2HOST


# live connectivity of the printers, maintained by ping_worker()
online_lock = threading.Lock()


ONLINE = {}  # hostname -> bool


def ping_ip(ip):
    # two attempts: the printers' lwip deprioritizes ICMP under load, and a
    # single missed 1 s ping used to flag phantom 30 s "disconnects" (22 of
    # 23 audited offline events had telemetry flowing right through them)
    return subprocess.run(["ping", "-c", "2", "-i", "0.3", "-W", "1", ip],
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
                mapped = {
                    r["hostname"]: r["last_ip"]
                    for r in db.execute(
                        "SELECT hostname, last_ip FROM printers"
                        " WHERE last_ip IS NOT NULL")
                }
                # resolve every alive address except the confirmed (online)
                # ones: a STALE last_ip of another printer used to shadow
                # re-discovery after DHCP address churn
                with online_lock:
                    online_now = dict(ONLINE)
                confirmed = {
                    ip
                    for host, ip in mapped.items() if online_now.get(host)
                }
                unknown = [ip for ip in alive if ip not in confirmed]
                for ip, host in resolve_mdns(unknown).items():
                    # only farm-scheme hostnames (or already-known ones) - the
                    # subnet also has PCs, phones, the NAS itself...
                    if not (FARM_HOST_RE.match(host) or host in known_hosts):
                        continue
                    old = mapped.get(host)
                    if old == ip:
                        continue
                    db.execute(
                        "UPDATE printers SET last_ip=NULL"
                        " WHERE last_ip=? AND hostname != ?", (ip, host))
                    db.execute(
                        "INSERT OR IGNORE INTO printers(hostname) VALUES (?)",
                        (host, ))
                    db.execute(
                        "UPDATE printers SET last_ip=? WHERE hostname=?",
                        (ip, host))
                    if old:
                        net_log(db, host, "rediscovered",
                                f"{old} -> {ip} (mDNS)")
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
                    # fresh telemetry IS proof of life - stronger than ping
                    results[host] = ok or live_of(host, max_age=75) is not None
        with online_lock:
            prev = dict(ONLINE)
            changed = results != ONLINE
            ONLINE.clear()
            ONLINE.update(results)
        if changed:
            bus.publish("printers")

        # connectivity audit + automatic re-discovery; must never kill the
        # worker, whatever the network throws at it
        try:
            went_off = [
                h for h, ok in results.items() if not ok and prev.get(h)
            ]
            came_back = [
                h for h, ok in results.items() if ok and prev.get(h) is False
            ]
            if went_off or came_back:
                with db_lock, open_db() as db:
                    for h in went_off:
                        row = db.execute(
                            "SELECT file FROM print_log WHERE hostname=?"
                            " AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                            (h, )).fetchone()
                        net_log(
                            db, h,
                            "offline_mid_print" if row else "offline",
                            f"drukowany plik: {row['file']}" if row else None)
                    for h in came_back:
                        row = db.execute(
                            "SELECT at_ts FROM net_log WHERE hostname=?"
                            " AND event LIKE 'offline%'"
                            " ORDER BY id DESC LIMIT 1", (h, )).fetchone()
                        gap = (f"po {(int(time.time()) - row['at_ts']) // 60}"
                               " min przerwy") if row else None
                        net_log(db, h, "online", gap)
                    db.commit()

            # every offline printer: ask the network for its hostname and
            # adopt the new address -> reconnect survives DHCP churn
            if went_off:
                # someone vanished: sweep on the next cycle (30 s) instead of
                # waiting out the regular ~4 min sweep period - a printer that
                # came back under a new DHCP address is re-adopted quickly
                cycle = 8
        except Exception as ex:  # noqa: BLE001
            print("net audit error:", ex)

        cycle += 1
        time.sleep(30)


def is_online(host):
    with online_lock:
        return ONLINE.get(host, False)
