# The CPU machine image (see DESIGN.md, "Pluggable backends"). Build context:
# the repository root (control/wrangler.jsonc sets it).
#   docker build --platform linux/amd64 -f runtime/Dockerfile -t tule-runtime .
# runtime/Dockerfile.cuda is the same stack on CUDA for GPU machines; keep them in step.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server git ca-certificates libgl1 libx11-6 \
 && { dpkg --remove --force-depends mesa-libgallium libllvm19 libz3-4 libgl1-mesa-dri libglx-mesa0 2>/dev/null || true; } \
 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*
# (OCP only needs libGL.so.1 to load; the Mesa drivers, 200 MB, are for rendering)

# OCP links against VTK libraries at import (vtk is its only heavy dependency)
COPY runtime/prune_vtk.sh /tmp/prune_vtk.sh
RUN pip install --no-cache-dir cadquery-ocp==8.0.1.1.0 numpy pillow==11.3.0 websockets==17.4 pytest \
 && sh /tmp/prune_vtk.sh && rm /tmp/prune_vtk.sh

COPY python /opt/tule/python
RUN pip install --no-cache-dir --no-deps /opt/tule/python
# the tule CLI: screenshot, inspect, docs, snapshot on the machine (and ssh/up from a laptop)
RUN --mount=type=bind,source=cli,target=/tmp/cli,rw pip install --no-cache-dir --no-deps /tmp/cli
COPY skills /opt/tule/skills
COPY templates/project /opt/tule/template
COPY runtime/tule_agent.py runtime/workspace.py runtime/local.py /opt/tule/
RUN git config --system init.defaultBranch main \
 && git config --system user.name "Tule agent" && git config --system user.email "agent@tule.local"

ENV TULE_WORKSPACE=/workspace PYTHONUNBUFFERED=1
WORKDIR /workspace
ENTRYPOINT ["python", "/opt/tule/tule_agent.py"]
