# The CPU machine image (see DESIGN.md, "Pluggable backends"). Build context:
# the repository root (control/wrangler.jsonc sets it).
#   docker build --platform linux/amd64 -f runtime/Dockerfile -t tule-runtime .
# runtime/Dockerfile.cuda is the same stack on CUDA for GPU machines; keep them in step.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server git git-lfs ca-certificates libgl1 libx11-6 ripgrep patch jq curl wget fonts-dejavu-core poppler-utils tini \
 && for p in mesa-libgallium libllvm19 libgl1-mesa-dri libglx-mesa0; do dpkg -L $p 2>/dev/null | while read -r f; do if [ -f "$f" ]; then rm -f "$f"; fi; done; done \
 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*
# (OCP only needs libGL.so.1 to load; the Mesa drivers, 200 MB, are for rendering. Their files go but the
# packages stay installed, so apt keeps working: `apt-get install --reinstall` brings them back; libz3 stays,
# PrusaSlicer needs it)

# OCP links against VTK libraries at import (vtk is its only heavy dependency)
COPY runtime/prune_vtk.sh /tmp/prune_vtk.sh
RUN pip install --no-cache-dir cadquery-ocp==8.0.1.1.0 numpy scipy scikit-image pillow==11.3.0 websockets==17.4 pytest requests beautifulsoup4 trimesh \
 && sh /tmp/prune_vtk.sh && rm /tmp/prune_vtk.sh

COPY python /opt/tule/python
RUN pip install --no-cache-dir --no-deps /opt/tule/python
# the tule CLI: screenshot, inspect, docs, snapshot on the machine (and ssh/up from a laptop)
RUN --mount=type=bind,source=cli,target=/tmp/cli,rw pip install --no-cache-dir --no-deps /tmp/cli
COPY skills /opt/tule/skills
COPY templates/project /opt/tule/template
COPY runtime/tule_agent.py runtime/workspace.py runtime/saves.py runtime/local.py runtime/files.py runtime/annotations.py /opt/tule/
# git: saves go to the project's Tule remote (git-remote-tule, from the CLI); big
# files through Git LFS, with the pre-commit hook moving any it missed there
COPY runtime/git-hooks /opt/tule/git-hooks
RUN git config --system init.defaultBranch main \
 && git config --system user.name "Tule agent" && git config --system user.email "agent@tule.local" \
 && git lfs install --system --skip-repo \
 && git config --system core.hooksPath /opt/tule/git-hooks \
 && git config --system lfs.locksverify false \
 && git config --system --add credential.helper "" && git config --system --add credential.helper tule \
 && printf '# SSH sessions and commands start in the project\n[ "$PWD" = "$HOME" ] && [ -d /workspace ] && cd /workspace\n' | cat - /root/.bashrc > /tmp/rc && mv /tmp/rc /root/.bashrc

ENV TULE_WORKSPACE=/workspace PYTHONUNBUFFERED=1
WORKDIR /workspace
ENTRYPOINT ["tini", "--", "python", "/opt/tule/tule_agent.py"]
