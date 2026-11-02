// three.js viewer, from genau's web/src/viewer.js: environment lighting, a key
// light that moves with the camera, z up, fit and preset views, translucent
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
// A part may carry a placement (mesh.matrix, 16 numbers, row-major).
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { color as palette } from "./colors.js";

const GHOST_OPACITY = 0.12; // the other parts while one is highlighted
const DIM = "--line-strong"; // faces an overlay does not mention (a palette.css variable)
const LAYERS = ["overlay", "annotate", "hover"];
// surfaces sit a little behind their edges and behind opaque parts they touch
const BEHIND = { polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 2 };

// camera directions (from the target) for view(); z is up, "front" looks along +y
export const VIEWS = {
  iso: [0.62, -1.0, 0.68],
  front: [0, -1, 0],
  back: [0, 1, 0],
  left: [-1, 0, 0],
  right: [1, 0, 0],
  top: [0, -1e-3, 1],
  bottom: [0, -1e-3, -1],
};

export class Viewer {
  constructor(el) {
    this.el = el;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const r = (this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true }));
    r.setPixelRatio(dpr);
    r.setClearColor(0x000000, 0);
    r.toneMapping = THREE.NeutralToneMapping;
    el.appendChild(r.domElement);

    this.scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(r);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.95;

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
    // the CAD edges: the palette's navy (--navy in palette.css), a constant since the 3D view stays light
    this.edgeMaterial = new THREE.LineBasicMaterial({ color: 0x23344a, transparent: true, opacity: 0.75 });
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
    const loop = () => {
      requestAnimationFrame(loop);
      if (this.controls.update() || this.dirty) {
        this.dirty = false;
        r.render(this.scene, cam);
        for (const f of this.listeners) f();
      }
    };
    loop();
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

  material(color) {
    let m = this.materials.get(color);
    if (!m) {
      m = new THREE.MeshStandardMaterial({ color: new THREE.Color(color), roughness: 0.62, metalness: 0.0, side: THREE.DoubleSide, ...BEHIND });
      this.materials.set(color, m);
    }
    return m;
  }

  // a face style: solid, or with light diagonal stripes in screen space (hatch)
  faceMaterial(color, hatch) {
    if (!hatch) return this.material(color);
    const key = `${color}/hatch`;
    let m = this.materials.get(key);
    if (!m) {
      m = new THREE.MeshStandardMaterial({ color: new THREE.Color(color), roughness: 0.62, side: THREE.DoubleSide, ...BEHIND });
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
      m = new THREE.MeshStandardMaterial({ color: new THREE.Color(color), roughness: 0.5, transparent: true, opacity, depthWrite: false, ...BEHIND });
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
   * matrix: 16 numbers, row-major | null}].
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
    for (const part of this.group.children) {
      part.userData.geometry.dispose();
      part.userData.lines?.dispose();
      part.userData.surface?.geometry.dispose();
      part.userData.surface?.lines.dispose();
    }
    this.group.clear();
    meshes.forEach((m, index) => {
      if (!m.positions.length) return;
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
      const color = m.color || "#8a96a3";
      part.userData = { index, color, geometry: g, mesh: m };
      part.add(new THREE.Mesh(g, this.material(color)));
      if (m.edges?.length) {
        const lg = new THREE.BufferGeometry();
        lg.setAttribute("position", new THREE.BufferAttribute(m.edges, 3));
        part.userData.lines = lg;
        const lines = new THREE.LineSegments(lg, this.edgeMaterial);
        lines.userData.edges = true;
        part.add(lines);
      }
      this.group.add(part);
    });
    this.group.updateMatrixWorld(true);
    this.paintFaces();
    this.setStyle(style);
    if (this.bounds && (fit === true || (fit === "auto" && this.farFromFit(this.bounds)))) this.fit();
  }

  /** hidden: a Set of part indices not drawn (and not fitted); lit: a Set of
   * highlighted indices (the other parts turn translucent), or null. */
  setStyle({ hidden = null, lit = null } = {}) {
    this.style = { hidden, lit };
    const box = new THREE.Box3();
    for (const part of this.group.children) {
      const { index, color, geometry } = part.userData;
      part.visible = !hidden?.has(index);
      if (part.visible) box.union(geometry.boundingBox.clone().applyMatrix4(part.matrix));
      const ghost = lit && !lit.has(index);
      const [solid] = part.children;
      solid.visible = !ghost;
      let glass = part.children.find((c) => c.userData.glass);
      if (ghost && !glass) {
        const depth = new THREE.Mesh(geometry, this.depthOnly);
        depth.renderOrder = 1;
        depth.userData.glass = "depth";
        glass = new THREE.Mesh(geometry, this.glass(color, GHOST_OPACITY));
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
    this.bounds = box.isEmpty() ? null : box;
    this.updateGrid();
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
      const moved = (rest, vec) => (vec && scale ? Float32Array.from(rest, (x, i) => x + scale * vec[i]) : rest);
      geometry.setAttribute("position", new THREE.BufferAttribute(moved(mesh.positions, motion?.vectors), 3));
      if (lines && mesh.edges) lines.setAttribute("position", new THREE.BufferAttribute(moved(mesh.edges, motion?.edges), 3));
      geometry.computeBoundingBox();
      geometry.computeBoundingSphere();
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
      const base = surface ? this.surfMaterial : part.userData.field ? this.fieldMaterial : this.dim ? this.material(palette(DIM)) : this.material(color);
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
    this.clip(dist, r);
    this.controls.update();
    this.fitted = this.bounds.clone();
    this.moved = false;
    this.dirty = true;
  }

  clip(dist, r) {
    const cam = this.camera;
    cam.near = Math.max(dist - r, dist / 100) / 10;
    cam.far = (dist + r) * 10;
    cam.updateProjectionMatrix();
  }

  // the 3/4 view (from above, one of four corners) that sees the most surface
  // besides the biggest face: a part's features (arms, ribs, pockets) rather
  // than its flat back; the usual corner unless another clearly sees more (as
  // tule screenshot picks it, cli/tule_cli/render.py)
  iso() {
    const [x, y, z] = VIEWS.iso;
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

  view(name) {
    if (!this.bounds) return;
    const dir = name === "iso" ? this.iso() : new THREE.Vector3(...VIEWS[name]);
    this.camera.position.copy(this.controls.target).add(dir);
    this.fitted ||= this.bounds.clone();
    this.fit();
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
    const r = this.bounds ? this.bounds.getBoundingSphere(new THREE.Sphere()).radius : 1;
    this.clip(cam.position.distanceTo(this.controls.target), r);
    this.controls.update();
    this.fitted ||= this.bounds?.clone() ?? null;
    this.moved = true; // a chosen view (an annotation's): keep it
    this.dirty = true;
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
    // the triangle's corner nearest the point (a field's value there)
    const pos = hit.object.geometry.attributes.position;
    const local = hit.object.worldToLocal(hit.point.clone());
    const vertex = [hit.face.a, hit.face.b, hit.face.c].reduce((best, v) =>
      new THREE.Vector3().fromBufferAttribute(pos, v).distanceToSquared(local) < new THREE.Vector3().fromBufferAttribute(pos, best).distanceToSquared(local) ? v : best);
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
    return { index, mesh, face, vertex, rest: at, surf, point: [hit.point.x, hit.point.y, hit.point.z] };
  }

  /** The view as drawn, on a plain background: a canvas at the device's resolution. */
  capture(background = palette("--view-mid")) {
    const src = this.renderer.domElement;
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

  setWireframe(on) {
    this.wire = on;
    this.setStyle(this.style);
  }

  clear() {
    this.setMeshes([]);
    this.setArrows([]);
  }
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
