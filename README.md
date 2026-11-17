# Cloudflare IP Range Updater

A Python-based automation tool for synchronizing Cloudflare's published IP ranges with **firewalld** and **Nginx** on an Ubuntu server.

The purpose of this project is to ensure that services exposed through Cloudflare's reverse proxy only accept HTTP/HTTPS connections from trusted Cloudflare IP ranges, while keeping the server's firewall and Nginx real-IP configuration synchronized with Cloudflare's official IP range list.

The updater periodically retrieves the latest IPv4 and IPv6 ranges, validates them, compares them with the existing configuration, and applies changes when necessary.

It also implements backups, transaction journaling, rollback procedures and interrupted-transaction recovery.

---

## 1. Environment

The project was implemented on an Oracle Cloud VPS running:

| Component                  | Version / Configuration   |
| -------------------------- | ------------------------- |
| Operating system           | Ubuntu Server 24.04.5 LTS |
| Architecture               | ARM64 (aarch64)           |
| Python                     | 3.12.3                    |
| Firewall                   | firewalld                 |
| Web server / reverse proxy | Nginx                     |
| Service manager            | systemd                   |
| DNS / Proxy provider       | Cloudflare                |

### Relevant system paths

| Purpose                     | Path                                                |
| --------------------------- | --------------------------------------------------- |
| Python updater              | `/usr/local/sbin/update-cloudflare-ips.py`          |
| State directory             | `/var/lib/cloudflare-ip-updater/`                   |
| Persistent state            | `/var/lib/cloudflare-ip-updater/state.json`         |
| Transaction lock            | `/var/lib/cloudflare-ip-updater/update.lock`        |
| Transaction journal         | `/var/lib/cloudflare-ip-updater/pending.json`       |
| Backups                     | `/var/lib/cloudflare-ip-updater/backups/`           |
| Nginx real-IP configuration | `/etc/nginx/conf.d/cloudflare-realip.conf`          |
| systemd service             | `/etc/systemd/system/cloudflare-ip-updater.service` |
| systemd timer               | `/etc/systemd/system/cloudflare-ip-updater.timer`   |

---

## 2. Architecture and objectives

Cloudflare operates as a reverse proxy between visitors and the origin server.

When a visitor accesses a proxied hostname, the connection reaches Cloudflare first. Cloudflare then establishes a separate connection to the origin server.

This introduces two security considerations:

1. The origin server should only accept proxied HTTP/HTTPS connections from trusted Cloudflare infrastructure.
2. Nginx must identify the actual visitor IP rather than recording the Cloudflare proxy address.

The implementation addresses both requirements.

### Network architecture

```text
                  Internet
                      |
                      |
                  Visitor
                      |
                      v
             +----------------+
             |   Cloudflare   |
             | Reverse Proxy  |
             +----------------+
                      |
                      |
             Cloudflare IP ranges
                      |
                      v
             +----------------+
             |    firewalld   |
             |                |
             | cloudflare4    |
             | cloudflare6    |
             +----------------+
                      |
                      |
                      v
             +----------------+
             |     Nginx      |
             |                |
             | Real IP module |
             +----------------+
                      |
                      |
                      v
             +----------------+
             | Local services |
             +----------------+
```

The firewall uses two IP sets:

- `cloudflare4` — trusted Cloudflare IPv4 ranges.
- `cloudflare6` — trusted Cloudflare IPv6 ranges.

HTTP and HTTPS traffic on ports 80 and 443 is permitted through rich rules referencing these IP sets.

Nginx uses the `CF-Connecting-IP` header to recover the original visitor address.

The updater ensures that both components use the same trusted ranges.

### Design goals

- Retrieve official Cloudflare IPv4 and IPv6 ranges automatically.
- Validate all retrieved CIDRs before using them.
- Keep firewalld runtime and permanent configurations synchronized.
- Keep Nginx trusted-proxy configuration synchronized.
- Avoid unnecessary configuration changes.
- Apply updates in a safe order.
- Maintain backups before modifying production configuration.
- Recover from interrupted transactions.
- Prevent concurrent updater executions.
- Provide a dry-run mode.
- Execute automatically through systemd.

---

## 3. Cloudflare API

The updater retrieves IP ranges from Cloudflare's official API:

`https://api.cloudflare.com/client/v4/ips`

The endpoint returns IPv4 and IPv6 ranges, along with metadata such as the API response ETag.

No API token is required to retrieve this public information.

### Validation

Before using the returned data, the updater validates:

- API response success.
- Presence of IPv4 and IPv6 ranges.
- Non-empty address lists.
- Valid CIDR notation.
- Strict network alignment.
- Correct IP address family.
- Duplicate entries.
- ETag availability.

Python's `ipaddress` module is used to validate the networks.

The updater uses strict CIDR parsing, preventing malformed or non-network-aligned entries from silently being accepted.

It also preserves the API's ordering when generating the Nginx configuration.

### Current synchronized ranges

At the time of initial deployment, Cloudflare returned:

**IPv4 — 15 ranges**

```text
173.245.48.0/20
103.21.244.0/22
103.22.200.0/22
103.31.4.0/22
141.101.64.0/18
108.162.192.0/18
190.93.240.0/20
188.114.96.0/20
197.234.240.0/22
198.41.128.0/17
162.158.0.0/15
104.16.0.0/13
104.24.0.0/14
172.64.0.0/13
131.0.72.0/22
```

**IPv6 — 7 ranges**

```text
2400:cb00::/32
2606:4700::/32
2803:f800::/32
2405:b500::/32
2405:8100::/32
2a06:98c0::/29
2c0f:f248::/32
```

These are the ranges present during the initial deployment, not a permanent list. The updater retrieves the authoritative list from Cloudflare on every execution.

---

## 4. firewalld integration

The updater manages two existing firewalld IP sets.

| IP set        | Address family | Initial entries |
| ------------- | -------------- | --------------: |
| `cloudflare4` | IPv4           |              15 |
| `cloudflare6` | IPv6           |               7 |

Both runtime and permanent configurations are managed.

This is important because firewalld maintains separate runtime and permanent configurations:

- Runtime changes affect the currently active firewall.
- Permanent changes survive a reload or system restart.

The updater checks that runtime and permanent entries are synchronized before proceeding.

If they differ, the updater refuses to continue rather than potentially making changes against an inconsistent firewall state.

### Firewall update strategy

The update process follows an add-before-remove approach.

1. Calculate the differences between existing and newly retrieved ranges.
2. Add new ranges to firewalld.
3. Update Nginx to trust the new ranges.
4. Validate and reload Nginx.
5. Remove obsolete firewall entries.
6. Save the updated state.

This ordering avoids removing previously trusted proxy addresses before Nginx has been updated.

Firewall operations are attempted independently, with errors collected and reported.

If an update fails, the updater attempts to reconcile the firewall with its original snapshot.

---

## 5. Nginx integration

Nginx uses a dedicated configuration file:

`/etc/nginx/conf.d/cloudflare-realip.conf`

The configuration contains the trusted Cloudflare proxy ranges and the real-IP directives.

Example:

```nginx
real_ip_header CF-Connecting-IP;
real_ip_recursive on;

# Cloudflare IPv4
set_real_ip_from 173.245.48.0/20;
set_real_ip_from 103.21.244.0/22;

# Additional IPv4 ranges...

# Cloudflare IPv6
set_real_ip_from 2400:cb00::/32;
set_real_ip_from 2606:4700::/32;

# Additional IPv6 ranges...
```

The updater generates the complete configuration automatically from the API response.

### Directive explanation

**`real_ip_header CF-Connecting-IP;`**

Instructs Nginx to obtain the original visitor address from Cloudflare's `CF-Connecting-IP` header.

**`real_ip_recursive on;`**

Enables recursive processing of trusted proxy addresses when determining the client address.

**`set_real_ip_from`**

Defines the addresses and networks trusted to supply the real-IP header.

Trusting only Cloudflare's published ranges is essential. Accepting arbitrary client-supplied forwarding headers would allow IP spoofing.

### Configuration update procedure

When a configuration change is required:

1. Generate the new configuration.
2. Write it atomically to the destination file.
3. Run `nginx -t`.
4. Reload Nginx through systemd.

If validation or reloading fails, the updater attempts to restore the previous configuration and reload Nginx again.

If the existing configuration already matches the API response, no reload is performed.

---

## 6. Python updater implementation

The main script is located at:

`/usr/local/sbin/update-cloudflare-ips.py`

It is owned by root and has permissions `750`.

The implementation is divided into several functional areas.

### API and validation

| Function                    | Responsibility                                         |
| --------------------------- | ------------------------------------------------------ |
| `fetch_cloudflare_ranges()` | Retrieves and validates Cloudflare's published ranges. |

### Firewall management

| Function                             | Responsibility                                    |
| ------------------------------------ | ------------------------------------------------- |
| `get_ipset_entries()`                | Retrieves entries from a firewalld IP set.        |
| `read_firewall_state()`              | Reads runtime and permanent firewall state.       |
| `firewall_change()`                  | Performs individual firewall modifications.       |
| `reconcile_firewall()`               | Reconciles the firewall with a supplied snapshot. |
| `update_firewall()`                  | Adds new trusted ranges.                          |
| `remove_obsolete_firewall_entries()` | Removes outdated ranges after Nginx is updated.   |

### Nginx management

| Function                  | Responsibility                             |
| ------------------------- | ------------------------------------------ |
| `generate_nginx_config()` | Generates the trusted-proxy configuration. |
| `restore_nginx()`         | Restores the previous Nginx configuration. |

### Filesystem and state management

| Function            | Responsibility                                                     |
| ------------------- | ------------------------------------------------------------------ |
| `fsync_directory()` | Synchronizes directory metadata to disk.                           |
| `atomic_write()`    | Writes files atomically using a temporary file and `os.replace()`. |
| `create_backups()`  | Creates a backup of the previous configuration and state.          |
| `save_state()`      | Saves the latest synchronized ranges and ETag.                     |
| `restore_state()`   | Restores the previous updater state.                               |

### Transaction management

| Function                        | Responsibility                                            |
| ------------------------------- | --------------------------------------------------------- |
| `create_pending_journal()`      | Records an active transaction before modifications begin. |
| `recover_pending_transaction()` | Detects and recovers interrupted transactions.            |
| `main()`                        | Coordinates the complete update process.                  |

### Concurrency protection

The updater uses Python's `fcntl.flock()` to acquire an exclusive, non-blocking lock.

The lock file is:

`/var/lib/cloudflare-ip-updater/update.lock`

If another instance is already running, the new execution terminates instead of allowing concurrent modifications.

---

## 7. Transaction safety and recovery

One of the most important parts of the implementation is its transaction mechanism.

Updating a firewall and a web server configuration involves multiple independent operations. There is no single atomic operation capable of updating both components simultaneously.

A failure halfway through could otherwise leave the system in an inconsistent state.

The updater therefore implements a backup, journal and rollback mechanism.

### Transaction sequence

```text
              Start update
                   |
                   v
           Acquire exclusive lock
                   |
                   v
           Recover pending transaction
                   |
                   v
          Retrieve and validate API data
                   |
                   v
          Read current firewall state
                   |
                   v
          Calculate configuration changes
                   |
                   v
             Create backups
                   |
                   v
          Write pending transaction
                   |
                   v
          Add new firewall ranges
                   |
                   v
          Update Nginx configuration
                   |
                   v
             Validate Nginx
                   |
                   v
             Reload Nginx
                   |
                   v
          Remove obsolete firewall ranges
                   |
                   v
              Save new state
                   |
                   v
          Remove transaction journal
                   |
                   v
                Success
```

### Backup structure

Backups are stored under:

`/var/lib/cloudflare-ip-updater/backups/`

Each transaction receives its own timestamped directory.

A backup contains:

- Previous Nginx configuration.
- Previous updater state, if one exists.
- Previous firewalld runtime and permanent IP-set snapshots.

The firewall snapshot is serialized into `firewall.json`.

Backup files and directories are synchronized to disk before the transaction journal is written.

### Pending transaction journal

The journal is stored at:

`/var/lib/cloudflare-ip-updater/pending.json`

It contains:

- The backup directory path.
- The transaction creation timestamp.

The journal is created before any production configuration changes occur.

It acts as a marker indicating that an update transaction has started but has not yet been conclusively committed.

### Atomic writes

Configuration and state files are written using temporary files in the target directory.

The process:

1. Creates a temporary file.
2. Writes the new content.
3. Flushes the file.
4. Calls `fsync()` on the file.
5. Applies the required permissions and ownership.
6. Replaces the destination using `os.replace()`.
7. Synchronizes the parent directory.

This reduces the possibility of partially written configuration files after an unexpected interruption.

### Rollback

If an update fails, the updater independently attempts to restore:

1. Nginx configuration.
2. Firewall runtime and permanent state.
3. Previous updater state.

The transaction journal is removed only if all restoration operations succeed.

If any restoration operation fails, the journal is retained so that a subsequent execution can attempt recovery.

### Interrupted transaction recovery

At startup, after acquiring the exclusive lock, the updater checks for `pending.json`.

If it exists, the updater attempts to recover the previous transaction before fetching new Cloudflare data.

Recovery includes validation of:

- Journal structure and timestamp.
- Backup path.
- Backup completeness.
- Firewall snapshot structure.
- CIDR validity and address families.
- Runtime/permanent firewall consistency.
- Required Nginx real-IP directives.
- Agreement between Nginx trusted ranges and the firewall snapshot.
- Previous updater state consistency, if a state backup exists.

Only after the backup passes validation does recovery begin.

The previous Nginx configuration, firewall state and updater state are then restored independently.

The journal is removed only after all three restoration operations succeed.

---

## 8. Persistent updater state

The updater stores its latest successful synchronization state in:

`/var/lib/cloudflare-ip-updater/state.json`

Example:

```json
{
  "etag": "38f79d050aa027e3be3865e495dcc9bc",
  "updated_at": "2026-11-17T12:07:07.833658+00:00",
  "ipv4": ["173.245.48.0/20", "103.21.244.0/22"],
  "ipv6": ["2400:cb00::/32", "2606:4700::/32"]
}
```

The example contains abbreviated range lists for readability.

The file records:

- Last retrieved Cloudflare ETag.
- Timestamp of the last saved synchronization state.
- IPv4 ranges.
- IPv6 ranges.

The state file is owned by root and has permissions `600`.

The state directory and backup directory both use permissions `700`.

---

## 9. Dry-run mode

The updater provides a `--dry-run` argument.

Usage:

```bash
sudo /usr/local/sbin/update-cloudflare-ips.py --dry-run
```

Dry-run mode:

1. Retrieves the latest Cloudflare ranges.
2. Validates the response.
3. Reads the current firewalld configuration.
4. Compares the existing firewall entries against the new ranges.
5. Compares the existing Nginx configuration.
6. Reports proposed additions and removals.

It does not apply firewall or Nginx changes, create transaction backups, or save a new synchronization state.

Example output:

```text
[INFO] Fetching Cloudflare IP ranges
[INFO] Validated 15 IPv4 and 7 IPv6 ranges
[INFO] cloudflare4: 15 synchronized entries
[INFO] cloudflare6: 7 synchronized entries
[INFO] Cloudflare ETag: 38f79d050aa027e3be3865e495dcc9bc
[INFO] cloudflare4: 0 additions, 0 removals
[INFO] cloudflare6: 0 additions, 0 removals
[INFO] Nginx configuration is already up to date
[INFO] DRY RUN: no changes were made
```

**Important:** pending-transaction recovery occurs before the dry-run check. Therefore, if an interrupted transaction is detected, recovery can modify the live configuration even when `--dry-run` was supplied.

---

## 10. systemd integration

The updater is executed automatically through a dedicated systemd oneshot service and weekly timer.

### Service configuration

File:

`/etc/systemd/system/cloudflare-ip-updater.service`

```ini
[Unit]
Description=Synchronize Cloudflare IP ranges
Wants=network-online.target
After=network-online.target firewalld.service nginx.service

[Service]
Type=oneshot
User=root
ExecStart=/usr/local/sbin/update-cloudflare-ips.py
UMask=0077
NoNewPrivileges=true
```

#### Service configuration explanation

- `Type=oneshot`: systemd waits for the updater to finish.
- `User=root`: required for firewall and system configuration changes.
- `ExecStart`: executes the Python updater.
- `UMask=0077`: restricts permissions on newly created files.
- `NoNewPrivileges=true`: prevents the process from gaining additional privileges.
- `After=`: ensures the relevant services are started before execution.

### Timer configuration

File:

`/etc/systemd/system/cloudflare-ip-updater.timer`

```ini
[Unit]
Description=Weekly Cloudflare IP range synchronization

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=1h
Unit=cloudflare-ip-updater.service

[Install]
WantedBy=timers.target
```

#### Timer configuration explanation

- `OnCalendar=weekly`: executes once per week.
- `Persistent=true`: missed executions are triggered after the system becomes available again.
- `RandomizedDelaySec=1h`: adds a randomized delay of up to one hour.
- `Unit=`: identifies the service executed by the timer.

### Enable the timer

Reload systemd and enable the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflare-ip-updater.timer
```

### Verify the timer

```bash
systemctl status cloudflare-ip-updater.timer
```

List scheduled executions:

```bash
systemctl list-timers cloudflare-ip-updater.timer
```

### Execute manually through systemd

```bash
sudo systemctl start cloudflare-ip-updater.service
```

Inspect service status:

```bash
systemctl status cloudflare-ip-updater.service
```

Inspect logs:

```bash
sudo journalctl -u cloudflare-ip-updater.service -n 30 --no-pager
```

Follow logs live:

```bash
sudo journalctl -u cloudflare-ip-updater.service -f
```

A oneshot service normally returns to `inactive (dead)` after successful execution. This is expected behavior. The important indicator is:

```text
code=exited, status=0/SUCCESS
```

---

## 11. Deployment and verification

The following commands were used to validate the deployment.

### Python syntax validation

```bash
sudo python3 -m py_compile /usr/local/sbin/update-cloudflare-ips.py
```

### Nginx configuration validation

```bash
sudo nginx -t
```

### Inspect firewalld IP sets

IPv4 runtime:

```bash
sudo firewall-cmd --ipset=cloudflare4 --get-entries
```

IPv4 permanent:

```bash
sudo firewall-cmd --permanent --ipset=cloudflare4 --get-entries
```

IPv6 runtime:

```bash
sudo firewall-cmd --ipset=cloudflare6 --get-entries
```

IPv6 permanent:

```bash
sudo firewall-cmd --permanent --ipset=cloudflare6 --get-entries
```

### Run a dry run

```bash
sudo /usr/local/sbin/update-cloudflare-ips.py --dry-run
```

### Execute a real update

```bash
sudo /usr/local/sbin/update-cloudflare-ips.py
```

### Inspect persistent state

```bash
sudo cat /var/lib/cloudflare-ip-updater/state.json
```

### Inspect permissions

```bash
sudo stat -c '%A %U:%G %n' \
    /usr/local/sbin/update-cloudflare-ips.py \
    /var/lib/cloudflare-ip-updater \
    /var/lib/cloudflare-ip-updater/backups \
    /var/lib/cloudflare-ip-updater/state.json
```

Expected permissions:

```text
-rwxr-x--- root:root update-cloudflare-ips.py
drwx------ root:root cloudflare-ip-updater
drwx------ root:root backups
-rw------- root:root state.json
```

---

## 12. Deployment results

Initial deployment and validation were completed on **17 November 2026**.

The following checks were successful:

| Test                                    | Result                     |
| --------------------------------------- | -------------------------- |
| Python syntax compilation               | Passed                     |
| Nginx configuration validation          | Passed                     |
| Cloudflare API retrieval                | Passed                     |
| IPv4 CIDR validation                    | Passed (15 ranges)         |
| IPv6 CIDR validation                    | Passed (7 ranges)          |
| firewalld runtime/permanent consistency | Passed                     |
| Nginx/firewalld range consistency       | Passed                     |
| Pending transaction check               | Passed                     |
| Dry-run execution                       | Passed                     |
| Direct updater execution                | Passed                     |
| systemd unit verification               | No updater-specific errors |
| systemd timer activation                | Passed                     |
| systemd service execution               | Passed                     |
| journald logging                        | Passed                     |

At initial deployment, all configurations were already synchronized.

Consequently, the first live execution initialized the persistent state without modifying the firewall or Nginx configuration.

The systemd service was subsequently triggered manually and also completed successfully with exit status `0`.

The weekly timer was confirmed active.

---

## 13. Known limitations and future testing

The initial deployment verified the normal execution path, including API retrieval, validation, synchronization checks, dry-run operation and systemd execution.

However, several failure scenarios have not yet been exercised through controlled tests.

### Outstanding tests

- Simulate Cloudflare API unavailability.
- Test malformed or invalid API responses.
- Test a newly added Cloudflare CIDR.
- Test removal of an obsolete CIDR.
- Test firewalld modification failures.
- Test Nginx configuration validation failures.
- Test rollback after a partially completed update.
- Simulate an interrupted transaction and verify automatic recovery.
- Verify that an incomplete recovery retains the transaction journal.

These tests should be performed in an isolated environment or with mocked system operations, rather than deliberately disrupting the production VPS firewall.

### Future improvements

Potential future improvements include:

- Automated unit tests for CIDR validation and transaction handling.
- Integration tests using mocked firewalld and systemd commands.
- Automated recovery testing.
- More detailed monitoring and failure notifications.
- Backup retention and cleanup policies.
- Additional systemd sandboxing, where compatible with required operations.

---

## 14. Security considerations

Several security measures are incorporated into the implementation:

- Only Cloudflare-published IP ranges are trusted.
- CIDRs are strictly validated before application.
- Runtime and permanent firewall state must agree.
- Nginx receives trusted-proxy ranges from the same API response as firewalld.
- New firewall entries are added before obsolete ones are removed.
- Nginx is validated before being reloaded.
- Configuration files are written atomically.
- Backups are created before production changes.
- A transaction journal is persisted before modifications.
- Rollback is attempted independently for each subsystem.
- Interrupted transactions are recovered on subsequent executions.
- An exclusive file lock prevents concurrent executions.
- Persistent state and backups are accessible only to root.

The updater is intended to manage the trusted Cloudflare proxy ranges for HTTP/HTTPS traffic. Other services are outside its scope.

---

## Conclusion

This project automates the maintenance of trusted Cloudflare proxy ranges across firewalld and Nginx.

Beyond basic API retrieval and configuration generation, it incorporates several reliability mechanisms commonly used in production infrastructure automation: strict input validation, atomic file replacement, filesystem synchronization, transaction journaling, rollback and recovery.

It also demonstrates practical integration between Python, Linux firewall management, reverse-proxy configuration and systemd scheduling.

The result is a self-maintaining component of the VPS infrastructure that periodically checks Cloudflare's published ranges and keeps the origin server's trusted-proxy configuration synchronized.
