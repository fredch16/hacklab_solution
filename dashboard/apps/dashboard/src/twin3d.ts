// Live 3-D twin of the arm: the ElRobot's URDF meshes posed from the joint readings (the arm server's fk, the same
// model motion planning uses), the camera's line of sight and the fused position of the marked object.
// three.js is vendored (offline-only rule); loaded at runtime so the rest of the dashboard never waits for it.

/* eslint-disable @typescript-eslint/no-explicit-any */
type Twin = {
  ok: boolean; links: { mesh: string; matrix: number[] }[];
  camera: { position: number[]; axis: number[]; up: number[] };
  object: { point_m: number[]; sigma_m: number } | null;
  target: string | null; tracking: boolean; moving: boolean;
  job: { kind: string; status: string; step: string } | null;
};

export type TwinListener = (t: Twin | null) => void;

const load = (path: string): Promise<any> => import(path);

export async function mountTwin(host: HTMLElement, onData: TwinListener): Promise<void> {
  let THREE: any, STLLoader: any, OrbitControls: any;
  try {
    THREE = await load("./three.module.min.js");
    ({ STLLoader } = await load("./STLLoader.js"));
    ({ OrbitControls } = await load("./OrbitControls.js"));
  } catch {
    host.dataset.state = "unavailable";
    return;
  }
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
  renderer.shadowMap.enabled = true;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  host.append(renderer.domElement);

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x0b1020, 1.4, 3.2);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 10);
  camera.up.set(0, 0, 1);                        // fk is z-up
  camera.position.set(0.75, -0.55, 0.55);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0.12, 0.14);
  controls.enableDamping = true;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.35;
  controls.minDistance = 0.3; controls.maxDistance = 2.2;
  renderer.domElement.addEventListener("pointerdown", () => { controls.autoRotate = false; });

  scene.add(new THREE.HemisphereLight(0xbfd6ff, 0x1a1530, 1.1));
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(0.6, -0.4, 1.2); key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  Object.assign(key.shadow.camera, { left: -0.6, right: 0.6, top: 0.6, bottom: -0.6, near: 0.1, far: 3 });
  scene.add(key);
  const rim = new THREE.PointLight(0xff8a3d, 1.6, 2); rim.position.set(-0.5, 0.4, 0.4); scene.add(rim);

  // the desk: a soft disc that catches the arm's shadow, and a fine polar grid
  const floor = new THREE.Mesh(new THREE.CircleGeometry(0.9, 64), new THREE.ShadowMaterial({ opacity: 0.35 }));
  floor.receiveShadow = true; scene.add(floor);
  const grid = new THREE.PolarGridHelper(0.8, 12, 8, 64, 0x2f4a7a, 0x1d2c4d);
  grid.rotation.x = Math.PI / 2; scene.add(grid);

  const white = new THREE.MeshStandardMaterial({ color: 0xece8e2, roughness: 0.45, metalness: 0.05 });
  const servo = new THREE.MeshStandardMaterial({ color: 0x1f232b, roughness: 0.55, metalness: 0.35 });
  const accent = new THREE.MeshStandardMaterial({ color: 0xff8a3d, roughness: 0.4, emissive: 0x401800 });
  const meshes = new Map<string, any>();
  const loader = new STLLoader();
  const geometry = new Map<string, Promise<any>>();
  const meshFor = (name: string) => {
    let m = meshes.get(name);
    if (m) return m;
    const mat = /^ST3215/i.test(name) ? servo : /gear|jaw/i.test(name) ? accent : white;
    m = new THREE.Mesh(undefined, mat);
    m.matrixAutoUpdate = false; m.castShadow = true; m.visible = false;
    scene.add(m); meshes.set(name, m);
    if (!geometry.has(name)) geometry.set(name, loader.loadAsync(`/laptop/director/mesh/${name}`));
    void geometry.get(name)!.then((g: any) => { g.computeVertexNormals(); m.geometry = g; m.visible = true; })
      .catch(() => { /* one missing mesh: the rest of the arm still shows */ });
    return m;
  };

  // the camera's line of sight, and the object: a glowing core inside a halo the size of its uncertainty
  const sight = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
    new THREE.LineDashedMaterial({ color: 0x5eead4, dashSize: 0.012, gapSize: 0.008, transparent: true, opacity: 0.9 }));
  scene.add(sight);
  const core = new THREE.Mesh(new THREE.SphereGeometry(0.012, 24, 16),
    new THREE.MeshStandardMaterial({ color: 0xff8a3d, emissive: 0xff6a1a, emissiveIntensity: 1.6 }));
  const halo = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 16),
    new THREE.MeshBasicMaterial({ color: 0xff8a3d, transparent: true, opacity: 0.14, depthWrite: false }));
  const drop = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
    new THREE.LineBasicMaterial({ color: 0xff8a3d, transparent: true, opacity: 0.5 }));
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.02, 0.026, 48),
    new THREE.MeshBasicMaterial({ color: 0xff8a3d, transparent: true, opacity: 0.7, side: THREE.DoubleSide }));
  const obj = new THREE.Group(); obj.add(core, halo); scene.add(obj, drop, ring);
  obj.visible = drop.visible = ring.visible = false;

  const resize = () => {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
  };
  new ResizeObserver(resize).observe(host); resize();

  let last: Twin | null = null;
  async function poll() {
    try {
      const r = await fetch("/laptop/director/twin", { cache: "no-store", signal: AbortSignal.timeout(1500) });
      last = r.ok ? await r.json() as Twin : null;
    } catch { last = null; }
    host.dataset.state = last?.ok ? "live" : "offline";
    if (last?.ok) {
      for (const l of last.links) meshFor(l.mesh).matrix.fromArray(l.matrix);
      const c = last.camera, p = new THREE.Vector3(...c.position), a = new THREE.Vector3(...c.axis);
      let reach = 0.35;
      if (last.object) {
        const o = new THREE.Vector3(...last.object.point_m);
        reach = Math.max(0.05, o.clone().sub(p).dot(a));
        obj.position.copy(o); halo.scale.setScalar(Math.max(0.015, last.object.sigma_m));
        drop.geometry.setFromPoints([o, new THREE.Vector3(o.x, o.y, 0.001)]);
        ring.position.set(o.x, o.y, 0.002);
      }
      obj.visible = drop.visible = ring.visible = !!last.object;
      core.material.emissiveIntensity = last.tracking ? 1.8 : 0.4;
      sight.geometry.setFromPoints([p, p.clone().add(a.multiplyScalar(reach))]);
      sight.computeLineDistances();
    }
    onData(last);
  }
  window.setInterval(() => { if (!document.hidden) void poll(); }, 200);
  void poll();

  const clock = new THREE.Clock();
  renderer.setAnimationLoop(() => {
    const t = clock.getElapsedTime();
    ring.scale.setScalar(1 + 0.6 * ((t * 0.8) % 1));
    ring.material.opacity = 0.7 * (1 - ((t * 0.8) % 1));
    controls.update();
    renderer.render(scene, camera);
  });
}
