# Raising gravedecay on AWS EC2

Tested on a `t3.medium` (2 vCPU, 4 GB RAM) running Amazon Linux 2023. AL2023
is dnf-based but not Fedora, and its base AMI has a few gaps the `generic`
ritual doesn't expect on its own. `raise.sh --profile aws` handles the ones
that are fixable from inside the box; two things need action outside it.

## 1. Install

```sh
curl -fsSL https://raw.githubusercontent.com/projectmushroom/gravedecay/master/install.sh \
  | bash -s -- --profile aws
```

`install.sh` installs `git` itself if the base AMI doesn't have it — AL2023's
base image ships without it.

## 2. What the `aws` profile + AL2023 detection handle automatically

`raise.sh`'s `dnf` branch detects `ID=amzn` in `/etc/os-release` and fills
these gaps before the generic ritual continues:

- **Docker Compose** — there is no `docker-compose` dnf package on AL2023.
  raise.sh fetches the official Compose plugin binary into
  `/usr/libexec/docker/cli-plugins` instead.
- **Node.js version** — the default `nodejs` package is v18; T3 Code needs
  `^22.16 || ^23.11 || >=24.10`. raise.sh installs `nodejs22`/`nodejs22-npm`
  and runs `alternatives --set node`.
- **C++ toolchain** — `t3` depends on `node-pty`, a native addon; the base
  AMI has no compiler. raise.sh installs `gcc-c++` and `make`.
- **ttyd** — not packaged for any Fedora-family dnf repo, AL2023 included.
  raise.sh fetches the static release binary into `/usr/local/bin/ttyd`.
- **firewalld** — not installed by default. raise.sh enables it, sets the
  default zone to `drop`, and allows `ssh` + `tailscale0` + the primary NIC
  (`ens5` on most EC2 instance types). `grave doctor`'s firewall check runs
  through a scoped `sudo -n firewall-cmd`, since the doctor itself runs
  unprivileged.
- **`systemctl restart` hangs** — occasionally blocks for minutes on AL2023
  when raise.sh runs from certain remote/agent environments. raise.sh uses
  `systemctl restart --no-block` and polls the service's HTTP endpoint for
  readiness instead of waiting on systemd.

The `aws` profile itself (`profiles/aws.sh`) is otherwise the same
always-on shape as `generic` — it masks suspend/hibernate targets, which
mostly matters for consistency since a cloud instance doesn't suspend on
its own anyway — plus it pins `CHECK_FIREWALL=1` explicitly rather than
relying on the config default, since a cloud box's public IP makes the
firewalld default-deny load-bearing in a way it isn't on a home LAN box.

## 3. What you still have to do yourself

Neither of these can be scripted from inside the instance:

1. **Enable Tailscale Serve for your tailnet, once.** The first
   `tailscale serve` attempt after login will print:

   ```
   Serve is not enabled on your tailnet.
   To enable, visit: https://login.tailscale.com/f/serve?node=...
   ```

   Visit that URL, then rerun `raise.sh` (or just `install.sh` again — both
   are idempotent) to create the three origins:

   | Path | Backend |
   |---|---|
   | `/` | `http://127.0.0.1:4711` (T3 Code) |
   | `/grave` | `http://127.0.0.1:4712` (gravedecay dashboard) |
   | `/term` | `http://127.0.0.1:4713` (web terminal) |

2. **Lock down the EC2 security group.** gravedecay's own firewalld rules
   only gate traffic once it reaches the instance's NIC — the security group
   decides what reaches the NIC at all, and it lives outside the box entirely.
   Allow inbound `22` (ssh) only from IP ranges you actually use; nothing else
   needs to be open, since every gravedecay service is reached over the
   tailnet rather than a public port.

## 4. Verify

```sh
grave doctor          # must report all checks passed
curl -sf http://127.0.0.1:4711/            # T3 Code
curl -sf http://127.0.0.1:4712/healthz     # gravedecay dashboard
curl -sf http://127.0.0.1:4713/            # web terminal
```

From another tailnet device, `https://<box>.ts.net/grave/` should load the
dashboard, and `docker ps` should show `core-postgres` and `core-redis`
healthy on `127.0.0.1` only.
