# Annotations: what the user drew in the viewer

The user can draw on the model in the viewer (strokes, arrows, notes) and pick faces with a label, then save. Each save becomes a folder you can read:

```
/workspace/annotations/<time>/annotation.png    the view as the user saw it, with the drawing
/workspace/annotations/<time>/annotation.json   which output and view, a summary, and the marks:
                                                strokes mapped to the B-rep faces they cover,
                                                face picks with the user's labels
```

## Read it

```sh
ls -t /workspace/annotations | head -1                     # the newest
cat /workspace/annotations/<time>/annotation.json          # start with the summary
```

Then look at `annotation.png` (read the image): the drawing says what the ids alone do not, such as a direction ("push here, downwards"), "this is too thin", or a circled region. Face ids (`f12`) are those of the output's file at the time of drawing, the same ids as `part.face(12)` and `tule screenshot --labels`. If you have changed the model since, map them through a labeled screenshot of the old output or ask.

## Apply it

Turn marks into model facts, usually tags (boundary conditions, keep-outs, faces to change), and show them back as an overlay so the user can confirm before you build on them:

```python
from tule import *

part = import_step("outputs/bracket/bracket.step")
# from the annotation: a stroke on f11 "500 N down", a pick of f13 "bolted"
part = part.tag("load", part.face(11)).tag("fixed", part.face(13))
outputs.overlay("bracket-bc", part, title="Boundary conditions from your sketch: right?",
                faces={part.faces["load"]: {"role": "load", "label": "500 N down"},
                       part.faces["fixed"]: {"role": "fixed", "label": "bolted"}},
                arrows=[{"at": (0, 6, 18), "dir": (0, 0, -1), "label": "500 N"}])
```

Then `tule screenshot -o /tmp/bc.png --output bracket-bc` to check it yourself, and tell the user what you understood ("500 N down on the boss top f11, bolted on the bottom face f13; see bracket-bc in the viewer").

## Make it last

Face ids change when the model changes. Once the user confirms, move the tags into the part's script in `src/` with queries that survive edits (`part.faces.top()`, `boss.faces["top"]`, `part.faces.planar().normal(-Z)`) rather than ids, `tule build`, and check the labeled screenshot shows the same faces. Note the decision in your commit message.
