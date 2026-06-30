import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const RADIUS = 1.0;

// ── Fresnel + directional-light shader for surface particles ──────────────────
const VERT = /* glsl */`
  attribute vec3 color;
  varying   vec3 vColor;
  varying   float vBright;
  uniform   float uScale;

  void main() {
    vec4 mvPos = modelViewMatrix * vec4(position, 1.0);

    // Each particle sits on a sphere → its sphere-normal = normalize(position)
    vec3 worldNormal = normalize(mat3(modelMatrix) * normalize(position));
    vec3 worldPos    = (modelMatrix * vec4(position, 1.0)).xyz;
    vec3 viewDir     = normalize(cameraPosition - worldPos);

    // Fresnel limb-darkening: 1 at globe centre facing camera, 0 at rim
    float fresnel = max(0.0, dot(worldNormal, viewDir));

    // Directional "key light" from upper-left-front (10 o'clock spec)
    vec3  lightDir = normalize(vec3(-0.4, 0.9, 0.5));
    float light    = max(0.0, dot(worldNormal, lightDir));

    // Fresnel dominates; light adds asymmetric warmth
    vBright = mix(0.12, 1.0, pow(fresnel, 2.2) * 0.6 + pow(light, 1.4) * 0.4);
    vColor  = color;

    gl_Position  = projectionMatrix * mvPos;
    // Perspective-attenuated size; brighter particles bloom slightly larger
    gl_PointSize = (0.005 + vBright * 0.005) * (uScale / -mvPos.z);
  }
`;

const FRAG = /* glsl */`
  varying vec3  vColor;
  varying float vBright;

  void main() {
    // Soft disc: hard core → smooth falloff
    float r     = length(gl_PointCoord - 0.5) * 2.0;
    float alpha = 1.0 - smoothstep(0.4, 1.0, r);
    if (alpha < 0.01) discard;
    gl_FragColor = vec4(vColor * vBright, alpha * 0.88);
  }
`;

export class GlobeScene {
  constructor(canvas) {
    this._canvas        = canvas;
    this._cityMarkers   = [];
    this._autoRotate    = true;
    this._hoveredMarker = null;
    this._clickCb       = null;
    this._orbit         = null;
    this._particleMat   = null; // ShaderMaterial — updated on resize
    this._init();
    this._setupInteraction();
    this._loadContinents();
    this._animate();
  }

  // ── Initialise scene ────────────────────────────────────────────────────────
  _init() {
    const w = this._canvas.clientWidth;
    const h = this._canvas.clientHeight;

    this._renderer = new THREE.WebGLRenderer({ canvas: this._canvas, antialias: true, alpha: true });
    this._renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this._renderer.setSize(w, h, false);

    this._scene  = new THREE.Scene();
    this._camera = new THREE.PerspectiveCamera(50, w / h, 0.01, 100);
    this._camera.position.set(0, 0, 2.8);

    this._controls = new OrbitControls(this._camera, this._canvas);
    this._controls.enableDamping  = true;
    this._controls.dampingFactor  = 0.05;
    this._controls.minDistance    = 1.8;
    this._controls.maxDistance    = 5;
    this._controls.addEventListener("start", () => { this._autoRotate = false; });
    this._controls.addEventListener("end",   () => setTimeout(() => { this._autoRotate = true; }, 3000));

    // Globe group — rotates as a unit
    this._globe = new THREE.Group();
    this._scene.add(this._globe);

    this._globe.add(this._buildOceanSphere());
    this._globe.add(this._buildCorona(RADIUS * 1.025, 0x0088ff, 0.22));
    this._globe.add(this._buildCorona(RADIUS * 1.07,  0x0055cc, 0.09));
    this._globe.add(this._buildCorona(RADIUS * 1.16,  0x003399, 0.04));

    this._scene.add(this._buildStars());
    this._scene.add(this._buildBokeh());
    this._scene.add(this._buildFloorReflection());

    this._orbit = this._buildOrbitalRings();
    this._scene.add(this._orbit);

    const ro = new ResizeObserver(() => this._onResize());
    ro.observe(this._canvas.parentElement);
  }

  // ── Static scene elements ───────────────────────────────────────────────────

  _buildOceanSphere() {
    return new THREE.Mesh(
      new THREE.SphereGeometry(RADIUS * 0.997, 64, 64),
      new THREE.MeshBasicMaterial({ color: 0x010c1a }),
    );
  }

  _buildCorona(radius, color, opacity) {
    return new THREE.Mesh(
      new THREE.SphereGeometry(radius, 32, 32),
      new THREE.MeshBasicMaterial({
        color, side: THREE.BackSide, transparent: true, opacity,
        blending: THREE.AdditiveBlending, depthWrite: false,
      }),
    );
  }

  _buildStars() {
    const n = 3000, pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const r = 15 + Math.random() * 20;
      const phi = Math.random() * Math.PI * 2, theta = Math.random() * Math.PI;
      pos[i*3]   = r * Math.sin(theta) * Math.cos(phi);
      pos[i*3+1] = r * Math.cos(theta);
      pos[i*3+2] = r * Math.sin(theta) * Math.sin(phi);
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    return new THREE.Points(geo, new THREE.PointsMaterial({
      size: 0.025, color: 0xaaccff,
      blending: THREE.AdditiveBlending, transparent: true, opacity: 0.5, depthWrite: false,
    }));
  }

  _buildBokeh() {
    const group    = new THREE.Group();
    const cyanTex  = this._makeGlowTexture("#00d4ff");
    const amberTex = this._makeGlowTexture("#ffb347");
    for (let i = 0; i < 80; i++) {
      const isAmber = i < 20;
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({
        map: isAmber ? amberTex : cyanTex,
        blending: THREE.AdditiveBlending, transparent: true, depthWrite: false,
        opacity: isAmber ? 0.06 + Math.random() * 0.10 : 0.07 + Math.random() * 0.13,
      }));
      const dist  = RADIUS * (1.6 + Math.random() * 2.4);
      const phi   = Math.random() * Math.PI * 2;
      const theta = Math.random() * Math.PI;
      sp.position.set(
        dist * Math.sin(theta) * Math.cos(phi),
        dist * Math.cos(theta),
        dist * Math.sin(theta) * Math.sin(phi),
      );
      sp.scale.setScalar(0.12 + Math.random() * 0.35);
      group.add(sp);
    }
    return group;
  }

  _buildFloorReflection() {
    const c = document.createElement("canvas");
    c.width = c.height = 256;
    const ctx = c.getContext("2d");
    const grd = ctx.createRadialGradient(128, 128, 0, 128, 128, 128);
    grd.addColorStop(0,   "rgba(0,140,255,0.13)");
    grd.addColorStop(0.6, "rgba(0,60,180,0.05)");
    grd.addColorStop(1,   "rgba(0,0,0,0)");
    ctx.fillStyle = grd;
    ctx.fillRect(0, 0, 256, 256);
    const plane = new THREE.Mesh(
      new THREE.PlaneGeometry(3.5, 3.5),
      new THREE.MeshBasicMaterial({
        map: new THREE.CanvasTexture(c),
        transparent: true, blending: THREE.AdditiveBlending,
        depthWrite: false, side: THREE.DoubleSide,
      }),
    );
    plane.rotation.x  = -Math.PI / 2;
    plane.position.y  = -RADIUS * 1.25;
    return plane;
  }

  _buildOrbitalRings() {
    const group = new THREE.Group();
    const r     = RADIUS * 1.28;
    const nodeTex = this._makeGlowTexture("#aaffff");

    const addRing = (tiltX, color, opacity, nodeCount) => {
      const ringGroup = new THREE.Group();
      ringGroup.rotation.x = tiltX;

      // Ring line
      const N = 128, pts = [];
      for (let i = 0; i <= N; i++) {
        const t = (i / N) * Math.PI * 2;
        pts.push(r * Math.cos(t), 0, r * Math.sin(t));
      }
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
      ringGroup.add(new THREE.LineLoop(geo, new THREE.LineBasicMaterial({
        color, transparent: true, opacity, blending: THREE.AdditiveBlending,
      })));

      // Node sprites at regular intervals
      for (let i = 0; i < nodeCount; i++) {
        const t  = (i / nodeCount) * Math.PI * 2;
        const sp = new THREE.Sprite(new THREE.SpriteMaterial({
          map: nodeTex, blending: THREE.AdditiveBlending, transparent: true,
          opacity: opacity * 1.4, depthWrite: false,
        }));
        sp.position.set(r * Math.cos(t), 0, r * Math.sin(t));
        sp.scale.setScalar(0.07);
        ringGroup.add(sp);
      }

      group.add(ringGroup);
    };

    // Primary — Earth's axial tilt
    addRing(23 * Math.PI / 180, 0xaaffff, 0.65, 12);
    // Secondary — different inclination, fainter
    addRing(62 * Math.PI / 180, 0x0055aa, 0.30,  8);

    return group;
  }

  // ── Async continent loader ──────────────────────────────────────────────────
  async _loadContinents() {
    try {
      const [topoMod, world] = await Promise.all([
        import("https://esm.sh/topojson-client@3"),
        fetch("https://cdn.jsdelivr.net/npm/world-atlas@2/land-110m.json").then(r => r.json()),
      ]);

      const land        = topoMod.feature(world, world.objects.land);
      const outlinePts  = [];   // coastline positions (flat)
      const outlineCols = [];   // coastline vertex colors
      const fillPts     = [];   // interior fill positions
      const fillCols    = [];   // interior fill vertex colors

      const cCoast  = new THREE.Color(0x00efff);
      const cAccent = new THREE.Color(0xccffff);
      const cFringe = new THREE.Color(0x9966ff);
      const cFill   = new THREE.Color(0x2255bb);

      const pip = (lon, lat, ring) => {
        let inside = false;
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
          const [xi, yi] = ring[i], [xj, yj] = ring[j];
          if (((yi > lat) !== (yj > lat)) && lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)
            inside = !inside;
        }
        return inside;
      };

      const pushColor = (arr, c) => arr.push(c.r, c.g, c.b);

      const processPolygon = (rings) => {
        const outer = rings[0];

        // ── Coastline outline ──────────────────────────────────────────────
        for (let i = 0; i < outer.length - 1; i++) {
          const [lon0, lat0] = outer[i], [lon1, lat1] = outer[i + 1];
          const steps = Math.max(1, Math.ceil(Math.hypot(lon1 - lon0, lat1 - lat0) / 0.3));
          for (let t = 0; t < steps; t++) {
            const v = this._latLonToXYZ(
              lat0 + (lat1 - lat0) * t / steps,
              lon0 + (lon1 - lon0) * t / steps,
              RADIUS * 1.001,
            );
            outlinePts.push(v.x, v.y, v.z);
            const rnd = Math.random();
            pushColor(outlineCols, rnd < 0.12 ? cFringe : rnd < 0.27 ? cAccent : cCoast);
          }
        }

        // ── Interior fill — random PIP scatter ────────────────────────────
        let minLon = Infinity, maxLon = -Infinity, minLat = Infinity, maxLat = -Infinity;
        for (const [ln, lt] of outer) {
          if (ln < minLon) minLon = ln; if (ln > maxLon) maxLon = ln;
          if (lt < minLat) minLat = lt; if (lt > maxLat) maxLat = lt;
        }
        const area     = (maxLon - minLon) * (maxLat - minLat);
        const nSamples = Math.min(500, Math.ceil(area * 0.5));
        for (let s = 0; s < nSamples; s++) {
          const lon = minLon + Math.random() * (maxLon - minLon);
          const lat = minLat + Math.random() * (maxLat - minLat);
          if (pip(lon, lat, outer)) {
            const v = this._latLonToXYZ(lat, lon, RADIUS * 0.998);
            fillPts.push(v.x, v.y, v.z);
            pushColor(fillCols, cFill);
          }
        }
      };

      for (const f of land.features) {
        const g = f.geometry;
        if (g.type === "Polygon")      processPolygon(g.coordinates);
        else if (g.type === "MultiPolygon") g.coordinates.forEach(p => processPolygon(p));
      }

      // ── Build ShaderMaterial ─────────────────────────────────────────────
      const uScale = this._camera.projectionMatrix.elements[5]
                   * this._canvas.clientHeight * 0.5;
      const mat = new THREE.ShaderMaterial({
        uniforms:       { uScale: { value: uScale } },
        vertexShader:   VERT,
        fragmentShader: FRAG,
        transparent:    true,
        blending:       THREE.AdditiveBlending,
        depthWrite:     false,
      });
      this._particleMat = mat;

      const addLayer = (positions, colors) => {
        const geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
        geo.setAttribute("color",    new THREE.Float32BufferAttribute(colors,    3));
        this._globe.add(new THREE.Points(geo, mat));
      };

      addLayer(outlinePts,  outlineCols);
      addLayer(fillPts,     fillCols);

      // ── Ocean haze (sparse dim scatter, same shader) ──────────────────
      const N_OCEAN = 900;
      const oPts = new Float32Array(N_OCEAN * 3);
      const oCols = new Float32Array(N_OCEAN * 3);
      const cOcean = new THREE.Color(0x001840);
      for (let i = 0; i < N_OCEAN; i++) {
        const phi   = Math.acos(2 * Math.random() - 1);
        const theta = Math.random() * Math.PI * 2;
        const r     = RADIUS * 0.997;
        oPts[i*3]   = -r * Math.sin(phi) * Math.cos(theta);
        oPts[i*3+1] =  r * Math.cos(phi);
        oPts[i*3+2] =  r * Math.sin(phi) * Math.sin(theta);
        oCols[i*3] = cOcean.r; oCols[i*3+1] = cOcean.g; oCols[i*3+2] = cOcean.b;
      }
      addLayer(oPts, oCols);

      // ── Surface wireframe — connect every 40th outline point ─────────
      const nodes = [];
      for (let i = 0; i < outlinePts.length; i += 3 * 40) {
        nodes.push(new THREE.Vector3(outlinePts[i], outlinePts[i+1], outlinePts[i+2]));
      }
      const wireVerts = [];
      const maxD      = RADIUS * 0.55;
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          if (nodes[i].distanceTo(nodes[j]) < maxD)
            wireVerts.push(nodes[i].x, nodes[i].y, nodes[i].z,
                           nodes[j].x, nodes[j].y, nodes[j].z);
        }
      }
      if (wireVerts.length) {
        const wGeo = new THREE.BufferGeometry();
        wGeo.setAttribute("position", new THREE.Float32BufferAttribute(wireVerts, 3));
        this._globe.add(new THREE.LineSegments(wGeo, new THREE.LineBasicMaterial({
          color: 0x004488, transparent: true, opacity: 0.18,
          blending: THREE.AdditiveBlending,
        })));
      }

    } catch (e) {
      console.warn("Continent load failed:", e);
    }
  }

  // ── Shared utilities ────────────────────────────────────────────────────────
  _makeGlowTexture(color) {
    const size = 128, c = document.createElement("canvas");
    c.width = c.height = size;
    const ctx = c.getContext("2d"), cx = size / 2;
    const grd = ctx.createRadialGradient(cx, cx, 0, cx, cx, cx);
    grd.addColorStop(0,   color + "ff");
    grd.addColorStop(0.3, color + "99");
    grd.addColorStop(0.7, color + "33");
    grd.addColorStop(1,   color + "00");
    ctx.fillStyle = grd;
    ctx.fillRect(0, 0, size, size);
    return new THREE.CanvasTexture(c);
  }

  _latLonToXYZ(lat, lon, r = RADIUS * 1.02) {
    const phi   = (90 - lat) * Math.PI / 180;
    const theta = (lon + 180) * Math.PI / 180;
    return new THREE.Vector3(
      -r * Math.sin(phi) * Math.cos(theta),
       r * Math.cos(phi),
       r * Math.sin(phi) * Math.sin(theta),
    );
  }

  // ── City markers (trade signals) ────────────────────────────────────────────
  updateCityMarkers(signals) {
    this._cityMarkers.forEach(m => this._globe.remove(m));
    this._cityMarkers   = [];
    this._hoveredMarker = null;

    const seen = new Set();
    for (const s of signals) {
      if (!s.quality_gate_passed) continue;
      const key = `${s.city}|${s.direction}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const color   = s.direction === "YES" ? "#00ff88" : "#ff4455";
      const sprite  = new THREE.Sprite(new THREE.SpriteMaterial({
        map: this._makeGlowTexture(color),
        blending: THREE.AdditiveBlending, transparent: true, depthWrite: false,
      }));
      sprite.position.copy(this._latLonToXYZ(s.lat, s.lon));
      const baseSize = 0.04 + Math.min(s.edge_pp, 0.5) * 0.12;
      sprite.scale.setScalar(baseSize);
      sprite.userData = { baseSize, startTime: Date.now(), color, signal: s };
      this._globe.add(sprite);
      this._cityMarkers.push(sprite);
    }
  }

  // ── Mouse interaction ───────────────────────────────────────────────────────
  _setupInteraction() {
    this._raycaster = new THREE.Raycaster();
    this._pointer   = new THREE.Vector2();

    this._canvas.addEventListener("pointermove", e => {
      const rect = this._canvas.getBoundingClientRect();
      this._pointer.set(
        ((e.clientX - rect.left) / rect.width)  *  2 - 1,
        ((e.clientY - rect.top)  / rect.height) * -2 + 1,
      );
      this._raycaster.setFromCamera(this._pointer, this._camera);
      const hit = this._raycaster.intersectObjects(this._cityMarkers)[0]?.object ?? null;
      if (hit !== this._hoveredMarker) {
        if (this._hoveredMarker) this._hoveredMarker.scale.setScalar(this._hoveredMarker.userData.baseSize);
        this._hoveredMarker = hit;
        if (hit) hit.scale.setScalar(hit.userData.baseSize * 1.6);
      }
      this._canvas.style.cursor = hit ? "pointer" : "";
    });

    this._canvas.addEventListener("click", e => {
      const rect = this._canvas.getBoundingClientRect();
      this._pointer.set(
        ((e.clientX - rect.left) / rect.width)  *  2 - 1,
        ((e.clientY - rect.top)  / rect.height) * -2 + 1,
      );
      this._raycaster.setFromCamera(this._pointer, this._camera);
      const hit = this._raycaster.intersectObjects(this._cityMarkers)[0];
      if (hit && this._clickCb) this._clickCb(hit.object.userData.signal);
    });
  }

  setClickCallback(fn) { this._clickCb = fn; }

  // ── Render loop ─────────────────────────────────────────────────────────────
  _animate() {
    requestAnimationFrame(() => this._animate());

    // ~87s per rotation at 60fps
    if (this._autoRotate) this._globe.rotation.y += 0.0012;

    // Orbit rings drift at half the globe speed
    if (this._orbit) this._orbit.rotation.y += 0.0006;

    // Pulse city markers (skip hovered)
    const t = Date.now();
    for (const m of this._cityMarkers) {
      if (m === this._hoveredMarker) continue;
      const phase = ((t - m.userData.startTime) % 2000) / 2000;
      m.scale.setScalar(m.userData.baseSize * (1 + 0.25 * Math.sin(phase * Math.PI * 2)));
    }

    this._controls.update();
    this._renderer.render(this._scene, this._camera);
  }

  // ── Resize ──────────────────────────────────────────────────────────────────
  _onResize() {
    const el = this._canvas.parentElement;
    const w  = el.clientWidth, h = el.clientHeight;
    this._camera.aspect = w / h;
    this._camera.updateProjectionMatrix();
    this._renderer.setSize(w, h, false);
    // Keep particle sizes correct at new viewport height
    if (this._particleMat) {
      this._particleMat.uniforms.uScale.value =
        this._camera.projectionMatrix.elements[5] * h * 0.5;
    }
  }
}
