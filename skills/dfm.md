# Design for manufacturing (`tule.dfm`)

Check whether a part can be made, and fix what cannot. (Placeholder: `tule.dfm` v0 is being built; this topic will document its API.)

## What it will do

- **CNC, 3-axis**: tool access and undercuts along a setup axis, internal corner radii against the tool radius, pocket depth to width ratios, thin walls.
- **FDM printing**: overhangs, minimum wall, bridging, bed contact.
- Findings carry face ids, tags, a severity and values (e.g. "corner radius 1 mm < tool radius 3 mm"), so you can act on them in code and show them to the user as an `overlay` output (`tule docs outputs`).

## Until then

Check the basics by query and look at them:

- Sharp internal vertical corners in pockets cannot be milled: fillet them with at least the tool radius (see "make this part machinable" in `tule docs golden-paths`).
- Holes: compare `part.faces.cylindrical().info()` radii with standard drill sizes.
- Walls: compare the centers and normals of opposite faces (`part.faces.planar().info()`).
- Show what you found as an overlay with `severity` per face, then `tule screenshot` and tell the user what to look at.
