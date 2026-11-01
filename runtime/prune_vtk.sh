#!/bin/sh
# Removes the VTK libraries OCP does not link (about 500 MB), keeping the
# shared-library closure of OCP's extension module. Nothing in tule uses VTK.
set -eu
SP=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
keep=$(mktemp)
ldd "$SP"/OCP/OCP*.so | awk '/=>/ {print $3}' | grep -E 'vtkmodules|cadquery_ocp.libs' | xargs -n1 basename | sort -u > "$keep"
before=$(du -sm "$SP/vtkmodules" | cut -f1)
find "$SP/vtkmodules" -type f \( -name '*.so' -o -name '*.so.*' \) | while read -r f; do
  grep -qxF "$(basename "$f")" "$keep" || rm -f "$f"
done
find "$SP/vtkmodules" -name '*.py' -delete
rm -rf "$SP/vtkmodules/web" "$SP/vtkmodules/qt" "$SP/vtkmodules/tk" "$SP/vtkmodules/test" "$SP/vtk.py"
echo "prune_vtk: vtkmodules ${before} MB -> $(du -sm "$SP/vtkmodules" | cut -f1) MB, kept $(wc -l < "$keep") libraries"
python -c "import OCP.BRepPrimAPI, OCP.STEPCAFControl, OCP.BRepMesh"
