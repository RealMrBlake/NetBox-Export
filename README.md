# NetBox-Export

Export a scoped tenant or site area as a portable ZIP archive and import it into
a second NetBox instance.

| | |
|---|---|
| **Plugin name** | `netbox_export` |
| **Package** | `netbox-export` |
| **NetBox** | `4.6.0` – `4.7.x` |
| **Python** | `>=3.12` |
| **Repository** | <https://git.mrblake.cc/MrBlake/NetBox-Export> |

## Features

Supported starting points:

- tenant group including sub-groups and tenants
- single tenant
- region including sub-regions and sites
- single site
- location including sub-locations

The export follows ownership relations to DCIM, IPAM, circuit, virtualisation,
VPN, wireless, contact, tag and image data. Required master data is included as
dependencies. Primary keys of the source instance are never used directly as
target keys.

## Compatibility

- NetBox `4.6.0` – `4.7.x`
- Python `>=3.12`
- Source and target must run the same NetBox version and the same plugins,
  plugin versions and migrations.

## Installation

The plugin must be installed on **both** NetBox instances. All paths assume a
standard installation under `/opt/netbox`.

### 1. Install the package

```bash
/opt/netbox/venv/bin/pip install --upgrade --force-reinstall \
  "git+https://git.mrblake.cc/MrBlake/NetBox-Export.git@main"
```

For reproducible production installs, replace `main` with a release tag or a
full commit ID.

### 2. Add the plugin to `local_requirements.txt`

This makes `upgrade.sh` reinstall the plugin automatically on every NetBox upgrade:

```bash
grep -qxF "git+https://git.mrblake.cc/MrBlake/NetBox-Export.git@main" /opt/netbox/local_requirements.txt \
  || echo "git+https://git.mrblake.cc/MrBlake/NetBox-Export.git@main" | sudo tee -a /opt/netbox/local_requirements.txt
```

If the repository is private, the NetBox server needs a read-only deploy token
or an SSH key. Do not store credentials in `local_requirements.txt`.

### 3. Enable the plugin

In `/opt/netbox/netbox/netbox/configuration.py`:

```python
PLUGINS = [
    "netbox_export",
]

PLUGINS_CONFIG = {
    "netbox_export": {
        "max_objects": 50000,
        "max_archive_size_mb": 250,
        "query_batch_size": 500,
        # Set identically on both instances to sign archives.
        "archive_signing_key": "a-long-random-secret-string",
    },
}
```

If other plugins are already configured, add `netbox_export` to the existing
list and dictionary instead of replacing them.

### 4. Apply migrations, collect static files, restart

```bash
cd /opt/netbox/netbox
/opt/netbox/venv/bin/python manage.py migrate netbox_export
/opt/netbox/venv/bin/python manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

If `ModuleNotFoundError: No module named 'netbox_export'` appears, the package
was not installed into `/opt/netbox/venv`. Check with:

```bash
/opt/netbox/venv/bin/python -c "import netbox_export; print(netbox_export.__file__)"
```

For Docker installations, add the package to your NetBox image, rebuild the
container with the plugin enabled and run the migration.

## Update

```bash
/opt/netbox/venv/bin/pip install --upgrade --force-reinstall \
  "git+https://git.mrblake.cc/MrBlake/NetBox-Export.git@main"

cd /opt/netbox/netbox
/opt/netbox/venv/bin/python manage.py migrate netbox_export
/opt/netbox/venv/bin/python manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

`--force-reinstall` makes pip pick up branch changes even if the package
version has not been bumped. Update source and target instance together.

When NetBox itself is upgraded, `upgrade.sh` reinstalls the plugin from
`local_requirements.txt` and runs migrations and `collectstatic`:

```bash
sudo /opt/netbox/upgrade.sh
sudo systemctl restart netbox netbox-rq
```

## Uninstall

1. Remove `"netbox_export"` from `PLUGINS` and `PLUGINS_CONFIG`.
2. Remove the line from `/opt/netbox/local_requirements.txt`.
3. Uninstall the package and restart NetBox:

```bash
/opt/netbox/venv/bin/pip uninstall netbox-export
sudo systemctl restart netbox netbox-rq
```

## Configuration

| Key | Default | Description |
|---|---|---|
| `max_objects` | `50000` | Upper limit of objects per export (runtime and memory) |
| `max_archive_size_mb` | `250` | Maximum archive size |
| `query_batch_size` | `500` | Size of batched database queries; `250`–`1000` is reasonable |
| `archive_signing_key` | – | Shared secret to sign archives; must be identical on both instances |

## Usage

The UI is located under **Plugins > NetBox-Export > Export / Import** and, for
security reasons, is only visible to superusers.

1. On instance A, choose the type and the specific object and export the ZIP.
2. On instance B, first process the ZIP with **Dry run only**.
3. After a successful dry run, disable **Dry run only**, confirm the writing
   import and upload the archive again.

The import is atomic: on error, all database changes are rolled back. The
conflict strategy **Update** first uses the stored mapping of source instance,
model and source ID; on a first import, existing objects are matched by their
unique natural keys.

**Force mode** skips objects whose import fails (database errors, unresolvable
references, failing plugin validation) instead of rolling back the whole
import. Each failing object is rolled back individually via a savepoint and
listed as a warning in the import report; references to it are omitted. The
conflict strategy **Abort import** still aborts on existing objects.

### Behaviour and limits

- User accounts and permissions are not exported. Missing or ambiguous user and
  group references are omitted and reported as warnings; if a new record
  strictly requires such a reference, only that record is skipped.
- The import creates and updates objects. Target objects not contained in the
  archive are intentionally **not** deleted.
- Missing image files are noted in the archive but cannot be reconstructed.
- Large exports are processed synchronously; `max_objects` limits runtime and memory.

### Plugin compatibility

- Tenant-related models and files of NetBox-SLM, Netbox-Documentation and
  NetBox-VM-Import are included. Private or temporary plugin models are not exported.
- The mandatory tenant assignment of Netbox-Utilities is respected: an object is
  only saved after its tenant has been imported. If no tenant can be resolved,
  an existing tenant `Auto-Import` is used or created, and the import report
  says so.
- Relations checked by plugin database constraints (e.g. the platform of
  NetBox-SLM software installations) are resolved before the first save.
- Unique optional relations such as primary IPs of devices and VMs are assigned
  in a second phase; stale target assignments are released atomically and logged.
- Stored import mappings are validated against the current natural key on
  repeated imports, correcting the mapping instead of creating duplicates.
- Automatic component replication is disabled for new modules; ports,
  interfaces and bays are created solely from archive records.
- Devices are placed in racks in a separate final phase so position swaps work
  without temporary double occupancy. For foreign occupants, **Update** releases
  the target device with a warning, **Skip** leaves the imported device without
  a position, and **Abort import** reports the rack conflict. Multi-U and
  full-depth occupancy are considered.
- Front/rear port mappings of patch panels are exported as separate records and
  synced on **Update**; cable paths are recalculated after import. This requires
  an archive created with version `0.3.12` or newer.
- PostgreSQL range fields (e.g. VLAN ID ranges of VLAN groups) are stored typed;
  text values from older versions are still recognised.
- Image width and height are read from the archived file. Images above NetBox's
  25-megapixel limit are scaled down to at most 20 megapixels with a warning; a
  hard source limit of 100 megapixels applies.
- The optional disk management of Netbox-Utilities is fully supported when the
  plugin is installed: disks, RAID groups and slot layouts of devices as well as
  disk bay templates of the required device types. Replaced disks are not
  confused with active disks in the same slot. If Netbox-Utilities is missing on
  the target, its records are skipped with a warning instead of aborting the import.
- Encrypted credentials of NetBox-VM-Import are only usable with an identical
  Django `SECRET_KEY`; otherwise set the password again on the target.

## Development and tests

```bash
python -m pytest
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
