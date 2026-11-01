# The loop: edit, run, look, iterate

How to work on a Tule machine so that the user can follow along and nothing is lost.

## One iteration

1. **Edit** Python in `/workspace` (start from `main.py`; keep parameters at the top).
2. **Run** it: `python main.py`. Fix `TuleError`s from their message and hint (`tule docs occt`).
3. **Write** results to `outputs/<name>/` with `tule.outputs`; the viewer updates live.
4. **Look**: `tule screenshot -o view.png --labels`, then read the PNG. Check other sides with `--view top|front|right|bottom`. Do not claim a feature is there until you have seen it.
5. **Check numbers**: `tule inspect outputs/<name>/<file>.step` (size, volume, faces, tags), or print measurements in the script (`part.size`, `part.volume`, `part.is_valid`).
6. **Iterate** until it matches the request; then tell the user what to look at in the viewer.

Faces are named `f<id>` the same way in the viewer, in screenshots and in `part.face(id)`. When the user says "the face on the left" or picks one in the viewer, confirm the id with a labeled screenshot before changing it.

## Git

`/workspace` is a git repository. Commit code that works with a message that says what changed:

```sh
git add -A && git commit -m "Bracket: 4 M5 holes, 2 mm fillets"
```

For **variants**, use a branch per variant and a separate output name, so the user can compare them side by side in the viewer:

```sh
git switch -c thin-wall
# change WALL = 1.5 in main.py, write to outputs/bracket-thin
python main.py && git commit -am "Thin-wall variant"
git switch main
```

Snapshots save everything (including outputs and uncommitted work) every few seconds; git is for readable history. `tule snapshot` saves now, e.g. before a risky change.

## Long jobs

The SSH session may end; jobs should not depend on it. Run long work in the background, log to a file, and write progress as a sequence output:

```sh
nohup python study.py > study.log 2>&1 &
tail -n 20 study.log          # check back later
```

The machine stops after 30 minutes without SSH sessions or traffic; a job running in the background does not keep it alive by itself, so keep a session open (or check in) during long runs. Everything in `/workspace` is saved when it stops.

## Housekeeping

- Install packages with `pip install`; add them to `tule.toml` (`[packages] pip = [...]`) so they are there after a restart.
- Keep big generated data under `/workspace` only if it must persist; scratch goes to `/tmp`.
- Put notes for the next session (decisions, open questions) in `NOTES.md` and commit it.
