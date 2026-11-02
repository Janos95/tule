# Tule: start here

Tule gives you, the agent, engineering computers: Linux machines with OpenCascade, the `tule` Python modeling API and engineering libraries installed, a project `/workspace` on each (autosaved continuously; `tule save` merges and saves it in git), and a web viewer where the user watches `/workspace/outputs/` live.

## Two ways in

- **The Tule connector (MCP)** manages resources: projects (list, create, rename, delete), machines (start several per project: Tule cloud, a GPU, the user's computers; stop, status), saves, autosaves, SSH and API keys, usage. It never does the work: no shell, no files.
- **The `tule` CLI and SSH** do the work: run Python on the machine, read and write files, render and inspect results. Use them whenever you can run commands.

Typical session: find or create the project with the connector (or `tule up -p <name>`), then `tule ssh -p <name>` and work in `/workspace`. In a chat app without a shell, the connector alone can manage projects and link the viewer, but cannot model.

## On the machine

- Work on the machine, not on your own computer: files written locally are not in the project and the user never sees them. Write a file with `tule ssh -p <name> -- 'cat > src/bracket.py' <<'EOF'` … `EOF`, or edit it on the machine.
- `/workspace` is the project and the only thing that persists (a git repository; `tule save` saves it). Everything else is rebuilt from the image at every boot.
- `src/<name>.py` makes `outputs/<name>/`; `tule build` runs the scripts whose inputs changed (nothing runs by itself). The viewer shows outputs as soon as they appear.
- Look at your own work: `tule screenshot -o /tmp/view.png --labels`, then read the PNG. `tule inspect <file>` lists faces with ids, types, areas and tags.
- You are root: `pip install`, background jobs and any tool are fine. Extra packages for every boot go in `tule.toml`.

## Topics

Read one with `tule docs <topic>` (on the machine) or the connector's `guide(topic)`.

| topic | read it when |
|---|---|
| `setup` | logging in, keys, starting machines, GPUs, SSH |
| `loop` | the working loop: edit, build, look, iterate; project layout; saving and merging; long jobs |
| `modeling` | writing geometry with `tule` (values, operators, selections, tags) |
| `outputs` | what the viewer shows and how to write it |
| `occt` | an operation failed: fillets, Booleans, shells, tolerances |
| `annotations` | the user drew on the model in the viewer |
| `topopt` | topology optimization from loads and fixed faces |
| `golden-paths` | tested end-to-end recipes to copy from |
