// three.js viewer, from genau's web/src/viewer.js: environment lighting, a key
// light that moves with the camera, z up, fit and preset views, translucent
// ghosting for parts that are not highlighted (the Parts panel), snapshots.
//
// What changed for Tule: meshes come from the machine's tessellation
// (indexed, normals from the surfaces, a B-rep face id per triangle) and the
// wireframe draws the real CAD edges (line segments from the B-rep) instead of
// polygon edges in a shader. pick(x, y) returns the part and B-rep face under
// a pixel, for the face labels.
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";

const GHOST_OPACITY = 0.12; // the other parts while one is highlighted
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

    this.group = new THREE.Group(); // one child per part: its surface, edges and glass
    this.scene.add(this.group);
    this.materials = new Map();
    this.edgeMaterial = new THREE.LineBasicMaterial({ color: 0x18202a, transparent: true, opacity: 0.75 });
    this.depthOnly = new THREE.MeshBasicMaterial({ colorWrite: false, ...BEHIND });
    this.style = { hidden: null, lit: null };
    this.wire = true;
    this.fitted = null;
    this.dirty = true;
    this.raycaster = new THREE.Raycaster();

    new ResizeObserver(() => this.resize()).observe(el);
    this.resize();
    r.domElement.addEventListener("dblclick", () => this.fit());
    const loop = () => {
      requestAnimationFrame(loop);
      if (this.controls.update() || this.dirty) {
        this.dirty = false;
        r.render(this.scene, cam);
      }
    };
    loop();
  }

  resize() {
    const w = this.el.clientWidth || 1;
    const h = this.el.clientHeight || 1;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.render(this.scene, this.camera);
  }

  material(color) {
    let m = this.materials.get(color);
    if (!m) {
      m = new THREE.MeshStandardMaterial({ color: new THREE.Color(color), roughness: 0.62, metalness: 0.0, side: THREE.DoubleSide, ...BEHIND });
      this.materials.set(color, m);
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
   * edges: Float32Array (segment endpoints) | null, tags: {tag: [face ids]}}].
   * fit: true, false or "auto" (fit when the bounds are new or far from the
   * last fit). */
  setMeshes(meshes, fit = false, style = this.style) {
    for (const part of this.group.children) {
      part.userData.geometry.dispose();
      part.userData.lines?.dispose();
    }
    this.group.clear();
    meshes.forEach((m, index) => {
      if (!m.positions.length) return;
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(m.positions, 3));
      g.setAttribute("normal", new THREE.BufferAttribute(m.normals, 3));
      g.setIndex(new THREE.BufferAttribute(m.indices, 1));
      g.computeBoundingBox();
      g.computeBoundingSphere();
      const part = new THREE.Group();
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
      if (part.visible) box.union(geometry.boundingBox);
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
    this.bounds = box.isEmpty() ? null : box;
    this.dirty = true;
  }

  farFromFit(box) {
    if (!this.fitted) return true;
    const a = box.getBoundingSphere(new THREE.Sphere());
    const b = this.fitted.getBoundingSphere(new THREE.Sphere());
    return a.radius > 1.6 * b.radius || a.radius < b.radius / 3 || a.center.distanceTo(b.center) > 0.6 * b.radius;
  }

  // frames the bounding box: the camera keeps its direction (a 3/4 view the
  // first time) and moves back until every box corner is inside the view
  fit() {
    if (!this.bounds) return;
    const cam = this.camera;
    const c = this.bounds.getCenter(new THREE.Vector3());
    const dir = this.fitted ? cam.position.clone().sub(this.controls.target).normalize() : new THREE.Vector3(...VIEWS.iso).normalize();
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
      dist = Math.max(dist, z + (1.15 * Math.abs(p.dot(right))) / th, z + (1.2 * Math.abs(p.dot(up))) / tv);
    }
    const r = this.bounds.getBoundingSphere(new THREE.Sphere()).radius || 1;
    dist = Math.max(dist, 1.05 * r, 1e-3);
    this.controls.target.copy(c);
    cam.position.copy(c).addScaledVector(dir, dist);
    cam.near = Math.max(dist - r, dist / 100) / 10;
    cam.far = (dist + r) * 10;
    cam.updateProjectionMatrix();
    this.controls.update();
    this.fitted = this.bounds.clone();
    this.dirty = true;
  }

  view(name) {
    if (!this.bounds) return;
    this.camera.position.copy(this.controls.target).add(new THREE.Vector3(...VIEWS[name]));
    this.fitted ||= this.bounds.clone();
    this.fit();
  }

  /** The visible part and B-rep face under a client pixel: {index, mesh, face} or null. */
  pick(clientX, clientY) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const solids = this.group.children.filter((p) => p.visible && p.children[0].visible).map((p) => p.children[0]);
    const hit = this.raycaster.intersectObjects(solids, false)[0];
    if (!hit) return null;
    const { index, mesh } = hit.object.parent.userData;
    return { index, mesh, face: mesh.faceIds[hit.faceIndex] };
  }

  setWireframe(on) {
    this.wire = on;
    this.setStyle(this.style);
  }

  clear() {
    this.setMeshes([]);
  }
}
