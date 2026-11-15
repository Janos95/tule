// three.js viewer, from genau's web/src/viewer.js: environment lighting, a key
// light that moves with the camera, z up, fit and a 3/4 view, translucent
// ghosting for parts that are not highlighted (the Parts panel), snapshots.
//
// What changed for Tule: meshes come from the machine's tessellation
// (indexed, normals from the surfaces, a B-rep face id per triangle) and the
// wireframe draws the real CAD edges (line segments from the B-rep) instead of
// polygon edges in a shader. pick(x, y) returns the part, B-rep face and 3D
// point under a pixel, for the face labels, overlays and annotations.
//
// Faces are colored by layers (setFaces: "overlay", "annotate", "hover"; a
// later layer wins): each part's triangles are sorted by face id once, so a
// face is one range of the index and a style is a geometry group with its own
// material; nothing is copied. setDim greys every face no layer colors.
// setField colors by a field instead (a stress field, per pixel), optionally on a moved shape;
// setSurfaceField draws a field's own surface mesh in place of a part (values per vertex: a
// finite-element mesh's boundary, so slots thinner than any grid stay open). setDeform scales either's motion.
// Arrows (setArrows) are 3D glyphs whose tip touches the point they act on.
// A part may carry a placement (mesh.matrix, 16 numbers, row-major), and placed copies
// (mesh.instances, N of them): drawn as one THREE.InstancedMesh, edges by an instanced
// line shader, picked per copy (pick's `instance`); face styles apply to every copy.
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { color as palette } from "./colors.js";

const GHOST_OPACITY = 0.12; // the other parts while one is highlighted
const DIM = "--line-strong"; // faces an overlay does not mention (a palette.css variable)
const LAYERS = ["overlay", "annotate", "hover"];
// surfaces sit a little behind their edges and behind opaque parts they touch
const _corner = new THREE.Vector3();
const BEHIND = { polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 2 };

// the 3/4 view's camera direction (from the target); z is up, "front" looks along +y
const ISO = [0.62, -1.0, 0.68];
const MAX_EL = 89.99; // an orbit's elevation stays off the poles, so it keeps its azimuth (as tule screenshot)

/** Sets a geometry's positions to rest + scale × vec (or rest), reusing its own array: whether anything changed. */
function place(geometry, rest, vec, scale) {
  const attr = geometry.getAttribute("position");
  if (!vec || !scale) {
    if (attr?.array === rest) return false;
    geometry.setAttribute("position", new THREE.BufferAttribute(rest, 3));
    return true;
  }
  let a = attr;
  if (!a || a.isInterleavedBufferAttribute || a.array === rest || a.array.length !== rest.length) {
    a = new THREE.BufferAttribute(new Float32Array(rest.length), 3);
    geometry.setAttribute("position", a);
  }
  const out = a.array;
  for (let i = 0; i < rest.length; i++) out[i] = rest[i] + scale * vec[i];
  a.needsUpdate = true;
  return true;
}

export class Viewer {
  constructor(el) {
    this.el = el;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    // no preserveDrawingBuffer: every read of the canvas (shot, capture, preview) renders right before it, and
    // keeping the buffer costs a full-screen copy per frame on tiled GPUs (Apple, phones)
    const r = (this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }));
    r.setPixelRatio(dpr);
    this.ratio = dpr; // the resolution of a still view
    this.motionRatio = dpr; // while the view moves: lowered (for good) when frames come too slowly, a huge model
    r.setClearColor(0x000000, 0);
    r.toneMapping = THREE.NeutralToneMapping;
    el.appendChild(r.domElement);

    this.scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(r);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.95;
    // what parts with a material (metal, roughness) reflect: a studio, made on first use
    this.studio = () => (this._studio ??= pmrem.fromScene(studioScene(), 0.0).texture);

    const cam = (this.camera = new THREE.PerspectiveCamera(30, 1, 0.01, 1000));
    cam.up.set(0, 0, 1);
    cam.position.set(3, -5, 3.5);
    this.scene.add(cam);
    const key = new THREE.DirectionalLight(0xffffff, 1.3);
    key.position.set(-1, 1.6, 1);
    key.target.position.set(0, 0, -1);
    cam.add(key, key.target);

    this.controls = new OrbitControls(cam, r.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.15;
    this.controls.screenSpacePanning = true;
    this.controls.addEventListener("start", () => (this.moved = true));

    this.group = new THREE.Group(); // one child per part: its surface, edges and glass
    this.arrows = new THREE.Group();
    this.grid = new THREE.Group(); // the floor grid (grid()), not a part: never picked or fitted
    this.gridStep = null;
    this.scene.add(this.grid, this.group, this.arrows);
    this.materials = new Map();
    // the CAD edges: --edge in palette.css (the navy), read once since the 3D view stays light
    const edge = new THREE.Color(palette("--edge"));
    this.edgeMaterial = new THREE.LineBasicMaterial({ color: edge, transparent: true, opacity: 0.75 });
    this.edgeClear = new THREE.LineBasicMaterial({ color: edge, transparent: true, opacity: 0.3 }); // a see-through part's
    this.depthOnly = new THREE.MeshBasicMaterial({ colorWrite: false, ...BEHIND });
    // a field is colored per pixel: its grid is a 3D texture sampled at the undeformed point ("rest"),
    // so a coarse mesh (a flat face is two triangles) still shows the field as it is
    this.fieldUniforms = { fieldTex: { value: null }, fieldRamp: { value: null }, fieldOrigin: { value: new THREE.Vector3() }, fieldStep: { value: 1 }, fieldSize: { value: new THREE.Vector3(1, 1, 1) } };
    this.fieldMaterial = new THREE.MeshStandardMaterial({ roughness: 0.62, side: THREE.DoubleSide, ...BEHIND });
    this.fieldMaterial.onBeforeCompile = (shader) => {
      Object.assign(shader.uniforms, this.fieldUniforms);
      shader.vertexShader = shader.vertexShader
        .replace("#include <common>", "#include <common>\nattribute vec3 rest;\nvarying vec3 vRest;")
        .replace("#include <begin_vertex>", "#include <begin_vertex>\nvRest = rest;");
      shader.fragmentShader = shader.fragmentShader
        .replace("#include <common>", "#include <common>\nprecision highp sampler3D;\nuniform sampler3D fieldTex;\nuniform sampler2D fieldRamp;\nuniform vec3 fieldOrigin;\nuniform float fieldStep;\nuniform vec3 fieldSize;\nvarying vec3 vRest;")
        .replace("#include <color_fragment>", "vec3 fq = (vRest - fieldOrigin) / fieldStep;\nfloat fv = texture(fieldTex, (fq.zyx + 0.5) / fieldSize).r;\ndiffuseColor.rgb = texture2D(fieldRamp, vec2(clamp(fv, 0.0, 1.0) * 255.0 / 256.0 + 0.5 / 256.0, 0.5)).rgb;");
    };
    // a surface field: a value per vertex (0..1 on the ramp), interpolated per pixel
    this.surfMaterial = new THREE.MeshStandardMaterial({ roughness: 0.62, side: THREE.DoubleSide, ...BEHIND });
    this.surfMaterial.onBeforeCompile = (shader) => {
      shader.uniforms.fieldRamp = this.fieldUniforms.fieldRamp;
      shader.vertexShader = shader.vertexShader
        .replace("#include <common>", "#include <common>\nattribute float fvalue;\nvarying float vFv;")
        .replace("#include <begin_vertex>", "#include <begin_vertex>\nvFv = fvalue;");
      shader.fragmentShader = shader.fragmentShader
        .replace("#include <common>", "#include <common>\nuniform sampler2D fieldRamp;\nvarying float vFv;")
        .replace("#include <color_fragment>", "diffuseColor.rgb = texture2D(fieldRamp, vec2(clamp(vFv, 0.0, 1.0) * 255.0 / 256.0 + 0.5 / 256.0, 0.5)).rgb;");
    };
    this.surfMaterial.customProgramCacheKey = () => "tule-surface-field";
    this.deformScale = 0; // the shown motion: rest + deformScale × vectors
    this.style = { hidden: null, lit: null };
    this.layers = new Map(); // layer -> [{part: index | null (all), faces: [ids], color, hatch}]
    this.dim = false;
    this.wire = true;
    this.fitted = null;
    this.moved = false; // the user moved the camera since the last fit: a resize then keeps their view
    this.insets = { left: 0, top: 0, right: 0, bottom: 0 }; // CSS px covered by panels: the part is framed in the rest
    this.dirty = true;
    this.raycaster = new THREE.Raycaster();
    this.listeners = new Set(); // after each render (HTML labels follow the view)

    new ResizeObserver(() => this.resize()).observe(el);
    this.resize();
    r.domElement.addEventListener("dblclick", () => this.controls.enabled && this.fit());
    // While the camera moves, a model too heavy for the screen's full resolution (missed frames: a stutter) is
    // drawn at a lower one, and sharp again as soon as it stops.
    let moving = false;
    let last = 0;
    const late = []; // the last 30 moving frames: came late (a missed vsync)?
    const loop = (t) => {
      requestAnimationFrame(loop);
      const moved = this.controls.update();
      if (moved) {
        if (moving && last) {
          late.push(t - last > 22);
          if (late.length > 30) late.shift();
          if (late.filter(Boolean).length >= 3 && this.motionRatio > 1) {
            this.motionRatio = Math.max(1, this.motionRatio * 0.6);
            late.length = 0;
          }
        }
        if (r.getPixelRatio() !== this.motionRatio) r.setPixelRatio(this.motionRatio);
        moving = true;
      } else if (moving) {
        moving = false;
        late.length = 0;
        if (r.getPixelRatio() !== this.ratio) {
          r.setPixelRatio(this.ratio);
          this.dirty = true;
        }
      }
      last = t;
      if (moved || this.dirty) {
        this.dirty = false;
        this.clip();
        r.render(this.scene, cam);
        for (const f of this.listeners) f();
      }
    };
    requestAnimationFrame(loop);
  }

  resize() {
    const w = this.el.clientWidth || 1;
    const h = this.el.clientHeight || 1;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.offset();
    // a new shape of the view (a resized window, a rotated phone): frame the part again, unless the user has moved it
    if (this.bounds && this.fitted && !this.moved) this.fit();
    this.renderer.render(this.scene, this.camera);
    this.dirty = true;
  }

  // a part's color: "#rrggbb", or "#rrggbbaa" for a see-through part (glass, a cover): drawn after the solid
  // ones, not hiding what is behind it, a little denser where the surface turns away. look: the part's
  // material, {metal, roughness} 0..1 (brushed steel, black tiles), reflecting a studio; none: a matte paint
  material(color, look = null) {
    const mat = lookOf(look);
    const key = mat ? `${color}|${lookKey(mat)}` : color;
    let m = this.materials.get(key);
    if (!m && mat) {
      const [rgb, a] = opacityOf(color);
      const { metal, roughness } = mat;
      m = new THREE.MeshStandardMaterial({ color: new THREE.Color(rgb), metalness: metal, roughness, envMap: this.studio(), envMapIntensity: 1.0 - 0.2 * (1 - metal), side: THREE.DoubleSide, ...BEHIND });
      if (a < 1) Object.assign(m, { transparent: true, opacity: a, depthWrite: false });
      this.materials.set(key, m);
    }
    if (!m) {
      const [rgb, a] = opacityOf(color);
      if (a < 1) {
        m = new THREE.MeshStandardMaterial({ color: new THREE.Color(rgb), roughness: 0.25, transparent: true, opacity: a, depthWrite: false, side: THREE.DoubleSide, ...BEHIND });
        m.onBeforeCompile = (shader) => {
          shader.fragmentShader = shader.fragmentShader.replace(
            "#include <dithering_fragment>",
            `#include <dithering_fragment>
            float rim = 1.0 - abs(dot(normal, normalize(vViewPosition)));
            gl_FragColor.a = min(0.92, gl_FragColor.a * (0.8 + 1.6 * rim * rim));`,
          );
        };
        m.customProgramCacheKey = () => "tule-clear";
      } else m = new THREE.MeshStandardMaterial({ color: new THREE.Color(rgb), roughness: 0.62, metalness: 0.0, side: THREE.DoubleSide, ...BEHIND });
      this.materials.set(key, m);
    }
    return m;
  }

  // a face style: solid, or with light diagonal stripes in screen space (hatch)
  faceMaterial(color, hatch) {
    if (!hatch) return this.material(color);
    const key = `${color}/hatch`;
    let m = this.materials.get(key);
    if (!m) {
      m = new THREE.MeshStandardMaterial({ color: new THREE.Color(opacityOf(color)[0]), roughness: 0.62, side: THREE.DoubleSide, ...BEHIND });
      m.onBeforeCompile = (shader) => {
        shader.fragmentShader = shader.fragmentShader.replace(
          "#include <dithering_fragment>",
          `#include <dithering_fragment>
          float stripe = step(0.5, fract((gl_FragCoord.x + gl_FragCoord.y) / 12.0));
          gl_FragColor.rgb = mix(gl_FragColor.rgb, vec3(1.0), 0.55 * stripe);`,
        );
      };
      m.customProgramCacheKey = () => "tule-hatch";
      this.materials.set(key, m);
    }
    return m;
  }

  glass(color, opacity) {
    const key = `${color}/${opacity}`;
    let m = this.materials.get(key);
    if (!m) {
      m = new THREE.MeshStandardMaterial({ color: new THREE.Color(opacityOf(color)[0]), roughness: 0.5, transparent: true, opacity, depthWrite: false, ...BEHIND });
      m.onBeforeCompile = (shader) => {
        shader.fragmentShader = shader.fragmentShader.replace(
          "#include <dithering_fragment>",
          `#include <dithering_fragment>
          float rim = 1.0 - abs(dot(normal, normalize(vViewPosition)));
          gl_FragColor.a = min(1.0, gl_FragColor.a * (0.6 + 2.4 * rim * rim));`,
        );
      };
      m.customProgramCacheKey = () => "tule-glass";
      this.materials.set(key, m);
    }
    return m;
  }

  /** Replaces the shown parts: [{name, color, positions: Float32Array (xyz),
   * normals, indices: Uint32Array, faceIds: Uint32Array (per triangle),
   * edges: Float32Array (segment endpoints) | null, tags: {tag: [face ids]},
   * matrix: 16 numbers, row-major | null, instances: N × 16 numbers (placed
   * copies, row-major, inside `matrix`) | null}].
   * fit: true, false or "auto" (fit when the bounds are new or far from the
   * last fit). extent: {min, max} the camera frames as well (every frame of a
   * motion study, so it holds still while the study plays), or null. */
  /** fit: true (frame it, same direction), "auto" (only when it moved far), "fresh" (a new
   * 3/4 view picked for it: another output) or false. */
  setMeshes(meshes, fit = false, style = this.style, extent = null) {
    if (fit === "fresh") {
      this.fitted = null;
      fit = true;
    }
    this.extent = extent ? new THREE.Box3(new THREE.Vector3(...extent.min), new THREE.Vector3(...extent.max)) : null;
    // a part whose mesh is the same data (a motion study's next frame, an output redrawn) is kept as it is:
    // its geometry stays on the GPU. One showing a field or moved by one is made anew.
    const spare = new Map(); // positions array -> [part]
    for (const part of this.group.children) {
      const u = part.userData;
      if (u.surface || u.field || u.motion || u.geometry.getAttribute("position")?.array !== u.mesh.positions) continue;
      if (!spare.has(u.mesh.positions)) spare.set(u.mesh.positions, []);
      spare.get(u.mesh.positions).push(part);
    }
    const kept = new Set();
    const reuse = (m) => {
      const list = spare.get(m.positions) ?? [];
      const i = list.findIndex((p) => p.userData.color === (m.color || palette("--part")) && lookKey(p.userData.mesh.material) === lookKey(m.material) && p.userData.mesh.indices === m.indices && p.userData.mesh.edges === m.edges && (p.userData.mesh.instances ?? null) === (m.instances ?? null));
      return i < 0 ? null : list.splice(i, 1)[0];
    };
    const plan = meshes.map((m) => (m.positions.length ? reuse(m) : null));
    for (const p of plan) if (p) kept.add(p);
    for (const part of this.group.children) {
      if (kept.has(part)) continue;
      for (const c of part.children) if (c.isInstancedMesh) c.dispose(); // its placements' GPU buffer
      part.userData.geometry.dispose();
      part.userData.lines?.dispose();
      part.userData.surface?.geometry.dispose();
      part.userData.surface?.lines.dispose();
    }
    this.group.clear();
    meshes.forEach((m, index) => {
      if (!m.positions.length || m.instances?.length === 0) return;
      const old = plan[index];
      if (old) {
        Object.assign(old.userData, { index, mesh: m });
        old.matrixAutoUpdate = !m.matrix;
        if (m.matrix) old.matrix.set(...m.matrix);
        else old.matrix.identity();
        this.group.add(old);
        return;
      }
      sortByFace(m);
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(m.positions, 3));
      g.setAttribute("normal", new THREE.BufferAttribute(m.normals, 3));
      g.setIndex(new THREE.BufferAttribute(m.indices, 1));
      g.computeBoundingBox();
      g.computeBoundingSphere();
      const part = new THREE.Group();
      if (m.matrix) {
        part.matrixAutoUpdate = false;
        part.matrix.set(...m.matrix);
      }
      const color = m.color || palette("--part");
      part.userData = { index, color, geometry: g, mesh: m };
      const clear = opacityOf(color)[1] < 1;
      // placed copies (m.instances: N × 16 numbers, row-major): one instanced draw of the mesh, and of its edges
      const placements = m.instances ? instanceMatrices(m.instances) : null;
      part.userData.placements = placements;
      const solid = this.solid(part, g, this.material(color, m.material));
      if (placements) part.userData.box = solid.boundingBox;
      if (clear) solid.renderOrder = 1; // after the solid parts
      part.add(solid);
      if (m.edges?.length) {
        const lg = placements ? new THREE.InstancedBufferGeometry() : new THREE.BufferGeometry();
        lg.setAttribute("position", new THREE.BufferAttribute(m.edges, 3));
        if (placements) {
          lg.setAttribute("instMatrix", placements);
          lg.instanceCount = placements.count;
        }
        part.userData.lines = lg;
        const edge = clear ? this.edgeClear : this.edgeMaterial;
        const lines = new THREE.LineSegments(lg, placements ? this.instancedLines(edge) : edge);
        if (placements) lines.frustumCulled = false; // its bounds are one copy's
        lines.userData.edges = true;
        part.add(lines);
      }
      this.group.add(part);
    });
    if (this.blend) this.movePlacements(this.blend.placements, this.blend.t);
    this.group.updateMatrixWorld(true);
    this.paintFaces();
    this.setStyle(style);
    if (this.bounds && (fit === true || (fit === "auto" && this.farFromFit(this.bounds)))) this.fit();
  }

  /** A part's surface drawn with `material`: a mesh, or for placed copies an InstancedMesh sharing the part's placements. */
  solid(part, geometry, material) {
    const placements = part.userData.placements;
    if (!placements) return new THREE.Mesh(geometry, material);
    const m = new THREE.InstancedMesh(geometry, material, placements.count);
    m.instanceMatrix = placements;
    m.computeBoundingBox();
    m.computeBoundingSphere();
    return m;
  }

  /** An edge material that draws each line at every placement (an InstancedBufferGeometry's instMatrix). */
  instancedLines(base) {
    const key = base === this.edgeClear ? "instancedClear" : "instancedEdges";
    if (!this[key]) {
      const m = base.clone();
      m.onBeforeCompile = (shader) => {
        shader.vertexShader = shader.vertexShader
          .replace("#include <common>", "#include <common>\nattribute mat4 instMatrix;")
          .replace("#include <begin_vertex>", "#include <begin_vertex>\ntransformed = (instMatrix * vec4(transformed, 1.0)).xyz;");
      };
      m.customProgramCacheKey = () => "tule-instanced-lines";
      this[key] = m;
    }
    return this[key];
  }

  /** Moves the drawn parts in place, without rebuilding them (a motion study while it plays):
   * placements(mesh) gives the part's placement in two frames ([a, b], 16 numbers row-major, or null
   * for none) and t in [0, 1] blends them: positions straight, rotations along the shortest turn. */
  movePlacements(placements, t) {
    this.blend = placements ? { placements, t } : null; // kept while playing: parts drawn anew move there too
    if (!placements) return;
    for (const part of this.group.children) {
      const [a, b] = placements(part.userData.mesh);
      const pa = pose(a), pb = pose(b);
      part.matrixAutoUpdate = false;
      part.matrix.compose(
        _p.lerpVectors(pa.p, pb.p, t),
        _q.slerpQuaternions(pa.q, pb.q, t),
        _s.lerpVectors(pa.s, pb.s, t),
      );
    }
    this.group.updateMatrixWorld(true);
    this.dirty = true;
  }

  /** Puts each drawn part at fn(its mesh): a 4x4 row-major placement, or null (where it was made). For a
   * motion study's frame whose files are the ones drawn: the parts move, nothing is rebuilt. */
  placeParts(fn) {
    for (const part of this.group.children) {
      const m = fn(part.userData.mesh);
      part.matrixAutoUpdate = false;
      if (m) part.matrix.set(...m);
      else part.matrix.identity();
    }
    this.group.updateMatrixWorld(true);
    this.dirty = true;
  }

  /** hidden: a Set of part indices not drawn (and not fitted); lit: a Set of
   * highlighted indices (the other parts turn translucent), or null. */
  setStyle({ hidden = null, lit = null } = {}) {
    this.style = { hidden, lit };
    const box = new THREE.Box3();
    for (const part of this.group.children) {
      const { index, color, geometry } = part.userData;
      part.visible = !hidden?.has(index);
      if (part.visible) box.union((part.userData.box ?? geometry.boundingBox).clone().applyMatrix4(part.matrix));
      const ghost = lit && !lit.has(index);
      const [solid] = part.children;
      solid.visible = !ghost;
      let glass = part.children.find((c) => c.userData.glass);
      if (ghost && !glass) {
        const depth = this.solid(part, geometry, this.depthOnly);
        depth.renderOrder = 1;
        depth.userData.glass = "depth";
        glass = this.solid(part, geometry, this.glass(color, GHOST_OPACITY));
        glass.renderOrder = 2;
        glass.userData.glass = "color";
        part.add(depth, glass);
      }
      for (const c of part.children) {
        if (c.userData.glass) c.visible = !!ghost;
        if (c.userData.edges) c.visible = this.wire && !ghost;
      }
    }
    if (this.extent && !hidden?.size) box.union(this.extent);
    const next = box.isEmpty() ? null : box;
    const same = next ? !!this.bounds?.equals(next) : !this.bounds;
    this.bounds = next;
    if (!same) this.updateGrid(); // not on every hover of a parts row: the grid follows the bounds
    this.dirty = true;
  }

  /** A floor grid under the parts: on z = 0 (or under the lowest point, if that is below it),
   * about twice the parts' footprint, with round spacing (1, 2, 5 × 10^n mm) and every fifth line
   * stronger; the X axis red, Y green, where they cross it; lines fade towards the edge. */
  updateGrid() {
    for (const c of this.grid.children) c.geometry.dispose();
    this.grid.clear();
    this.gridStep = null;
    if (!this.bounds) return;
    const { min, max } = this.bounds;
    const span = Math.max(max.x - min.x, max.y - min.y, (max.z - min.z) * 0.5, 1e-3);
    const raw = (span * 2.2) / 24;
    const pow = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 5, 10].map((m) => m * pow).find((v) => v >= raw);
    const half = Math.ceil((span * 1.1) / step) * step;
    const cx = Math.round((min.x + max.x) / 2 / step) * step;
    const cy = Math.round((min.y + max.y) / 2 / step) * step;
    const z = Math.min(0, min.z);
    const R = half;
    const n = Math.round(half / step);
    const minor = [], major = [], axes = [];
    const seg = 24; // pieces per line, for the fade
    const add = (list, color, x0, y0, x1, y1) => {
      for (let i = 0; i < seg; i++) {
        const t0 = i / seg, t1 = (i + 1) / seg;
        for (const t of [t0, t1]) {
          const x = x0 + (x1 - x0) * t, y = y0 + (y1 - y0) * t;
          const d = Math.hypot(x - cx, y - cy) / R;
          const a = Math.max(0, 1 - d ** 3);
          list.push(x, y, z, color[0], color[1], color[2], a);
        }
      }
    };
    // palette.css: the lines in --faint (at the opacities below, about as light as --line-strong),
    // the X axis in --danger-fill, Y in --ok-fill
    const rgb = (name) => new THREE.Color(palette(name)).toArray();
    const GRID = rgb("--faint"), AXIS_X = rgb("--danger-fill"), AXIS_Y = rgb("--ok-fill");
    for (let i = -n; i <= n; i++) {
      const v = i * step;
      const strong = (Math.round((cx + v) / step) % 5) === 0;
      const onAxisX = Math.abs(cy + v) < step / 1000; // the line y = 0: the X axis
      const onAxisY = Math.abs(cx + v) < step / 1000; // x = 0: the Y axis
      add(onAxisX ? axes : strong ? major : minor, onAxisX ? AXIS_X : GRID, cx - half, cy + v, cx + half, cy + v);
      add(onAxisY ? axes : strong ? major : minor, onAxisY ? AXIS_Y : GRID, cx + v, cy - half, cx + v, cy + half);
    }
    const lines = (data, opacity) => {
      const a = new Float32Array(data);
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.InterleavedBufferAttribute(new THREE.InterleavedBuffer(a, 7), 3, 0));
      g.setAttribute("color", new THREE.InterleavedBufferAttribute(new THREE.InterleavedBuffer(a, 7), 4, 3));
      const m = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity, depthWrite: false });
      const l = new THREE.LineSegments(g, m);
      l.renderOrder = -1;
      return l;
    };
    this.grid.add(lines(minor, 0.32), lines(major, 0.6), lines(axes, 0.9));
    this.gridStep = step;
  }

  /** Colors faces: styles [{part: index or null for every part, faces: [B-rep
   * face ids], color, hatch}] under a layer name ("overlay", "annotate",
   * "hover"); an empty list removes the layer. */
  setFaces(layer, styles) {
    if (styles?.length) this.layers.set(layer, styles);
    else this.layers.delete(layer);
    this.paintFaces();
  }

  /** The color ramp as a texture: [[r, g, b] sRGB 0..1, low to high]. */
  setRamp(colors) {
    const rgba = new Uint8Array(256 * 4);
    for (let i = 0; i < 256; i++) {
      const x = (i / 255) * (colors.length - 1);
      const j = Math.min(colors.length - 2, Math.floor(x));
      for (let k = 0; k < 3; k++) rgba[4 * i + k] = Math.round(255 * (colors[j][k] + (colors[j + 1][k] - colors[j][k]) * (x - j)));
      rgba[4 * i + 3] = 255;
    }
    const u = this.fieldUniforms;
    u.fieldRamp.value?.dispose();
    const ramp = new THREE.DataTexture(rgba, 256, 1);
    ramp.colorSpace = THREE.SRGBColorSpace;
    ramp.minFilter = ramp.magFilter = THREE.LinearFilter;
    ramp.needsUpdate = true;
    u.fieldRamp.value = ramp;
  }

  /** A field instead of a color per face (a stress field). volume: {values: Float32Array, one per grid
   * node (nx, ny, nz, z fastest) on the scale (0 low, 1 high), shape, origin, step, ramp: [[r, g, b] sRGB
   * 0..1, low to high]}; fn(mesh, part index) returns {vectors?: Float32Array (a motion per vertex),
   * edges?: Float32Array (one per edge point)} or null (the part as it is), drawn moved by setDeform's scale.
   * setField(null) restores the parts. Face layers still win. */
  setField(fn, volume) {
    const u = this.fieldUniforms;
    this.setSurfaceField(null);
    if (volume) {
      const [nx, ny, nz] = volume.shape;
      u.fieldTex.value?.dispose();
      const tex = new THREE.Data3DTexture(Uint16Array.from(volume.values, (v) => THREE.DataUtils.toHalfFloat(v)), nz, ny, nx);
      tex.format = THREE.RedFormat;
      tex.type = THREE.HalfFloatType;
      tex.minFilter = tex.magFilter = THREE.LinearFilter;
      tex.unpackAlignment = 1;
      tex.needsUpdate = true;
      u.fieldTex.value = tex;
      this.setRamp(volume.ramp);
      u.fieldOrigin.value.set(...volume.origin);
      u.fieldStep.value = volume.step;
      u.fieldSize.value.set(nz, ny, nx);
    }
    for (const part of this.group.children) {
      const { index, mesh, geometry } = part.userData;
      const f = fn && volume ? fn(mesh, index) ?? {} : null;
      part.userData.field = !!f;
      part.userData.motion = f?.vectors ? { vectors: f.vectors, edges: f.edges ?? null } : null;
      if (f) geometry.setAttribute("rest", new THREE.BufferAttribute(mesh.positions, 3));
      else geometry.deleteAttribute("rest");
    }
    this.setDeform(this.deformScale);
    this.paintFaces();
  }

  /** Draws a field's own surface in place of part `surf.index` (an overlay's part): {index, positions:
   * Float32Array (rest), indices: Uint32Array, faceIds: Uint32Array (a B-rep face per triangle) | null,
   * values: Float32Array (per vertex, 0..1 on the ramp), vectors: Float32Array | null (a motion per vertex),
   * ramp}. Lines are drawn where the face id changes. null puts the parts back. */
  setSurfaceField(surf) {
    for (const part of this.group.children) {
      const s = part.userData.surface;
      if (!s) continue;
      const [solid] = part.children;
      solid.geometry = part.userData.geometry;
      const lines = part.children.find((c) => c.userData.edges);
      if (lines && part.userData.lines) lines.geometry = part.userData.lines;
      else if (lines) part.remove(lines);
      s.geometry.dispose();
      s.lines?.dispose();
      part.userData.surface = null;
    }
    const part = surf && this.group.children.find((p) => p.userData.index === surf.index);
    if (part) {
      this.setRamp(surf.ramp);
      part.userData.surface = surfaceGeometry(surf);
      part.userData.field = false;
      part.userData.motion = null;
      part.userData.geometry.deleteAttribute("rest");
      const [solid] = part.children;
      solid.geometry = part.userData.surface.geometry;
      let lines = part.children.find((c) => c.userData.edges);
      if (!lines) {
        lines = new THREE.LineSegments(part.userData.surface.lines, this.edgeMaterial);
        lines.userData.edges = true;
        part.add(lines);
      } else lines.geometry = part.userData.surface.lines;
      this.setStyle(this.style);
    }
    this.setDeform(this.deformScale);
    this.paintFaces();
  }

  /** Moves shapes with a motion (setField's vectors, a surface field's) to rest + scale × motion. */
  setDeform(scale) {
    this.deformScale = scale;
    for (const part of this.group.children) {
      const { mesh, geometry, lines, motion, surface } = part.userData;
      if (surface) {
        surface.move(scale);
        continue;
      }
      // a part that does not move keeps its rest positions; a moving one is written in place (no new GPU buffer per frame)
      if (place(geometry, mesh.positions, motion?.vectors, scale)) {
        geometry.computeBoundingBox();
        geometry.computeBoundingSphere();
      }
      if (lines && mesh.edges) place(lines, mesh.edges, motion?.edges, scale);
    }
    this.dirty = true;
  }

  /** Greys every face that no layer colors (an overlay's "the rest"). */
  setDim(on) {
    this.dim = !!on;
    this.paintFaces();
  }

  paintFaces() {
    for (const part of this.group.children) {
      const { index, color, mesh, surface } = part.userData;
      const geometry = surface?.geometry ?? part.userData.geometry;
      const solid = part.children[0];
      const pick = new Map(); // face id -> material
      for (const layer of LAYERS)
        for (const s of this.layers.get(layer) ?? [])
          if (s.part == null || s.part === index) for (const f of s.faces) pick.set(+f, this.faceMaterial(s.color, s.hatch));
      const base = surface ? this.surfMaterial : part.userData.field ? this.fieldMaterial : this.dim ? this.material(palette(DIM)) : this.material(color, mesh.material);
      geometry.clearGroups();
      if (!pick.size) {
        solid.material = base;
        continue;
      }
      const mats = [base];
      const slot = (m) => (mats.includes(m) ? mats.indexOf(m) : mats.push(m) - 1);
      const ids = surface?.faceIds ?? mesh.faceIds;
      let start = 0;
      let cur = -1;
      for (let t = 0; t <= ids.length; t++) {
        const m = t < ids.length ? slot(pick.get(ids[t]) ?? base) : -2;
        if (m === cur) continue;
        if (cur >= 0) geometry.addGroup(start * 3, (t - start) * 3, cur);
        start = t;
        cur = m;
      }
      solid.material = mats;
    }
    this.dirty = true;
  }

  /** Arrows [{at: [x,y,z], dir: [dx,dy,dz], color}]: the tip touches `at`,
   * the length is a fraction of the shown parts' size. */
  setArrows(list) {
    for (const a of this.arrows.children) for (const c of a.children) c.geometry.dispose();
    this.arrows.clear();
    const r = this.bounds ? this.bounds.getBoundingSphere(new THREE.Sphere()).radius : 1;
    const len = 0.32 * r;
    const shaft = new THREE.CylinderGeometry(len * 0.03, len * 0.03, len * 0.72, 12);
    shaft.translate(0, -len * 0.64, 0);
    const head = new THREE.ConeGeometry(len * 0.085, len * 0.28, 20);
    head.translate(0, -len * 0.14, 0);
    (list ?? []).forEach((a, i) => {
      const dir = new THREE.Vector3(...a.dir);
      if (!(dir.lengthSq() > 0)) return;
      const g = new THREE.Group();
      const m = this.material(a.color ?? palette("--reed"));
      g.add(new THREE.Mesh(shaft.clone(), m), new THREE.Mesh(head.clone(), m));
      // the glyph points along +y with its tip at the origin: turn +y onto dir
      g.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.normalize());
      g.position.set(...a.at);
      g.userData = { arrow: i, tail: new THREE.Vector3(...a.at).addScaledVector(dir, -len) };
      this.arrows.add(g);
    });
    shaft.dispose();
    head.dispose();
    this.arrows.updateMatrixWorld(true);
    this.dirty = true;
  }

  /** Where arrow i starts (its tail), for labels. */
  arrowTail(i) {
    return this.arrows.children.find((a) => a.userData.arrow === i)?.userData.tail ?? null;
  }

  farFromFit(box) {
    if (!this.fitted) return true;
    const a = box.getBoundingSphere(new THREE.Sphere());
    const b = this.fitted.getBoundingSphere(new THREE.Sphere());
    return a.radius > 1.6 * b.radius || a.radius < b.radius / 3 || a.center.distanceTo(b.center) > 0.6 * b.radius;
  }

  /** The parts of the view covered by panels (the legend, the toolbar): fitting frames the
   * part in the rest, and the view's center moves there (a shifted projection, not a smaller canvas). */
  setInsets(insets) {
    const old = JSON.stringify(this.insets);
    this.insets = { left: 0, top: 0, right: 0, bottom: 0, ...insets };
    if (JSON.stringify(this.insets) === old) return;
    this.offset();
    if (this.bounds && this.fitted && !this.moved) this.fit();
    this.dirty = true;
  }

  offset() {
    const w = this.el.clientWidth || 1;
    const h = this.el.clientHeight || 1;
    const { left, top, right, bottom } = this.insets;
    if (left || top || right || bottom) this.camera.setViewOffset(w, h, (right - left) / 2, (bottom - top) / 2, w, h);
    else this.camera.clearViewOffset();
    this.camera.updateProjectionMatrix();
  }

  /** The share of the view's width and height left free by the insets. */
  free() {
    const w = this.el.clientWidth || 1;
    const h = this.el.clientHeight || 1;
    const { left, top, right, bottom } = this.insets;
    return [Math.max(0.35, (w - left - right) / w), Math.max(0.35, (h - top - bottom) / h)];
  }

  // frames the bounding box in the free part of the view: the camera keeps its
  // direction (a 3/4 view the first time) and moves back until every box
  // corner is inside it
  fit() {
    if (!this.bounds) return;
    const cam = this.camera;
    const c = this.bounds.getCenter(new THREE.Vector3());
    const dir = this.fitted ? cam.position.clone().sub(this.controls.target).normalize() : this.iso();
    const right = new THREE.Vector3().crossVectors(cam.up, dir).normalize();
    if (right.lengthSq() < 0.5) right.set(1, 0, 0);
    const up = new THREE.Vector3().crossVectors(dir, right);
    const [fx, fy] = this.free();
    const tv = Math.tan(THREE.MathUtils.degToRad(cam.fov) / 2) * fy;
    const th = (tv / fy) * cam.aspect * fx;
    const { min, max } = this.bounds;
    let dist = 0;
    for (let i = 0; i < 8; i++) {
      const p = new THREE.Vector3(i & 1 ? max.x : min.x, i & 2 ? max.y : min.y, i & 4 ? max.z : min.z).sub(c);
      const z = p.dot(dir);
      dist = Math.max(dist, z + (1.15 * Math.abs(p.dot(right))) / th, z + (1.2 * Math.abs(p.dot(up))) / tv);
    }
    const r = this.bounds.getBoundingSphere(new THREE.Sphere()).radius || 1;
    dist = Math.max(dist, 1.05 * r, 1e-3);
    this.controls.target.copy(c);
    cam.position.copy(c).addScaledVector(dir, dist);
    this.clip();
    this.controls.update();
    this.fitted = this.bounds.clone();
    this.moved = false;
    this.dirty = true;
  }

  /** The near and far planes from where the camera is now (every frame, so zooming in never cuts into the
   * model): near just short of the model's box, or, with the camera inside it, a small share of the distance to
   * what it orbits; far well past the box's farthest corner (the floor grid reaches beyond it). */
  clip() {
    const cam = this.camera;
    const b = this.bounds;
    if (!b || b.isEmpty()) return;
    const p = cam.position;
    const focus = Math.max(p.distanceTo(this.controls.target), 1e-6);
    let farthest = 0;
    for (let i = 0; i < 8; i++) {
      _corner.set(i & 1 ? b.max.x : b.min.x, i & 2 ? b.max.y : b.min.y, i & 4 ? b.max.z : b.min.z);
      farthest = Math.max(farthest, _corner.distanceTo(p));
    }
    const far = Math.max(farthest, focus) * 4;
    const near = Math.max(b.distanceToPoint(p) * 0.8, focus / 500, far / 2e5);
    if (Math.abs(cam.near - near) > near * 0.01 || Math.abs(cam.far - far) > far * 0.01) {
      cam.near = near;
      cam.far = far;
      cam.updateProjectionMatrix();
    }
  }

  // the 3/4 view (from above, one of four corners) that sees the most surface
  // besides the biggest face: a part's features (arms, ribs, pockets) rather
  // than its flat back; the usual corner unless another clearly sees more (as
  // tule screenshot picks it, cli/tule_cli/render.py)
  iso() {
    const [x, y, z] = ISO;
    const corners = [[x, y, z], [-x, y, z], [x, -y, z], [-x, -y, z]].map((c) => new THREE.Vector3(...c).normalize());
    const all = []; // [unit normal, area] of each face that points one way
    const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
    for (const part of this.group.children) {
      const { mesh: m } = part.userData;
      const rot = new THREE.Matrix3().setFromMatrix4(part.matrixWorld);
      const faces = new Map(); // face id -> [nx, ny, nz, area]
      const P = m.positions, I = m.indices;
      for (let t = 0; t < I.length / 3; t++) {
        a.fromArray(P, 3 * I[3 * t]);
        b.fromArray(P, 3 * I[3 * t + 1]).sub(a);
        c.fromArray(P, 3 * I[3 * t + 2]).sub(a);
        b.cross(c); // area-weighted normal
        const id = m.faceIds?.length ? m.faceIds[t] : t;
        let f = faces.get(id);
        if (!f) faces.set(id, (f = [0, 0, 0, 0]));
        f[0] += b.x; f[1] += b.y; f[2] += b.z; f[3] += b.length();
      }
      for (const f of faces.values()) {
        const n = new THREE.Vector3(f[0], f[1], f[2]).applyMatrix3(rot);
        if (n.length() <= 0.5 * f[3]) continue; // curved all round (a hole's wall)
        all.push([n.normalize(), f[3]]);
      }
    }
    const big = all.reduce((m, f) => Math.max(m, f[1]), 0);
    const seen = corners.map((d) => all.reduce((s, [n, area]) => s + (area < big ? area * Math.max(0, n.dot(d)) : 0), 0));
    const best = seen.indexOf(Math.max(...seen));
    return corners[seen[best] > 1.2 * seen[0] ? best : 0];
  }

  /** The camera as plain numbers: {position, target, up, fov (degrees)}. */
  getCamera() {
    const v = (x) => [x.x, x.y, x.z].map((n) => +n.toPrecision(7));
    return { position: v(this.camera.position), target: v(this.controls.target), up: v(this.camera.up), fov: this.camera.fov };
  }

  setCamera({ position, target, up, fov }) {
    const cam = this.camera;
    cam.position.set(...position);
    this.controls.target.set(...target);
    if (up) cam.up.set(...up);
    if (fov) cam.fov = fov;
    cam.updateProjectionMatrix(); // the fov; clip() only updates it when the planes move
    this.clip();
    this.controls.update();
    this.fitted ||= this.bounds?.clone() ?? null;
    this.moved = true; // a chosen view (an annotation's): keep it
    this.dirty = true;
  }

  /** The camera as tule screenshot names it (cli/tule_cli/camera.py): an orbit about the target, z up.
   * azimuth: degrees about +Z from +X toward +Y; elevation: above the XY plane; distance: mm to the
   * target; fov: the vertical field of view in degrees. */
  orbit() {
    const t = this.controls.target;
    const d = this.camera.position.clone().sub(t);
    const distance = d.length() || 1;
    const r = (x, n = 7) => +x.toPrecision(n);
    return {
      azimuth: r(THREE.MathUtils.radToDeg(Math.atan2(d.y, d.x)), 6),
      elevation: r(THREE.MathUtils.radToDeg(Math.asin(Math.max(-1, Math.min(1, d.z / distance)))), 6),
      distance: r(distance),
      target: [t.x, t.y, t.z].map((v) => r(v)),
      fov: r(this.camera.fov, 6),
    };
  }

  /** Puts the camera at an orbit (orbit()'s numbers; missing ones stay as they are). */
  setOrbit({ azimuth, elevation, distance, target, fov } = {}) {
    const now = this.orbit();
    const az = THREE.MathUtils.degToRad(Number.isFinite(azimuth) ? azimuth : now.azimuth);
    const el = THREE.MathUtils.degToRad(Math.max(-MAX_EL, Math.min(MAX_EL, Number.isFinite(elevation) ? elevation : now.elevation)));
    const dist = distance > 0 ? distance : now.distance;
    const t = Array.isArray(target) && target.length === 3 && target.every(Number.isFinite) ? target : now.target;
    const position = [t[0] + dist * Math.cos(el) * Math.cos(az), t[1] + dist * Math.cos(el) * Math.sin(az), t[2] + dist * Math.sin(el)];
    this.setCamera({ position, target: t, up: [0, 0, 1], fov: fov > 0 ? fov : now.fov });
  }

  /** A picture through the camera (or through `orbit`, leaving the view as it is), exactly w×h pixels with
   * nothing shifted for panels and no floor grid (grid: true keeps it): what tule screenshot draws through the
   * same camera. */
  shot(w, h, orbit = null, { background = palette("--view-mid"), grid = false } = {}) {
    const r = this.renderer;
    const cam = this.camera;
    const saved = { cam: this.getCamera(), size: r.getSize(new THREE.Vector2()), ratio: r.getPixelRatio(), moved: this.moved, fitted: this.fitted, grid: this.grid.visible };
    this.grid.visible = grid && saved.grid;
    if (orbit) this.setOrbit(orbit);
    r.setPixelRatio(1);
    r.setSize(w, h, false);
    cam.aspect = w / h;
    cam.clearViewOffset();
    cam.updateProjectionMatrix();
    r.render(this.scene, cam);
    const out = Object.assign(document.createElement("canvas"), { width: w, height: h });
    const g = out.getContext("2d");
    g.fillStyle = background;
    g.fillRect(0, 0, w, h);
    g.drawImage(r.domElement, 0, 0, w, h, 0, 0, w, h);
    this.grid.visible = saved.grid;
    r.setPixelRatio(saved.ratio);
    r.setSize(saved.size.x, saved.size.y, false);
    cam.aspect = saved.size.x / saved.size.y;
    this.setCamera(saved.cam);
    this.moved = saved.moved;
    this.fitted = saved.fitted;
    this.offset();
    r.render(this.scene, cam);
    return out;
  }

  /** Orbit, pan and zoom on or off (annotation strokes keep their view). */
  setLocked(on) {
    this.controls.enabled = !on;
  }

  /** A world point in CSS pixels from the viewer's top left, and whether it is in front of the camera. */
  project(p) {
    const v = p.clone().project(this.camera);
    return { x: ((v.x + 1) / 2) * this.el.clientWidth, y: ((1 - v.y) / 2) * this.el.clientHeight, front: v.z < 1 };
  }

  /** What is under a client pixel: {index, mesh, face, point: [x,y,z]} for a
   * visible part's B-rep face, {arrow: i} for an arrow, or null. */
  pick(clientX, clientY) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const solids = this.group.children.filter((p) => p.visible && p.children[0].visible).map((p) => p.children[0]);
    const hit = this.raycaster.intersectObjects([...solids, ...this.arrows.children], true)[0];
    if (!hit) return null;
    const arrow = hit.object.parent?.userData.arrow;
    if (arrow !== undefined) return { arrow };
    const { index, mesh, surface } = hit.object.parent.userData;
    // the same point on the undeformed shape (where a field is sampled)
    const rest = hit.object.geometry.attributes.rest;
    let at = null;
    if (rest && hit.barycoord) {
      const b = hit.barycoord;
      at = [0, 1, 2].map((d) => b.x * rest.getComponent(hit.face.a, d) + b.y * rest.getComponent(hit.face.b, d) + b.z * rest.getComponent(hit.face.c, d));
    }
    // on a surface field: the triangle's vertices (the field's own numbering) and the point's weights
    const surf = surface && hit.barycoord ? { tri: [hit.face.a, hit.face.b, hit.face.c].map((v) => surface.vertexOf[v]), bary: [hit.barycoord.x, hit.barycoord.y, hit.barycoord.z] } : null;
    const face = surface ? surface.faceIds?.[hit.faceIndex] ?? -1 : mesh.faceIds[hit.faceIndex];
    // a placed copy: which one (its face ids are the part's, the same on every copy)
    const instance = hit.object.isInstancedMesh ? hit.instanceId : null;
    return { index, mesh, face, instance, rest: at, surf, point: [hit.point.x, hit.point.y, hit.point.z] };
  }

  /** A picture for a link's preview: w×h pixels, from the camera's direction, the model filling it
   * (the view itself is left as it was). Null with nothing drawn. */
  preview(w = 1200, h = 630, background = palette("--view-mid")) {
    if (!this.bounds) return null;
    const r = this.renderer;
    const cam = this.camera;
    const saved = { cam: this.getCamera(), size: r.getSize(new THREE.Vector2()), ratio: r.getPixelRatio(), moved: this.moved, fitted: this.fitted };
    r.setPixelRatio(1);
    r.setSize(w, h, false);
    cam.aspect = w / h;
    cam.clearViewOffset();
    const c = this.bounds.getCenter(new THREE.Vector3());
    const dir = cam.position.clone().sub(this.controls.target).normalize();
    const right = new THREE.Vector3().crossVectors(cam.up, dir).normalize();
    if (right.lengthSq() < 0.5) right.set(1, 0, 0);
    const up = new THREE.Vector3().crossVectors(dir, right);
    const tv = Math.tan(THREE.MathUtils.degToRad(cam.fov) / 2);
    const th = tv * cam.aspect;
    const { min, max } = this.bounds;
    let dist = 0;
    for (let i = 0; i < 8; i++) {
      const p = new THREE.Vector3(i & 1 ? max.x : min.x, i & 2 ? max.y : min.y, i & 4 ? max.z : min.z).sub(c);
      const z = p.dot(dir);
      dist = Math.max(dist, z + (1.08 * Math.abs(p.dot(right))) / th, z + (1.1 * Math.abs(p.dot(up))) / tv);
    }
    cam.position.copy(c).addScaledVector(dir, Math.max(dist, 1e-3));
    cam.lookAt(c);
    this.clip();
    // the box's corners as drawn: zoom until the farthest one is near the edge
    let reach = 0;
    for (let i = 0; i < 8; i++) {
      const q = new THREE.Vector3(i & 1 ? max.x : min.x, i & 2 ? max.y : min.y, i & 4 ? max.z : min.z).project(cam);
      reach = Math.max(reach, Math.abs(q.x), Math.abs(q.y));
    }
    cam.zoom = reach > 0 ? 0.94 / reach : 1;
    cam.updateProjectionMatrix();
    r.render(this.scene, cam);
    cam.zoom = 1;
    const out = Object.assign(document.createElement("canvas"), { width: w, height: h });
    const g = out.getContext("2d");
    g.fillStyle = background;
    g.fillRect(0, 0, w, h);
    g.drawImage(r.domElement, 0, 0);
    // the view as it was
    r.setPixelRatio(saved.ratio);
    r.setSize(saved.size.x, saved.size.y, false);
    cam.aspect = saved.size.x / saved.size.y;
    this.setCamera(saved.cam);
    this.moved = saved.moved;
    this.fitted = saved.fitted;
    this.offset();
    r.render(this.scene, cam);
    return out;
  }

  /** The view as drawn, on a plain background: a canvas at the device's resolution. */
  capture(background = palette("--view-mid")) {
    const src = this.renderer.domElement;
    if (this.renderer.getPixelRatio() !== this.ratio) this.renderer.setPixelRatio(this.ratio); // full resolution, even mid-move
    this.renderer.render(this.scene, this.camera);
    const out = document.createElement("canvas");
    out.width = src.width;
    out.height = src.height;
    const g = out.getContext("2d");
    g.fillStyle = background;
    g.fillRect(0, 0, out.width, out.height);
    g.drawImage(src, 0, 0);
    return out;
  }

  clear() {
    this.setMeshes([]);
    this.setArrows([]);
  }
}

// A part's material, {metal, roughness} each 0..1, or null for none (or one that cannot be read: drawn as before).
function lookOf(look) {
  if (!look || typeof look !== "object") return null;
  const metal = Number(look.metal ?? look.metalness ?? 0), roughness = Number(look.roughness ?? 0.62);
  if (!Number.isFinite(metal) || !Number.isFinite(roughness)) return null;
  const clamp = (x) => Math.min(1, Math.max(0, x));
  return { metal: clamp(metal), roughness: clamp(roughness) };
}
const lookKey = (look) => (lookOf(look) ? `${lookOf(look).metal},${lookOf(look).roughness}` : "");

// The studio a material reflects (z up, as the parts): a bright ceiling fading to a grey horizon, a dark floor,
// a big soft overhead panel, a key strip and a rim strip, so a steel cylinder shows a horizon line and soft
// highlights and a polished ball the room, while black tiles stay black. Built once into a PMREM.
function studioScene() {
  const scene = new THREE.Scene();
  const dome = new THREE.SphereGeometry(10, 64, 32);
  dome.rotateX(Math.PI / 2); // its poles on z
  const p = dome.getAttribute("position");
  const c = new Float32Array(p.count * 3);
  // linear values, by the direction's height z: floor, ground, horizon, sky
  const stops = [[-1, 0.24], [-0.5, 0.34], [-0.15, 0.58], [0.02, 0.74], [0.4, 0.84], [1, 0.92]];
  for (let i = 0; i < p.count; i++) {
    const z = p.getZ(i) / 10;
    const k = stops.findIndex(([at]) => at >= z);
    const [z0, v0] = stops[Math.max(0, k - 1)], [z1, v1] = stops[Math.max(0, k)];
    const v = z1 > z0 ? v0 + ((v1 - v0) * (z - z0)) / (z1 - z0) : v1;
    c.set([v, v, v * 0.99], 3 * i); // a touch warm, as daylight off concrete
  }
  dome.setAttribute("color", new THREE.BufferAttribute(c, 3));
  scene.add(new THREE.Mesh(dome, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.BackSide })));
  const panel = (w, h, intensity, pos, look) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ side: THREE.DoubleSide }));
    m.material.color.setScalar(intensity);
    m.position.set(...pos);
    m.lookAt(...look);
    scene.add(m);
  };
  panel(8, 5, 2.0, [0, 0, 6], [0, 0, 0]); // overhead softbox
  // tall strips around (a cylinder shows them as soft vertical bands): azimuth°, width, brightness
  for (const [az, w, k] of [[135, 2.2, 3.2], [35, 1.2, 1.4], [-55, 1.8, 2.2], [-150, 1.0, 1.0]]) {
    const a = (az * Math.PI) / 180;
    panel(w, 7, k, [6.5 * Math.cos(a), 6.5 * Math.sin(a), 1.2], [0, 0, 1.2]);
  }
  return scene;
}

// A color and its opacity: "#rrggbbaa" or "#rgba" -> ["#rrggbb", 0..1]; any other color is solid.
export function opacityOf(color) {
  const m = typeof color === "string" && /^#([0-9a-f]{8}|[0-9a-f]{4})$/i.exec(color);
  if (!m) return [color, 1];
  const h = m[1].length === 4 ? [...m[1]].map((c) => c + c).join("") : m[1];
  return [`#${h.slice(0, 6)}`, parseInt(h.slice(6), 16) / 255];
}

// Placed copies' matrices (N × 16 numbers, row-major) as an instanced attribute (three's matrices are column-major),
// shared by the part's surface, edges and ghost.
function instanceMatrices(rowMajor) {
  const n = Math.floor(rowMajor.length / 16);
  const a = new Float32Array(16 * n);
  for (let i = 0, o = 0; i < n; i++, o += 16)
    for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) a[o + 4 * c + r] = rowMajor[o + 4 * r + c];
  return new THREE.InstancedBufferAttribute(a, 16);
}

// A placement (16 numbers, row-major, or null) as position, rotation and scale; kept per array.
const _p = new THREE.Vector3(), _q = new THREE.Quaternion(), _s = new THREE.Vector3();
const IDENTITY = { p: new THREE.Vector3(), q: new THREE.Quaternion(), s: new THREE.Vector3(1, 1, 1) };
const poses = new WeakMap();
function pose(m) {
  if (!m) return IDENTITY;
  let r = poses.get(m);
  if (!r) {
    r = { p: new THREE.Vector3(), q: new THREE.Quaternion(), s: new THREE.Vector3() };
    new THREE.Matrix4().set(...m).decompose(r.p, r.q, r.s);
    poses.set(m, r);
  }
  return r;
}

// Reorders a mesh's triangles by B-rep face id (once), so each face is one
// range of the index buffer. Returns the mesh.
export function sortByFace(m) {
  if (m.sorted) return m;
  const n = m.faceIds.length;
  const order = Array.from({ length: n }, (_, i) => i).sort((a, b) => m.faceIds[a] - m.faceIds[b] || a - b);
  const idx = new Uint32Array(n * 3);
  const ids = new Uint32Array(n);
  order.forEach((t, i) => {
    idx[3 * i] = m.indices[3 * t];
    idx[3 * i + 1] = m.indices[3 * t + 1];
    idx[3 * i + 2] = m.indices[3 * t + 2];
    ids[i] = m.faceIds[t];
  });
  m.indices = idx;
  m.faceIds = ids;
  m.sorted = true;
  return m;
}

// A surface field's mesh for drawing: vertices split per B-rep face (sharp edges, smooth faces),
// triangles sorted by face (as sortByFace), lines where the face changes or the surface ends,
// and move(scale) to draw it at rest + scale × the vectors.
function surfaceGeometry({ positions, indices, faceIds, values, vectors }) {
  const nt = indices.length / 3;
  const ids = faceIds ?? new Uint32Array(nt);
  const order = Array.from({ length: nt }, (_, i) => i).sort((a, b) => ids[a] - ids[b] || a - b);
  const key = new Map(); // (vertex, face) -> new vertex
  const vertexOf = [];
  const index = new Uint32Array(3 * nt);
  const sortedIds = new Uint32Array(nt);
  order.forEach((t, i) => {
    const f = ids[t];
    sortedIds[i] = f;
    for (let c = 0; c < 3; c++) {
      const v = indices[3 * t + c];
      const k = v * 4294967296 + f;
      let n = key.get(k);
      if (n === undefined) {
        n = vertexOf.length;
        key.set(k, n);
        vertexOf.push(v);
      }
      index[3 * i + c] = n;
    }
  });
  const g = new THREE.BufferGeometry();
  const nv = vertexOf.length;
  const rest = new Float32Array(3 * nv);
  const vec = vectors ? new Float32Array(3 * nv) : null;
  const val = new Float32Array(nv);
  vertexOf.forEach((v, i) => {
    for (let d = 0; d < 3; d++) {
      rest[3 * i + d] = positions[3 * v + d];
      if (vec) vec[3 * i + d] = vectors[3 * v + d];
    }
    val[i] = values[v];
  });
  g.setIndex(new THREE.BufferAttribute(index, 1));
  g.setAttribute("position", new THREE.BufferAttribute(rest.slice(), 3));
  g.setAttribute("fvalue", new THREE.BufferAttribute(val, 1));
  // lines: triangle edges (in the field's own numbering) with one triangle, or two of different faces
  const seen = new Map();
  for (let t = 0; t < nt; t++)
    for (let c = 0; c < 3; c++) {
      const a = indices[3 * t + c], b = indices[3 * t + ((c + 1) % 3)];
      const k = Math.min(a, b) * 4294967296 + Math.max(a, b);
      const e = seen.get(k);
      if (e === undefined) seen.set(k, ids[t]);
      else seen.set(k, e === ids[t] ? -1 : -2); // -1: inside a face, -2: between faces
    }
  const segs = [];
  for (const [k, e] of seen) if (e !== -1) segs.push(Math.floor(k / 4294967296), k % 4294967296);
  const lineRest = new Float32Array(3 * segs.length);
  const lineVec = vectors ? new Float32Array(3 * segs.length) : null;
  segs.forEach((v, i) => {
    for (let d = 0; d < 3; d++) {
      lineRest[3 * i + d] = positions[3 * v + d];
      if (lineVec) lineVec[3 * i + d] = vectors[3 * v + d];
    }
  });
  const lines = new THREE.BufferGeometry();
  lines.setAttribute("position", new THREE.BufferAttribute(lineRest.slice(), 3));
  const move = (scale) => {
    const p = g.attributes.position.array;
    const lp = lines.attributes.position.array;
    for (let i = 0; i < p.length; i++) p[i] = rest[i] + (vec ? scale * vec[i] : 0);
    for (let i = 0; i < lp.length; i++) lp[i] = lineRest[i] + (lineVec ? scale * lineVec[i] : 0);
    g.attributes.position.needsUpdate = true;
    lines.attributes.position.needsUpdate = true;
    g.computeVertexNormals();
    g.computeBoundingBox();
    g.computeBoundingSphere();
    lines.computeBoundingSphere();
  };
  move(0);
  return { geometry: g, lines, faceIds: faceIds ? sortedIds : null, vertexOf: Uint32Array.from(vertexOf), move };
}
