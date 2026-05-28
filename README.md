# truenas-csi-api-guard

A filtering WebSocket proxy between the [truenas-csi](https://github.com/truenas/truenas-csi) Kubernetes driver and TrueNAS middleware. It enforces that the CSI driver can only operate on a configured dataset prefix, regardless of the role assigned to the API key.

**Requires TrueNAS 25.10.3.1 or later.**

## Why this exists

The truenas-csi driver authenticates to TrueNAS using an API key over a WebSocket connection. TrueNAS API keys have no dataset-level scope: a key either has access to the full API or it doesn't. There is no built-in way to restrict a key to a specific pool or dataset prefix.

In practice this means the API key given to your Kubernetes cluster has the ability to create, modify, or **permanently delete any dataset on any pool** on your TrueNAS system, including datasets that have nothing to do with Kubernetes.

This is a hard blocker for production and security-hardened environments.

In a multi-pool or multi-tenant setup, a single misconfigured or malicious workload can destroy storage belonging to unrelated systems or other tenants. If a Kubernetes node is compromised, an attacker who gains access to the CSI driver's API key gets unrestricted write access to the entire NAS. And without path enforcement at the API level, there is no guarantee that the CSI driver only touches what it is supposed to touch, even with correct StorageClass configuration.

truenas-csi-api-guard sits in front of the TrueNAS WebSocket API and enforces at the protocol level that:
1. Only a specific allowlist of JSON-RPC methods can be called
2. All dataset, snapshot, and share operations must target a configured path prefix — any call targeting a path outside that prefix is rejected before it ever reaches TrueNAS

The API key still needs to exist, but the blast radius of its compromise is contained to the datasets your Kubernetes cluster is actually supposed to manage.

For the strongest security posture, the TrueNAS native WebSocket port (443) should not be reachable from the Kubernetes cluster at all. Only the proxy port should be exposed to the cluster, ideally through a dedicated VLAN or network segment that carries no other TrueNAS management traffic. This way, even if the cluster is fully compromised, there is no path to the unrestricted TrueNAS API.

## How it works

- Listens on a configurable address and port with TLS, using TrueNAS's own certificate
- Accepts WSS connections from the CSI driver
- Inspects every JSON-RPC frame:
  - Rejects connections from IPs not in `allowed_sources` (if configured)
  - Rejects methods not in `allowed_methods` with a JSON-RPC error
  - Rejects dataset/zvol/NFS operations targeting paths outside `allowed_dataset_prefix`
  - Passes everything else through to TrueNAS unchanged
- Proxies TrueNAS responses back transparently

## Requirements

- TrueNAS 25.10.3.1 or later
- A user with `sudo` access on TrueNAS (e.g. `truenas_admin`)
- Python 3 with `pip` on your **local machine** (used by `deploy.sh` to vendor dependencies)
- SSH key-based access to TrueNAS from your local machine

## Deployment

### 1. Create the dataset on TrueNAS

Create a ZFS dataset on your TrueNAS pool to hold the proxy files. This can be done through the TrueNAS UI (Datasets > Add Dataset) or via the shell:

```bash
zfs create tank/truenas-csi-api-guard
```

Set `proxy_dir` in `config.yaml` to the mountpoint of this dataset (e.g. `/mnt/tank/truenas-csi-api-guard`). The deploy user must have write access to it, either through ownership or sudo.

### 2. Configure

```bash
cp config.template.yaml config.yaml
```

Edit `config.yaml`:

| Field | Required | Description |
|---|---|---|
| `proxy_dir` | Yes | Path on the TrueNAS pool where files will be installed (e.g. `/mnt/tank/truenas-csi-api-guard`) |
| `listen_host` | No | IP or list of IPs to bind (default: `"0.0.0.0"`) |
| `listen_port` | Yes | Port the CSI driver will connect to (e.g. `8443`) |
| `allowed_sources` | No | List of client IPs allowed to connect — omit to allow all |
| `cert` | Yes | Path to TrueNAS TLS certificate (default path is stable across upgrades) |
| `key` | Yes | Path to TrueNAS TLS private key |
| `upstream` | Yes | TrueNAS WebSocket API endpoint — always `wss://localhost/api/current` |
| `allowed_dataset_prefix` | Yes | ZFS path prefix (or list of prefixes) the CSI driver is allowed to operate on (e.g. `tank/kubernetes`) |
| `allowed_methods` | Yes | List of JSON-RPC methods the CSI driver may call |

`allowed_dataset_prefix` must match your CSI StorageClass. If your StorageClass sets `pool: tank` and `datasetPath: kubernetes`, the prefix is `tank/kubernetes`. Multiple StorageClasses targeting different paths can be covered with a list:

```yaml
allowed_dataset_prefix:
  - "tank/k8s-nfs"
  - "tank/k8s-iscsi"
```

### 3. Deploy

```bash
bash deploy.sh <user@truenas-host>
```

Example:
```bash
bash deploy.sh truenas_admin@192.168.1.10
```

`deploy.sh` will:
- Vendor the `websockets` Python dependency locally (no pip on TrueNAS required)
- Copy `proxy.py`, `boot.sh`, `config.yaml`, and `lib/` to TrueNAS over SSH/SCP
- Create the install directory with `sudo`
- Register `boot.sh` as a Post-Init script so the service survives reboots
- Start the service immediately

`config.yaml` is always redeployed on each run, so local edits take effect immediately.

### 4. Point the CSI driver at the proxy

In your truenas-csi configuration, change the TrueNAS URL from:
```
wss://your-truenas-ip/api/current
```
to:
```
wss://your-truenas-ip:8443/api/current
```

The CSI driver's API key and authentication flow are unchanged.

### StorageClass example (NFS)

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: truenas-nfs
provisioner: csi.truenas.io
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
parameters:
  protocol: nfs
  pool: tank
  datasetPath: kubernetes   # results in tank/kubernetes/<pvc-name>
```

> **Note:** The parameter is `datasetPath` (path within the pool), not `datasetParentName`.
> `allowed_dataset_prefix` in `config.yaml` must be set to `pool/datasetPath` — `tank/kubernetes` in this example.

To restrict which Kubernetes nodes can mount NFS volumes, use the `nfs.hosts` and/or `nfs.networks` StorageClass parameters. These are passed directly to TrueNAS when creating the NFS share:

```yaml
parameters:
  nfs.hosts: "10.0.0.10,10.0.0.11"   # comma-separated IPs or hostnames
  nfs.networks: "10.0.0.0/24"         # or CIDR ranges
```

If omitted, TrueNAS defaults to exporting to all hosts (`*`).

## Verifying

```bash
# Check the service is running
ssh truenas_admin@<host> systemctl status truenas-csi-api-guard

# Watch the proxy logs
ssh truenas_admin@<host> journalctl -u truenas-csi-api-guard -f
```

Blocked calls appear as:
```
BLOCKED [('10.0.0.50', 12345)] pool.dataset.delete — method 'pool.dataset.delete' targets 'tank/other' which is outside the allowed prefixes ['tank/kubernetes']
```

Rejected connections appear as:
```
REJECTED connection from 10.0.0.99 — not in allowed_sources
```

## Upgrade behavior

After a TrueNAS OS upgrade:
- All files on the pool (`proxy_dir`) survive untouched
- `boot.sh` is registered as a Post-Init script and re-runs on next boot, regenerating the systemd unit with correct paths and restarting the service
- No manual intervention needed

To update the proxy itself, edit files locally and re-run `deploy.sh`.

## WireGuard

If `listen_host` is set to a WireGuard tunnel IP, the service is ordered after `wg-quick.target` in systemd, ensuring all WireGuard tunnels are up before the proxy attempts to bind.
