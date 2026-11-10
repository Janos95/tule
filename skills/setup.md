# Setup: login, keys, machines, SSH

Get from "the user has a Tule account" to a shell on a machine with the project.

## Install and sign in, once per computer

Call the connector's `connect_info` (any project) and run its `setup` command:

```sh
curl -fsSL https://<tule host>/cli.sh | sh -s -- tulel_…   # installs the tule CLI and signs it in
```

The code is approved in advance for the user who added the connector: nothing for the user to do. It works once, for an hour; ask `connect_info` again if it has expired. Skip this if `tule ls` already works. The login is stored in `~/.config/tule/config.json`. Without the connector, `tule login --url https://<tule host>` shows the user a link and code to approve.

## Machines

```sh
tule up -p bracket                      # load the project on Tule cloud (cpu-1), wait until ready
tule up -p bracket --machine cpu-4      # resize it: cpu-1 (1 vCPU, 6 GiB), cpu-2 (2, 8 GiB), cpu-4 (4, 12 GiB)
tule up -p bracket --machine gpu-rtx4090   # also load it on a GPU; the cloud machine keeps running
tule status -p bracket                  # every machine: state, unsaved work, timings
tule down -p bracket [--machine …]      # stop them, or one (each stops after 30 idle minutes)
```

The project name comes from `-p`, else `$TULE_PROJECT`, else the last one used.

**Several machines:** a project can run on several machines at once (Tule cloud, a GPU, one of the user's computers by name), each with its own copy of `/workspace`. A machine that ran the project before gets its own copy back; a new one starts from the latest save. When several run, `ssh`, `build`, `save` and `snapshot` need `--machine` (`cloud`, `gpu-<type>`, a computer's name; the ids are in `tule status`); `down` without it stops them all.

**GPUs** run on RunPod and are paid from the user's RunPod balance: the user connects a RunPod API key in Tule's settings, then `--machine gpu-<type>` (connector: `machine_start`). Same image plus CUDA 12 and CuPy. Alternatively, the user's own RunPod connector can create a pod from `ghcr.io/janos95/tule-runtime:cuda` with the environment that `tule join -p bracket` prints; the pod joins the project.

## SSH

```sh
tule ssh -p bracket                     # a shell (starts the machine if needed)
tule ssh -p bracket -- tule build      # one command (or just: tule build -p bracket)
tule ssh -p bracket --machine gpu-rtx4090   # one of several machines
tule ssh -p bracket --config            # then plain `ssh tule-bracket` (`tule-bracket.<machine>` when several run), `scp`, `rsync`
```

The CLI makes its own key (`~/.config/tule/id_ed25519`) and adds it to the account on first use; `tule keys add ~/.ssh/id_ed25519.pub` adds another (keys work on every project). The machine's host key is pinned in `~/.config/tule/known_hosts`. Everything goes through one HTTPS tunnel: no ports to open, and it works wherever the Tule host is reachable.

## Saving

Two kinds, both of `/workspace`:

- **Autosave** keeps each machine's copy safe: a few seconds after changes stop, at least every minute while it changes, and when the machine stops. It never merges.
- **Save** is git: `tule save -m "what changed"` (on the machine, or `-p` from your computer) commits everything, merges what other machines saved, and pushes it as the project's latest save. Machines loaded later start from it. On a merge conflict nothing is saved: resolve the listed files, then `tule save` again.

```sh
tule snapshot                     # autosave now
tule snapshots -p bracket         # autosaves, newest first
tule restore <id> -p bracket      # go back; the current state is autosaved first
tule forget -p bracket --machine rechenknecht   # drop a stopped machine's unsaved copy (ask the user)
```

## The viewer

`tule open -p bracket` opens it (`--print` prints the link to hand to the user). It shows the outputs live; the user can pick faces and draw on the model (see `annotations`).
