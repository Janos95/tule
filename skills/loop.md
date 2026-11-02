# The loop: edit, build, look, iterate

How to work on a Tule machine so that the user can follow along and nothing is lost.

## One iteration

1. **Edit** a script in `src/` (keep parameters at the top). Rename the placeholder `src/part.py` for your part (`git mv src/part.py src/bracket.py`). It writes its output with `tule.outputs`, named like the script (`src/part.py` → `outputs.part("part", …)`; other files into `outputs.folder()`).
2. **Build**: `tule build` runs the scripts whose inputs changed (`tule build part` for one); the viewer updates live. Fix `TuleError`s from their message and hint (`tule docs occt`). Nothing rebuilds by itself: keeping outputs current is your job.
3. **Look**: `tule screenshot -o /tmp/view.png --labels`, then read the PNG (your own checks stay in `/tmp`, not `work/`). Check other sides (`--view top|front|right|bottom`). Claim a feature only once you have seen it.
4. **Check numbers**: `tule inspect outputs/<name>/<file>.step` (size, volume, faces, tags), or print measurements in the script (`part.size`, `part.volume`, `part.is_valid`).
5. **Iterate** until it matches the request; then save (`tule save -m "…"`) and tell the user what to look at in the viewer. Don't end a turn with work unsaved.

Faces are `f<id>` alike in the viewer, screenshots and `part.face(id)`. When the user says "the face on the left" or picks one in the viewer, confirm the id with a labeled screenshot before changing it.

## Project layout

```
inputs/    from outside: supplier STEP, scans, datasheets, customer specs; scripts only read it
src/       src/<name>.py makes outputs/<name>/: a part, an assembly, a BOM, a study;
           a folder is a group: src/checks/fit.py makes outputs/checks/fit/
lib/       code and parameters the scripts share (from lib.params import WALL)
work/      done by hand from the design: supplier quotes, part comparisons, reviews
outputs/   made by tule build; never edit it by hand
```

A script may read inputs and other outputs (`import_step("outputs/bracket/bracket.step")`): `tule build` records what each script read, builds what it reads first, and reruns a script only when something it read changed. A script may write only its own output.

Before you show the user anything, `tule build --check`: it lists what is out of date or failed (with why and how long its last run took), outputs whose script is gone, and work to check again. Rebuild what you changed and what reads it: `tule build plate --dependents`, `tule build 'plate-*'`, or everything but a long run, `tule build --skip study`. Leave long runs until the design settles, and say so. An output some machine already built from the same files is downloaded instead of run (the build cache); `--force` runs it anyway.

## Work done by hand

Work that is not a script (finding suppliers for the bracket, comparing bearings, a design review) is never re-run by `tule build`. Save its result in `work/` (`work/suppliers/bracket.json`; a JSON list of records shows as a table) and record what it was based on:

```sh
tule based-on work/suppliers/bracket.json outputs/bracket
```

When `outputs/bracket` changes, `tule build` asks to check it again. Redo the work, or if it still holds, run `tule based-on work/suppliers/bracket.json` again. A choice it leads to (which bearing) goes in code, `lib/params.py`.

## Saving

`/workspace` is a git repository. Save when something works, with a message that says what changed:

```sh
tule save -m "Bracket: 4 M5 holes, 2 mm fillets"
```

`tule save` commits, merges what other machines saved, and pushes. If it reports a conflict, open the listed files, resolve the `<<<<<<<` sections (keep both sides' intent), and `tule save` again; for a binary file pick one side (`git checkout --ours <file>` or `--theirs`). Plain `git commit` works too; the next `tule save` pushes it. Files over 5 MB go to Git LFS by themselves; if a save is refused for a large file, run the command in the error.

For **variants** the user compares side by side, put the model in a function in `lib/` and make a script per variant (`src/bracket-thin.py` calls `make(wall=1.5)`). A branch (`git switch -c thin-wall`) is for trying something you may throw away; `tule save` on it saves the branch, not main.

Old revisions live in git history (`git show <rev>:src/bracket.py`): never copy them to archive folders. Autosaves keep everything every few seconds, but only on this machine's copy; a save is what other machines and later sessions get. `tule snapshot` autosaves now.

## Long jobs

`tule build` goes on when the SSH command that started it ends (a timeout, a dropped connection); `tule build study` again waits for it (log: `outputs/.build/build.log`). Write progress as a sequence output.

The machine stops after 30 minutes without SSH sessions or traffic; a background job does not keep it alive, so keep a session open during long runs. Everything in `/workspace` is autosaved when it stops; save before you leave work for another machine or session.

## Housekeeping

- `pip install`, or `apt-get update && apt-get install -y prusa-slicer`; list them in `tule.toml` (`[packages] pip = [...]`, `apt = [...]`) to keep them after a restart.
- Big generated data stays out of `/workspace` unless it must persist; scratch goes to `/tmp`.
- Several load cases or variants of one study: one overlay output with a field each, not an output per case.
- Put notes for the next session (decisions, open questions) in `NOTES.md` and save it.
