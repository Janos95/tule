# The loop: edit, run, look, iterate

How to work on a Tule machine so that the user can follow along and nothing is lost.

## One iteration

1. **Edit** a script in `src/` (start from `src/part.py`; keep parameters at the top).
2. **Build**: `tule build` runs the scripts whose inputs changed (`tule build part` runs one). Fix `TuleError`s from their message and hint (`tule docs occt`).
3. **Write** results with `tule.outputs`, named like the script (`src/part.py` → `outputs.part("part", …)`; other files into `outputs.folder()`); the viewer updates live.
4. **Look**: `tule screenshot -o view.png --labels`, then read the PNG. Check other sides with `--view top|front|right|bottom`. Do not claim a feature is there until you have seen it.
5. **Check numbers**: `tule inspect outputs/<name>/<file>.step` (size, volume, faces, tags), or print measurements in the script (`part.size`, `part.volume`, `part.is_valid`).
6. **Iterate** until it matches the request; then tell the user what to look at in the viewer.

Faces are named `f<id>` the same way in the viewer, in screenshots and in `part.face(id)`. When the user says "the face on the left" or picks one in the viewer, confirm the id with a labeled screenshot before changing it.

## Project layout

```
inputs/    from outside: supplier STEP, scans, datasheets, prices; scripts only read it
src/       src/<name>.py makes outputs/<name>/: a part, an assembly, a BOM, a study
lib/       code and parameters the scripts share (from lib.params import WALL)
outputs/   made by tule build; never edit it by hand
```

A script may read inputs and other outputs (`import_step("outputs/bracket/bracket.step")`): `tule build` records what each script read, builds what it reads first, and reruns a script only when something it read changed. A script may write only its own output. `tule build --check` lists what is stale.

## Saving

`/workspace` is a git repository. Save when something works, with a message that says what changed:

```sh
tule save -m "Bracket: 4 M5 holes, 2 mm fillets"
```

`tule save` commits everything and merges what other machines saved, then pushes. If it reports a conflict, open the listed files, resolve the `<<<<<<<` sections (keep both sides' intent), and `tule save` again; for a binary file pick one side (`git checkout --ours <file>` or `--theirs`). Plain `git commit` works too; the next `tule save` pushes it. Files over 5 MB go to Git LFS by themselves; if a save is refused for a large file, run the command in the error.

For **variants** the user compares side by side, put the model in a function in `lib/` and make a script per variant (`src/bracket-thin.py` calls `make(wall=1.5)`). A branch (`git switch -c thin-wall`) is for trying something you may throw away; `tule save` on it saves the branch, not main.

Autosaves keep everything (including outputs and unsaved work) every few seconds, but only on this machine's copy; a save is what other machines and later sessions get. `tule snapshot` autosaves now, e.g. before a risky change.

## Long jobs

The SSH session may end; jobs should not depend on it. Run long work in the background, log to a file, and write progress as a sequence output:

```sh
nohup python study.py > study.log 2>&1 &
tail -n 20 study.log          # check back later
```

The machine stops after 30 minutes without SSH sessions or traffic; a job running in the background does not keep it alive by itself, so keep a session open (or check in) during long runs. Everything in `/workspace` is autosaved when it stops; save before you leave work for another machine or session.

## Housekeeping

- Install packages with `pip install`; add them to `tule.toml` (`[packages] pip = [...]`) so they are there after a restart.
- Keep big generated data under `/workspace` only if it must persist; scratch goes to `/tmp`.
- Put notes for the next session (decisions, open questions) in `NOTES.md` and save it.
