# Setup: login, keys, machines, SSH

Get from "the user has a Tule account" to a shell on the project's machine.

## Install and sign in, once per computer

Call the connector's `connect_info` (any project) and run its `setup` command:

```sh
curl -fsSL https://<tule host>/cli.sh | sh -s -- tulel_…   # installs the tule CLI and signs it in
```

The code is approved in advance for the user who added the connector: nothing for the user to do. It works once, for an hour; ask `connect_info` again if it has expired. Skip this if `tule ls` already works. The login is stored in `~/.config/tule/config.json`. Without the connector, `tule login --url https://<tule host>` shows the user a link and code to approve.

## Machines

```sh
tule up -p bracket                      # start the project's machine (cpu-1), wait until ready
tule up -p bracket --machine cpu-4      # resize: cpu-1 (1 vCPU, 6 GiB), cpu-2 (2, 8 GiB), cpu-4 (4, 12 GiB)
tule status -p bracket                  # state, machine, timings, outputs
tule down -p bracket                    # stop it (it also stops after 30 idle minutes)
```

The project name comes from `-p`, else `$TULE_PROJECT`, else the last one used. Resizing saves `/workspace`, stops the old machine and starts the new one; nothing in `/workspace` is lost.

**GPUs** run on RunPod and are paid from the user's RunPod balance: the user connects a RunPod API key in Tule's settings, then resize to `gpu-<type>` (connector: `machines.resize`). Same image plus CUDA 12 and CuPy, same `/workspace`. Alternatively, the user's own RunPod connector can create a pod from `ghcr.io/janos95/tule-runtime:cuda` with the environment that `tule join -p bracket` prints; the pod joins the project.

## SSH

```sh
tule ssh -p bracket                     # a shell (starts the machine if needed)
tule ssh -p bracket -- python main.py   # one command
tule ssh -p bracket --config            # then plain `ssh tule-bracket`, `scp`, `rsync`, editors' remote SSH
```

The CLI makes its own key (`~/.config/tule/id_ed25519`) and adds it to the project on first use; `tule keys add ~/.ssh/id_ed25519.pub -p bracket` adds another. The machine's host key is pinned in `~/.config/tule/known_hosts`. Everything goes through one HTTPS tunnel: no ports to open, and it works wherever the Tule host is reachable.

## Snapshots

`/workspace` is saved a few seconds after changes stop, at least every minute while it changes, and when the machine stops.

```sh
tule snapshot            # save now (on the machine or from your computer with -p)
tule snapshots -p bracket
tule restore <id> -p bracket            # go back; the current state is saved first
```

## The viewer

`tule open -p bracket` opens it (`--print` prints the link to hand to the user). It shows the outputs live; the user can pick faces and draw on the model (see `annotations`).
