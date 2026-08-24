# Deployment Notes — NAS (lildeet)

As-deployed reference for the running instance, first deployed 2026-08-24.

`docs/design/nixos-deployment.md` is the pre-deployment *draft*; where the two
disagree, this file describes what actually runs. In particular the draft's
`PYVISOR_SANDBOX_IMAGE_IDENTITY=...@sha256:REPLACE_WITH_NAR_HASH` was shipped
verbatim at first and has since been replaced by a derived value (see below).

## Reaching it

`https://pyvisor.dunker-bramble.ts.net` — tailnet only, over a tsnsrv sidecar.

It is **deliberately never public**: no Tailscale Funnel attribute, and the
tailnet policy carries an explicit deny assertion for non-admin identities,
because this service executes caller-supplied code. Do not add a funnel entry.

Access is granted to the `joshpearce@github` identity. Two practical
consequences:

- **You cannot test it from the NAS itself.** `lildeet` is a tagged node
  (`tag:nas`) and so carries no user identity. From there the pyvisor node is
  ACL-filtered out of `tailscale status`, MagicDNS will not resolve the name,
  and a direct connection to its tailnet IP times out. `curl` reports all three
  as `000`. Test from a personal device.
- The same is true of every other workload on that host. It is the design
  working, not a fault.

## How your code reaches production

The NixOS flake pins this repo as a **git** input, currently:

```
refs/heads/stage6-rootfs-integration @ 281900ade22e9eb3b0451e5d3f1d32ebd77bee06
```

- **Uncommitted work does not deploy.** `git+file://` reads committed state, not
  your working tree. Commit before deploying.
- Deploying is two steps in the `nixconfigs` repo:
  `nix flake lock --update-input pyvisor`, then
  `sudo nixos-rebuild switch --flake .#nas`.
- Budget ~2 minutes for the image load alone — it is a multi-GB Nix closure.
- The image is built by `dockerTools.buildLayeredImage`, `podman load`ed by a
  system unit, and applied to the running container by `pyvisor-quadlet-sync`.
  Because the Quadlet unit pins a fixed tag, that sync unit is what actually
  swaps the container; without it a rebuilt image would never take effect.

## Contracts that break the deploy silently

These are hardcoded in `nas/pyvisor.nix` in the `nixconfigs` repo. Changing them
here without a matching change there fails quietly rather than loudly.

| What | Value | If you change it |
|---|---|---|
| Image name and tag | `localhost/pyvisor:0.1.0` | **Sharpest edge.** Bump `tag` to `0.2.0` and the load succeeds, but the Quadlet still references `0.1.0` — so the *old* image keeps running and everything looks green. Coordinate every version bump. |
| Listen port | `8000` | Healthcheck and the tsnsrv backend URL both break. |
| Health endpoint | `GET /healthz` | Container is marked unhealthy forever. |
| `pkgs.curl` in image `contents` | required | The healthcheck runs *inside* the container as `/bin/curl --fail --silent http://127.0.0.1:8000/healthz`. |
| Flake output names | `pyvisor-container`, `sandbox-rootfs` | Both are referenced by name from the NixOS module. |

The healthcheck deliberately uses the image's own `/bin/curl` rather than a host
store path. A host path only resolves inside the container by coincidence of a
shared nixpkgs, and drifts silently the moment that stops being true.

## The runtime you are actually running in

Rootless Podman, host user `pyvisor` (UID 1219), with `runsc` sandboxes nested
*inside* that container — a user namespace within a user namespace.

### Sandbox image identity

`PYVISOR_SANDBOX_IMAGE_IDENTITY` is now derived at Nix evaluation time from the
same `sandbox-rootfs` derivation the image bakes in:

```
pyvisor-sandbox-python-3.13@1lmzlrmxg18p6q4lp72msjl4m94s73kh-pyvisor-sandbox-rootfs-3.13.14
```

Nix's hashing guarantees it cannot drift from the rootfs that actually ran the
code. It previously shipped as the literal `REPLACE_WITH_NAR_HASH` placeholder
from the design draft.

**Suggested change here:** `config.py` currently rejects only an *empty*
identity, so a placeholder or stale value is accepted silently and the
`io.pyvisor.sandbox_image` annotation becomes meaningless provenance. Rejecting
placeholder-shaped values would turn a silent misconfiguration into a startup
error, which matters for anyone deploying this somewhere else.

### Storage: what is durable and what is not

| Path | Backing | Lifetime |
|---|---|---|
| `PYVISOR_SESSIONS_ROOT` (`/var/lib/pyvisor/sessions`) | Its own ZFS dataset, snapshotted and replicated off-site nightly | Durable |
| `PYVISOR_RUNTIME_ROOT` (`/var/lib/pyvisor/runtime`) | Not mounted — container writable layer | Dies with the container |

This split follows `docs/design/session-layout.md`, and the NAS backup policy now
depends on it. **If durable state ever moves out of `sessions/`, off-site backup
silently stops covering it.** The surrounding `$HOME` — the podman image store,
tsnsrv node state, the auth key — is deliberately excluded from backup as
re-creatable, so nothing outside `sessions/` is protected.

### Resource bounds are on the outer unit

`MemoryMax=16G`, `CPUQuota=400%` (4 of 16 cores), `TasksMax=2048`, applied to the
container's systemd service.

Rootless runsc uses a single-UID user namespace and creates no per-sandbox
cgroups, so this is the **only** kernel-enforced limit in the system. A runaway
sandbox therefore takes down the whole service, not just its own job — every
live session dies together. Any per-job budget enforced in-process is the only
thing standing between one bad submission and that outcome.

## Fixed: the executor omitted `--rootless`

**Resolved in `743ef2e`, verified on the NAS 2026-08-24.** Jobs now run:

```json
{"status":"completed","command":"python --version","exit_code":0,
 "stdout":{"text":"Python 3.13.14\n"}}
```

That interpreter version is the sandbox rootfs's own
(`pyvisor-sandbox-rootfs-3.13.14`), confirming the job executed inside the
gVisor sandbox built from the pinned derivation rather than in the service
container.

### What the bug was

Every submission terminated immediately with `status: "runtime_error"`,
`reason: "executor_start_failed"`, a null exit code and empty output streams.

The environment was never at fault. gVisor starts fine nested inside the
rootless container; the flag set in `runsc_executor.py` was wrong. Verified
directly in the running container:

```
a) --ignore-cgroups --platform=systrap --network=none   (the old launch flags)
   -> creating container: cannot create sandbox: cannot create sandbox process:
      can't run sandbox process in minimal chroot since we don't have CAP_SYS_ADMIN

b) --rootless --ignore-cgroups --platform=systrap --network=none
   -> hello
```

Inside the container the process is UID 0 of its own user namespace, so runsc
concluded it was real root and took the minimal-chroot sandbox path, which
requires `CAP_SYS_ADMIN`. A nested user namespace cannot grant that.
`--ignore-cgroups` does not help — it addresses a different failure (a
read-only `/sys/fs/cgroup`, which is what surfaces when neither flag is passed).
Both flags are needed.

`--rootless` is now passed on the warm-runtime launch and mirrored onto
`runsc exec`, so the global mode flags describe the same sandbox the exec joins.
Both paths are exercised by a completed job: pyvisor boots the runtime with
`run --detach` and runs each command through `exec`.

### Resolved: launch diagnostics reach structured logs

`_runtime()` writes `runsc` stderr to `runsc-launch.stderr` while starting and
folds it into `RuntimeUnavailableError`. `job_start_failed` logs that exception
as a structured `error` field, capped at 4 KiB, so operators can diagnose a
launch failure from the service logs without retaining a bundle or raw runtime
output in durable state.

Bundles remain disposable. On a failed `runsc run`, Pyvisor attempts
`runsc delete --force` against the generated container id before deleting the
bundle; a cleanup failure is logged separately and never obscures the launch
diagnostic. `reason: "executor_start_failed"` remains the stable durable/API
classification, rather than exposing implementation output to callers.

## Verified working

- Nested `runsc`, contrary to the original deployment review's concern that it
  would need `PodmanArgs=`, seccomp/AppArmor relaxation, or cgroup delegation
  from the Quadlet unit. It needs none of those, and nothing on the NixOS side
  had to change for sandboxes to run.
- End-to-end job execution through both the `run --detach` and `exec` paths.
- The sandbox provenance annotation: durable job records carry
  `"sandbox_image": "pyvisor-sandbox-python-3.13@1lmzlrmxg18p6q4lp72msjl4m94s73kh-pyvisor-sandbox-rootfs-3.13.14"`,
  the real store path of the rootfs that ran the code.

## Debugging

The obvious commands do not work, because everything runs in a rootless user
session under a different account.

```sh
# Application logs
journalctl _SYSTEMD_USER_UNIT=pyvisor.service -f

# Everything for that user: podman, tsnsrv, and the app together
journalctl _UID=1219 -f

# Podman needs BOTH the user and -H, and a world-readable cwd. Without -H,
# HOME stays /home/josh, podman reads an empty store, reports no containers,
# and the service appears stopped when it is running fine. Without the cd,
# sudo keeps the current directory and the pyvisor account cannot traverse
# /home/josh (mode 700), so it fails before podman even starts.
cd /tmp && sudo -u pyvisor -H XDG_RUNTIME_DIR=/run/user/1219 podman ps
```

Two failure modes that look like application faults but are not:

- **`502` from the tailnet endpoint** with `connect tcp <ip>:8000: connection
  was refused` in the tsnsrv logs, where `<ip>` is tsnsrv's own tailnet address.
  tsnet answers DNS for its own node name inside the sidecar container, so the
  sidecar's `-name` must never equal the backend container's `HostName`. The
  backend is `pyvisor-app` for exactly this reason. The app looks healthy
  throughout, because its healthcheck hits `127.0.0.1` and bypasses DNS.
- **`invalid key: API key ... not valid`** from tsnsrv at startup. The tailnet
  auth key has a 20-minute lifetime and the unit that mints it does not re-run
  on rebuild, so a stalled deploy ages it out. Only affects *initial*
  registration; recover with
  `sudo systemctl restart pyvisor-ts-auth.service pyvisor-quadlet-sync.service`.

## Operator documentation

In the `nixconfigs` repo:

- `docs/design-plans/2026-08-24-pyvisor.md` — design and acceptance criteria
- `docs/test-plans/2026-08-24-pyvisor.md` — verification steps; Phase 4 is the
  runsc check above
- `nas/pyvisor.nix` — the module itself
