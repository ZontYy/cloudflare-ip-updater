#!/usr/bin/env python3

import argparse
import fcntl
import ipaddress
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# Configuration
API_URL = "https://api.cloudflare.com/client/v4/ips"

STATE_DIR = Path("/var/lib/cloudflare-ip-updater")
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "update.lock"
BACKUP_DIR = STATE_DIR / "backups"
PENDING_FILE = STATE_DIR / "pending.json"

NGINX_FILE = Path("/etc/nginx/conf.d/cloudflare-realip.conf")

IPSETS = {
    "ipv4": "cloudflare4",
    "ipv6": "cloudflare6",
}

TIMEOUT = 20


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

log = logging.getLogger("cloudflare-updater")


def run_command(args):
    """Execute a system command and raise on failure."""

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(args)}\n"
            f"{result.stderr.strip()}"
        )

    return result.stdout.strip()


def fetch_cloudflare_ranges():
    """Fetch and validate Cloudflare's official IP ranges."""

    log.info("Fetching Cloudflare IP ranges")

    request = urllib.request.Request(
        API_URL,
        headers={"User-Agent": "Cloudflare-IP-Updater/1.0"},
    )

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = json.loads(response.read())

    if data.get("success") is not True:
        raise RuntimeError("Cloudflare API reported failure")

    result = data.get("result", {})

    validated = {}

    for family, key, version in (
        ("ipv4", "ipv4_cidrs", 4),
        ("ipv6", "ipv6_cidrs", 6),
    ):
        raw_ranges = result.get(key)

        if not isinstance(raw_ranges, list) or not raw_ranges:
            raise RuntimeError(f"Missing or empty {key}")

        networks = []
        seen = set()

        for item in raw_ranges:
            if not isinstance(item, str):
                raise RuntimeError(f"Invalid CIDR value: {item!r}")

            network = ipaddress.ip_network(item, strict=True)

            if network.version != version:
                raise RuntimeError(
                    f"Wrong address family in {key}: {item}"
                )

            normalized = str(network)

            if normalized not in seen:
                networks.append(normalized)
                seen.add(normalized)

        if not networks:
            raise RuntimeError(f"No valid ranges in {key}")

        validated[family] = networks

    etag = result.get("etag")

    if not isinstance(etag, str) or not etag:
        raise RuntimeError("Cloudflare API returned no ETag")

    log.info(
        "Validated %d IPv4 and %d IPv6 ranges",
        len(validated["ipv4"]),
        len(validated["ipv6"]),
    )

    return validated, etag

def get_ipset_entries(ipset, permanent=False):
    """Read the current entries of a firewalld IP set."""

    args = ["firewall-cmd"]

    if permanent:
        args.append("--permanent")

    args.extend([
        f"--ipset={ipset}",
        "--get-entries",
    ])

    output = run_command(args)

    return set(output.split()) if output else set()


def read_firewall_state():
    """Capture runtime and permanent IP set configurations."""

    snapshot = {}

    for family, ipset in IPSETS.items():
        runtime = get_ipset_entries(ipset)
        permanent = get_ipset_entries(ipset, permanent=True)

        if runtime != permanent:
            raise RuntimeError(
                f"{ipset}: runtime and permanent entries differ. "
                "Refusing automatic update."
            )

        snapshot[family] = {
            "runtime": runtime,
            "permanent": permanent,
        }

        log.info(
            "%s: %d synchronized entries",
            ipset,
            len(runtime),
        )

    return snapshot


def firewall_change(ipset, entry, action, permanent=False):
    """Add or remove one IP set entry."""

    args = ["firewall-cmd"]

    if permanent:
        args.append("--permanent")

    args.extend([
        f"--ipset={ipset}",
        f"--{action}-entry={entry}",
    ])

    run_command(args)


def reconcile_firewall(snapshot):
    """Restore previous firewalld state, attempting every operation."""

    log.warning("Restoring previous firewalld configuration")

    errors = []

    for family, ipset in IPSETS.items():
        desired = snapshot[family]

        for permanent in (True, False):
            current_label = "permanent" if permanent else "runtime"

            try:
                current = get_ipset_entries(
                    ipset,
                    permanent=permanent,
                )

                target = desired[current_label]

                for entry in sorted(target - current):
                    try:
                        firewall_change(
                            ipset, entry, "add", permanent
                        )
                    except Exception as exc:
                        errors.append(
                            f"{ipset} {current_label}: "
                            f"failed to add {entry}: {exc}"
                        )

                # Re-read state because some operations may have failed.
                current = get_ipset_entries(
                    ipset,
                    permanent=permanent,
                )

                for entry in sorted(current - target):
                    try:
                        firewall_change(
                            ipset, entry, "remove", permanent
                        )
                    except Exception as exc:
                        errors.append(
                            f"{ipset} {current_label}: "
                            f"failed to remove {entry}: {exc}"
                        )

            except Exception as exc:
                errors.append(
                    f"{ipset} {current_label}: {exc}"
                )

    if errors:
        for error in errors:
            log.error("Rollback error: %s", error)

        raise RuntimeError(
            f"Firewall rollback incomplete: {len(errors)} error(s)"
        )

    log.info("Previous firewall state successfully restored")


def update_firewall(snapshot, desired):
    """Add new entries first, then remove obsolete entries."""

    for family, ipset in IPSETS.items():
        old = snapshot[family]["runtime"]
        new = set(desired[family])

        additions = new - old
        removals = old - new

        log.info(
            "%s: %d additions, %d removals",
            ipset,
            len(additions),
            len(removals),
        )

        # Add new entries to permanent and runtime configurations.
        for entry in sorted(additions):
            firewall_change(ipset, entry, "add", True)
            firewall_change(ipset, entry, "add", False)

    # Obsolete entries are removed only after Nginx is validated
    # and successfully reloaded.


def remove_obsolete_firewall_entries(snapshot, desired):
    """Remove ranges no longer published by Cloudflare."""

    for family, ipset in IPSETS.items():
        old = snapshot[family]["runtime"]
        new = set(desired[family])

        for entry in sorted(old - new):
            firewall_change(ipset, entry, "remove", True)
            firewall_change(ipset, entry, "remove", False)


def generate_nginx_config(ranges):
    """Generate the Nginx trusted-proxy configuration."""

    lines = [
        "real_ip_header CF-Connecting-IP;",
        "real_ip_recursive on;",
        "",
        "# Cloudflare IPv4",
    ]

    for cidr in ranges["ipv4"]:
        lines.append(f"set_real_ip_from {cidr};")

    lines.extend([
        "",
        "# Cloudflare IPv6",
    ])

    for cidr in ranges["ipv6"]:
        lines.append(f"set_real_ip_from {cidr};")

    return "\n".join(lines) + "\n"


def fsync_directory(path):
    """Ensure directory metadata is flushed to disk."""

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)

    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, content, mode=0o644):
    """Atomically replace a file in its existing directory."""

    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )

    try:
        with os.fdopen(fd, "w") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        os.chmod(temporary, mode)
        os.chown(temporary, 0, 0)

        os.replace(temporary, path)
        fsync_directory(path.parent)

    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def create_backups(firewall_snapshot):
    """Back up Nginx configuration and firewalld state."""

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    backup = BACKUP_DIR / timestamp

    backup.mkdir(parents=True, exist_ok=False)

    shutil.copy2(
        NGINX_FILE,
        backup / NGINX_FILE.name,
    )

    # Preserve the previous state file if one exists.
    if STATE_FILE.exists():
        shutil.copy2(
            STATE_FILE,
            backup / STATE_FILE.name,
        )

    firewall_backup = backup / "firewall.json"

    serializable_snapshot = {
        family: {
            state: sorted(entries)
            for state, entries in states.items()
        }
        for family, states in firewall_snapshot.items()
    }

    with firewall_backup.open("w") as file:
        json.dump(serializable_snapshot, file, indent=4)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())

    # Flush backup files and directory metadata to disk.
    for backup_file in backup.iterdir():
        if backup_file.is_file():
            fd = os.open(backup_file, os.O_RDONLY)

            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    fsync_directory(backup)
    fsync_directory(BACKUP_DIR)

    log.info("Created backup: %s", backup)

    return backup


def create_pending_journal(backup):
    """Mark an update transaction as pending."""

    journal = {
        "backup": str(backup),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    atomic_write(
        PENDING_FILE,
        json.dumps(journal, indent=2) + "\n",
        0o600,
    )

    log.info("Created pending transaction journal")


def restore_nginx(original_content, original_mode):
    """Restore the previous Nginx configuration."""

    log.warning("Restoring previous Nginx configuration")

    fd, temporary = tempfile.mkstemp(
        prefix=".cloudflare-restore.",
        dir=NGINX_FILE.parent,
    )

    try:
        with os.fdopen(fd, "wb") as file:
            file.write(original_content)
            file.flush()
            os.fsync(file.fileno())

        os.chmod(temporary, original_mode)
        os.chown(temporary, 0, 0)

        os.replace(temporary, NGINX_FILE)

    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def save_state(ranges, etag):
    """Save successfully applied ranges and ETag."""

    state = {
        "etag": etag,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "ipv4": ranges["ipv4"],
        "ipv6": ranges["ipv6"],
    }

    atomic_write(
        STATE_FILE,
        json.dumps(state, indent=2) + "\n",
        0o600,
    )


def restore_state(backup):
    """Restore the previous updater state file."""

    state_backup = backup / STATE_FILE.name

    if state_backup.is_file():
        original_content = state_backup.read_text(encoding="utf-8")
        original_mode = stat.S_IMODE(state_backup.stat().st_mode)

        atomic_write(
            STATE_FILE,
            original_content,
            original_mode,
        )

        log.info("Previous updater state restored")

    else:
        STATE_FILE.unlink(missing_ok=True)
        log.info("Previous updater state did not exist; removed current state")


def recover_pending_transaction():
    """Recover an interrupted update from its persisted backup."""

    if not PENDING_FILE.exists():
        return

    log.warning("Pending transaction detected; starting recovery")

    with PENDING_FILE.open() as file:
        journal = json.load(file)

    if not isinstance(journal, dict):
        raise RuntimeError("Invalid transaction journal format")

    if set(journal) != {"backup", "created_at"}:
        raise RuntimeError("Invalid transaction journal fields")

    if not isinstance(journal["backup"], str) or not journal["backup"]:
        raise RuntimeError("Invalid backup path in transaction journal")

    if not isinstance(journal["created_at"], str):
        raise RuntimeError("Invalid transaction timestamp")

    try:
        datetime.fromisoformat(journal["created_at"])
    except ValueError as exc:
        raise RuntimeError("Invalid transaction timestamp") from exc

    backup = Path(journal["backup"]).resolve()
    backup_root = BACKUP_DIR.resolve()

    # Ensure the journal references a backup inside our backup directory.
    if backup.parent != backup_root:
        raise RuntimeError("Invalid backup path in transaction journal")

    nginx_backup = backup / NGINX_FILE.name
    firewall_backup = backup / "firewall.json"

    if not nginx_backup.is_file() or not firewall_backup.is_file():
        raise RuntimeError("Transaction backup is incomplete")

    with firewall_backup.open() as file:
        raw_snapshot = json.load(file)

    if not isinstance(raw_snapshot, dict):
        raise RuntimeError("Invalid firewall snapshot format")

    if set(raw_snapshot) != set(IPSETS):
        raise RuntimeError("Invalid firewall snapshot families")

    firewall_snapshot = {}

    for family, states in raw_snapshot.items():
        if not isinstance(states, dict):
            raise RuntimeError(f"Invalid firewall snapshot for {family}")

        if set(states) != {"runtime", "permanent"}:
            raise RuntimeError(f"Invalid firewall snapshot for {family}")

        expected_version = 4 if family == "ipv4" else 6
        firewall_snapshot[family] = {}

        for state, entries in states.items():
            if not isinstance(entries, list) or not entries:
                raise RuntimeError(
                    f"Invalid or empty {family} {state} entries"
                )

            validated_entries = set()

            for entry in entries:
                if not isinstance(entry, str):
                    raise RuntimeError(
                        f"Invalid CIDR entry in {family} {state}"
                    )

                network = ipaddress.ip_network(entry, strict=True)

                if network.version != expected_version:
                    raise RuntimeError(
                        f"Wrong address family for {entry}"
                    )

                validated_entries.add(str(network))

            if len(validated_entries) != len(entries):
                raise RuntimeError(
                    f"Duplicate entries in {family} {state}"
                )

            firewall_snapshot[family][state] = validated_entries

        if (
            firewall_snapshot[family]["runtime"]
            != firewall_snapshot[family]["permanent"]
        ):
            raise RuntimeError(
                f"Runtime and permanent {family} snapshots differ"
            )

    original_content = nginx_backup.read_bytes()

    if not original_content:
        raise RuntimeError("Nginx backup is empty")

    nginx_lines = original_content.decode("utf-8").splitlines()
    nginx_directives = {line.strip() for line in nginx_lines}

    required_directives = {
        "real_ip_header CF-Connecting-IP;",
        "real_ip_recursive on;",
    }

    missing_directives = required_directives - nginx_directives

    if missing_directives:
        raise RuntimeError(
            f"Nginx backup is missing required directives: "
            f"{', '.join(sorted(missing_directives))}"
        )

    nginx_ranges = {
        "ipv4": set(),
        "ipv6": set(),
    }

    for line in nginx_lines:
        line = line.strip()

        if line.startswith("set_real_ip_from "):
            if not line.endswith(";"):
                raise RuntimeError("Malformed Nginx trusted-proxy directive")

            cidr = line[len("set_real_ip_from "):-1].strip()
            network = ipaddress.ip_network(cidr, strict=True)

            family = f"ipv{network.version}"
            nginx_ranges[family].add(str(network))

    for family in IPSETS:
        if nginx_ranges[family] != firewall_snapshot[family]["runtime"]:
            raise RuntimeError(
                f"Nginx backup and firewall snapshot differ for {family}"
            )

    original_mode = stat.S_IMODE(nginx_backup.stat().st_mode)

    state_backup = backup / STATE_FILE.name

    if state_backup.exists():
        if not state_backup.is_file() or state_backup.stat().st_size == 0:
            raise RuntimeError("Previous state backup is invalid")

        with state_backup.open() as file:
            previous_state = json.load(file)

        required_fields = {"etag", "updated_at", "ipv4", "ipv6"}

        if not isinstance(previous_state, dict):
            raise RuntimeError("Previous state backup has invalid format")

        if set(previous_state) != required_fields:
            raise RuntimeError("Previous state backup has invalid fields")

        if not isinstance(previous_state["etag"], str):
            raise RuntimeError("Previous state ETag is invalid")

        if not isinstance(previous_state["updated_at"], str):
            raise RuntimeError("Previous state timestamp is invalid")

        try:
            datetime.fromisoformat(previous_state["updated_at"])
        except ValueError as exc:
            raise RuntimeError("Previous state timestamp is invalid") from exc

        for family in IPSETS:
            entries = previous_state[family]

            if not isinstance(entries, list) or not entries:
                raise RuntimeError(
                    f"Previous {family} state is invalid"
                )

            validated_entries = set()

            for entry in entries:
                if not isinstance(entry, str):
                    raise RuntimeError(
                        f"Invalid {family} entry in previous state"
                    )

                network = ipaddress.ip_network(entry, strict=True)

                expected_version = 4 if family == "ipv4" else 6

                if network.version != expected_version:
                    raise RuntimeError(
                        f"Wrong address family in previous {family} state"
                    )

                validated_entries.add(str(network))

            if len(validated_entries) != len(entries):
                raise RuntimeError(
                    f"Duplicate entries in previous {family} state"
                )

            if validated_entries != firewall_snapshot[family]["runtime"]:
                raise RuntimeError(
                    f"Previous {family} state differs from firewall snapshot"
                )

    nginx_restored = False
    firewall_restored = False
    state_restored = False

    try:
        restore_nginx(original_content, original_mode)
        run_command(["nginx", "-t"])
        run_command(["systemctl", "reload", "nginx"])

        nginx_restored = True
        log.info("Nginx recovery successful")

    except Exception:
        log.exception("Nginx recovery failed")

    try:
        reconcile_firewall(firewall_snapshot)

        firewall_restored = True
        log.info("Firewall recovery successful")

    except Exception:
        log.exception("Firewall recovery failed")

    try:
        restore_state(backup)
        state_restored = True

    except Exception:
        log.exception("Failed to restore updater state")

    if not (nginx_restored and firewall_restored and state_restored):
        raise RuntimeError(
            "Automatic recovery incomplete; journal retained"
        )

    PENDING_FILE.unlink()
    fsync_directory(PENDING_FILE.parent)

    log.info("Interrupted transaction successfully recovered")


def main(dry_run=False):
    if os.geteuid() != 0:
        raise RuntimeError("This script must run as root")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(
                lock,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            raise RuntimeError("Another updater is already running")

        recover_pending_transaction()

        ranges, etag = fetch_cloudflare_ranges()
        firewall_snapshot = read_firewall_state()

        original_content = NGINX_FILE.read_bytes()
        original_mode = stat.S_IMODE(NGINX_FILE.stat().st_mode)

        new_config = generate_nginx_config(ranges)
        current_config = original_content.decode("utf-8")

        config_changed = current_config != new_config

        firewall_changes = {}

        for family in IPSETS:
            old = firewall_snapshot[family]["runtime"]
            new = set(ranges[family])

            firewall_changes[family] = {
                "add": new - old,
                "remove": old - new,
            }

        has_changes = config_changed or any(
            changes["add"] or changes["remove"]
            for changes in firewall_changes.values()
        )

        # Report the proposed changes.
        log.info("Cloudflare ETag: %s", etag)

        for family, changes in firewall_changes.items():
            log.info(
                "%s: %d additions, %d removals",
                IPSETS[family],
                len(changes["add"]),
                len(changes["remove"]),
            )

            for entry in sorted(changes["add"]):
                log.info("  ADD: %s", entry)

            for entry in sorted(changes["remove"]):
                log.info("  REMOVE: %s", entry)

        if config_changed:
            log.info("Nginx trusted-proxy configuration needs updating")
        else:
            log.info("Nginx configuration is already up to date")

        if dry_run:
            log.info("DRY RUN: no changes were made")
            return

        if not has_changes:
            log.info("Everything is already up to date")
            save_state(ranges, etag)
            return

        backup = create_backups(firewall_snapshot)

        # Record the transaction before making any changes.
        create_pending_journal(backup)

        try:
            # Add new firewall entries first.
            update_firewall(firewall_snapshot, ranges)

            if config_changed:
                log.info("Updating Nginx configuration")

                atomic_write(
                    NGINX_FILE,
                    new_config,
                    original_mode,
                )

                run_command(["nginx", "-t"])
                run_command(["systemctl", "reload", "nginx"])

                log.info("Nginx successfully reloaded")

            # Remove obsolete entries only after Nginx is updated.
            remove_obsolete_firewall_entries(
                firewall_snapshot,
                ranges,
            )

            save_state(ranges, etag)

            # Commit: remove the journal only after everything succeeds.
            PENDING_FILE.unlink()
            fsync_directory(PENDING_FILE.parent)

            log.info("Update completed successfully")

        except Exception:
            log.exception("Update failed; attempting rollback")

            nginx_restored = True
            firewall_restored = True
            state_restored = False

            if config_changed:
                try:
                    restore_nginx(original_content, original_mode)

                    run_command(["nginx", "-t"])
                    run_command(["systemctl", "reload", "nginx"])

                    log.info("Nginx configuration restored successfully")

                except Exception:
                    nginx_restored = False
                    log.exception("Failed to restore Nginx configuration")

            try:
                reconcile_firewall(firewall_snapshot)
                log.info("Firewall state restored successfully")

            except Exception:
                firewall_restored = False
                log.exception("Failed to restore firewall state")

            try:
                restore_state(backup)
                state_restored = True

            except Exception:
                log.exception("Failed to restore updater state")

            if nginx_restored and firewall_restored and state_restored:
                PENDING_FILE.unlink(missing_ok=True)
                fsync_directory(PENDING_FILE.parent)
                log.info("Rollback completed; transaction journal removed")
            else:
                log.critical(
                    "Rollback incomplete. Pending journal retained for recovery."
                )

            raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Synchronize Cloudflare IP ranges"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show proposed changes without applying them",
    )

    args = parser.parse_args()

    try:
        main(dry_run=args.dry_run)
    except Exception:
        log.exception("Cloudflare IP updater terminated")
        sys.exit(1)
