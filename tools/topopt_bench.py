"""Benchmark: CG iterations per solve of tule.topopt's multigrid-preconditioned solver as the grid gets finer.

Usage: python tools/topopt_bench.py [iterations]   (a 60 × 20 × 30 cantilever, three voxel sizes)

For each voxel size it runs the optimization without continuation and
prints the CG iteration count of the first solve (uniform density) and at a
few later iterations (black-and-white designs, high stiffness contrast),
plus the wall time per optimization iteration.
"""
import sys
import time

from tule import box, topopt

iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 30
part = box(60, 20, 30)
part = part.tag("fixed", part.faces["left"]).tag("load", part.faces["right"])
problem = topopt.Problem(part, fixed=part.faces["fixed"], loads={part.faces["load"]: (0, 0, -100)}, volume_fraction=0.3)
marks = sorted({1, iterations // 3, 2 * iterations // 3, iterations})
print(f"{'voxel':>6} {'elements':>9} {'levels':>6}  " + "  ".join(f"it{m:<3}" for m in marks) + "   s/iteration")
for voxel in (2.5, 1.25, 0.625):
    t = time.time()
    r = topopt.optimize(problem, voxel=voxel, iterations=iterations, continuation=False, output=None, tol=0, filter_radius=5.0)
    dt = (time.time() - t) / r.iterations
    cg = [r.history[m - 1]["cg_iterations"] for m in marks]
    dims = r.density.shape
    levels = topopt._levels_for(dims)
    print(f"{voxel:>6} {r.density.size:>9} {levels:>6}  " + "  ".join(f"{c:<5}" for c in cg) + f"   {dt:.2f}")
