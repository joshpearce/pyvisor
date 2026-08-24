# NixOS Deployment Draft

This is the Stage 9 deployment shape for `lildeet`. Pyvisor is a rootless
Podman/Quadlet workload. The `tsnsrv` sidecar is **not** a Pyvisor component or
artifact: it belongs solely in the NAS host configuration, where the draft
below shows how it proxies Pyvisor's private HTTP listener. The app image embeds
the direct `runsc` binary and the pinned sandbox rootfs; Pyvisor invokes
`runsc` directly for sandbox jobs, so the outer container must **not** set its
Podman runtime to `runsc`.

The draft is intentionally not applied to the NAS configuration repository.
Review the changes below in `/home/josh/code/nixconfigs/nas` before applying
them. In particular, the first deployment must run the opt-in `runsc`
integration suite from the real Quadlet service before exposing the Tailscale
endpoint.

## Pyvisor build artifacts

The flake exports two deployment artifacts:

```sh
nix build .#pyvisor
nix build .#pyvisor-container
```

`pyvisor-container` is a Docker archive tagged `localhost/pyvisor:0.1.0`. It
contains the service, `runsc`, and the closure-complete sandbox rootfs. The
image listens on `0.0.0.0:8000` only inside its private Quadlet network. It
deliberately has no configured sandbox image identity: deployment must set
`PYVISOR_SANDBOX_IMAGE_IDENTITY` to the NAR identity of the rootfs it was built
with. A missing identity makes the `runsc` backend reject job execution.

The app runs as UID 0 *inside* the rootless Podman user namespace, which maps
to the dedicated unprivileged host account. The runtime explicitly verifies
that mapping and still rejects real host root.

The first deployment uses `systrap` and deliberately does not mount `/dev/kvm`.
KVM remains an optional future runtime mode that needs a separate security and
device-access review.

## NAS changes for review

The NAS host's workload contract requires five coordinated changes.

1. Add a path input to `/home/josh/code/nixconfigs/flake.nix`:

   ```nix
   pyvisor.url = "path:/home/josh/code/pyvisor";
   pyvisor.inputs.nixpkgs.follows = "nixpkgs";
   ```

2. Add `./pyvisor.nix` to `nas/default.nix`.

3. Add a `pyvisor` system group and rootless Podman user in `nas/users.nix`:

   ```nix
   pyvisor = { gid = 1219; };

   pyvisor = {
     uid = 1219;
     group = "pyvisor";
     description = "Pyvisor rootless Podman service account";
     isSystemUser = true;
     home = "/workloads/pyvisor";
     createHome = true;
     subUidRanges = [{ startUid = 363144; count = 65536; }];
     subGidRanges = [{ startGid = 363144; count = 65536; }];
     linger = true;
   };
   ```

   This consumes the next range after `pyvisor-test`
   (`297608-363143`) and must remain documented in `nas/CLAUDE.md`.

4. Create the ZFS dataset *before* rebuilding NixOS, then register it in the
   manifest and off-site backup map:

   ```sh
   sudo zfs create \
     -o mountpoint=/workloads/pyvisor \
     -o com.sun:auto-snapshot=true \
     lildeet/nasdata/workloads/pyvisor
   ```

   Add this manifest entry to `nas/zfs.nix`:

   ```nix
   "lildeet/nasdata/workloads/pyvisor" = {
     mountpoint = "/workloads/pyvisor";
     "com.sun:auto-snapshot" = "true";
   };
   ```

   Recommended backup entry in `nas/zfs-backup.nix`:

   ```nix
   "lildeet/nasdata/workloads/pyvisor" = "lildeet/pyvisor";
   ```

5. Add `nas/pyvisor.nix` using the Quadlet and image-load pattern below. It
   reuses the existing workload Tailscale OAuth credentials, so no new agenix
   secret is required.

## `nas/pyvisor.nix` draft

```nix
{ pkgs, pyvisor, ... }:
let
  pyvisorUid = 1219;
  pyvisorImage = pyvisor.packages.x86_64-linux.pyvisor-container;
  tsOauthScripts = pkgs.callPackage ../packages/tailscale-oauth/tailscale-oauth.nix { };
  authKeyPath = "/workloads/pyvisor/.tailscale-key";
  quadletDir = "containers/systemd/users/${toString pyvisorUid}";
in
{
  environment.etc."${quadletDir}/pyvisor-net.network".text = ''
    [Network]
    Subnet=10.89.13.0/24
    Gateway=10.89.13.1
  '';

  environment.etc."${quadletDir}/pyvisor.container".text = ''
    [Unit]
    Description=Pyvisor direct-runsc sandbox service

    [Container]
    Image=localhost/pyvisor:0.1.0
    Network=pyvisor-net.network
    HostName=pyvisor
    Volume=/workloads/pyvisor/sessions:/var/lib/pyvisor/sessions
    Environment=PYVISOR_SANDBOX_IMAGE_IDENTITY=pyvisor-sandbox-python-3.13@sha256:REPLACE_WITH_NAR_HASH
    HealthCmd=${pkgs.curl}/bin/curl --fail --silent http://127.0.0.1:8000/healthz
    HealthInterval=30s
    HealthTimeout=5s
    HealthRetries=3

    [Service]
    Restart=always
    RestartSec=5s
    TimeoutStartSec=120

    [Install]
    WantedBy=default.target
  '';

  environment.etc."${quadletDir}/tsnsrv-pyvisor.container".text = ''
    [Unit]
    Description=tsnsrv Tailscale proxy for Pyvisor
    After=pyvisor.service

    [Container]
    Image=${(import ./images.nix).tsnsrv}
    Network=pyvisor-net.network
    Volume=/workloads/pyvisor/.tsnsrv-pyvisor:/state
    Volume=${authKeyPath}:/authkey:ro
    Exec=-stateDir /state -authkeyPath /authkey -name pyvisor http://pyvisor:8000/

    [Service]
    Restart=always
    TimeoutStartSec=120

    [Install]
    WantedBy=default.target
  '';

  systemd.tmpfiles.rules = [
    "d /workloads/pyvisor 0700 pyvisor pyvisor -"
    "d /workloads/pyvisor/sessions 0700 pyvisor pyvisor -"
    "d /workloads/pyvisor/.tsnsrv-pyvisor 0700 pyvisor pyvisor -"
  ];

  systemd.services.pyvisor-image-load = {
    description = "Load the pinned Pyvisor image into rootless Podman storage";
    wantedBy = [ "multi-user.target" ];
    before = [ "user@${toString pyvisorUid}.service" ];
    restartTriggers = [ pyvisorImage ];
    path = [ pkgs.podman ];
    serviceConfig = {
      Type = "oneshot";
      User = "pyvisor";
      Group = "pyvisor";
      RuntimeDirectory = "pyvisor-podman";
      RuntimeDirectoryMode = "0700";
    };
    environment = {
      HOME = "/workloads/pyvisor";
      XDG_RUNTIME_DIR = "/run/pyvisor-podman";
    };
    script = ''
      exec podman load --input ${pyvisorImage}
    '';
  };

  systemd.services.pyvisor-ts-auth = {
    description = "Generate Tailscale auth key for Pyvisor";
    wantedBy = [ "multi-user.target" ];
    before = [ "user@${toString pyvisorUid}.service" ];
    serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
    script = ''
      set -euo pipefail
      ${tsOauthScripts}/bin/ts-get-auth-key \
        "$(cat /run/agenix/ts-oauth-workloads-id)" \
        "$(cat /run/agenix/ts-oauth-workloads-secret)" \
        "tag:wl-pyvisor" > ${authKeyPath}
      chown pyvisor:pyvisor ${authKeyPath}
      chmod 0400 ${authKeyPath}
    '';
  };
}
```

## Required review and validation

Review the proposed `tag:wl-pyvisor` Tailscale ACL/tag authorization before
deploying. Build the image and obtain the rootfs NAR identity before replacing
the placeholder:

```sh
cd /home/josh/code/pyvisor
rootfs="$(nix build --no-link --print-out-paths .#sandbox-rootfs)"
nix path-info --json --json-format 1 "$rootfs"
```

After `nixos-rebuild switch`, first exercise a real sandbox command through the
private Quadlet service, then inspect the app container logs. Do not expose the
Tailscale sidecar until that command completes and the mount/network isolation
checks pass. The existing `pyvisor-test` systemd service remains a useful
host-level control, but it does not validate nesting under Podman; add a
same-image integration-test container before treating the full opt-in suite as
deployment proof.
