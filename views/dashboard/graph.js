/* 球面均匀排布与透视投影。相似连线使用索引中的真实分数。 */
(() => {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const colors = { app_experience: "#3b82f6", learning: "#8b5cf6", diary: "#10b981" };
  const make = (tag, attrs = {}) => {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
    return el;
  };
  const initial = () => ({ yaw: 0.35, pitch: -0.25, scale: 1, x: 0, y: 0 });
  function render(svg, data, onSelect) {
    const abort = new AbortController(), options = { signal: abort.signal };
    // Fibonacci 球面不依赖矩形边界，孤立节点与高密度图均匀覆盖球体。
    const nodes = data.nodes.map((node, i) => {
      const y = data.nodes.length === 1 ? 0 : 1 - 2 * (i + 0.5) / data.nodes.length;
      const radius = Math.sqrt(1 - y * y), angle = i * Math.PI * (3 - Math.sqrt(5));
      return { ...node, position: data.nodes.length === 1 ? [0, 0, 0] : [radius * Math.cos(angle), y, radius * Math.sin(angle)] };
    });
    const byId = new Map(nodes.map(node => [node.id, node]));
    const edges = data.edges.filter(edge => byId.has(edge.source) && byId.has(edge.target))
      .map(edge => ({ ...edge, a: byId.get(edge.source), b: byId.get(edge.target) }));
    svg.replaceChildren();
    const group = make("g"), guides = make("g", { "aria-hidden": "true", "pointer-events": "none" });
    const lines = make("g", { "aria-hidden": "true" }), points = make("g");
    group.append(guides, lines, points); svg.append(group);
    const rings = [];
    if (nodes.length > 1) {
      for (const latitude of [-Math.PI / 3, -Math.PI / 6, 0, Math.PI / 6, Math.PI / 3]) {
        rings.push(Array.from({ length: 73 }, (_, i) => {
          const a = i * 2 * Math.PI / 72;
          return [Math.cos(latitude) * Math.cos(a), Math.sin(latitude), Math.cos(latitude) * Math.sin(a)];
        }));
      }
      for (let j = 0; j < 6; j += 1) {
        rings.push(Array.from({ length: 73 }, (_, i) => {
          const a = i * 2 * Math.PI / 72, longitude = j * Math.PI / 6;
          return [Math.cos(a) * Math.cos(longitude), Math.sin(a), Math.cos(a) * Math.sin(longitude)];
        }));
      }
    }
    const ringElements = rings.map(() => {
      const path = make("path", { fill: "none", stroke: "#94a3b8", "stroke-width": 0.65, "stroke-opacity": 0.13 });
      guides.append(path); return path;
    });
    const edgeElements = edges.map(edge => {
      const path = make("path", { fill: "none", "stroke-linecap": "round", "pointer-events": "none" });
      const title = make("title"); title.textContent = `余弦相似度 ${edge.score.toFixed(3)}`;
      path.append(title); lines.append(path); return path;
    });
    let view = initial(), selected = null, gesture = null;
    const elements = new Map();
    function project(position) {
      const [x, y, z] = position, cy = Math.cos(view.yaw), sy = Math.sin(view.yaw);
      const cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
      const rx = x * cy + z * sy, rz = z * cy - x * sy;
      const ry = y * cp - rz * sp, depth = y * sp + rz * cp;
      const perspective = 720 / (720 - depth * 220);
      return { x: 500 + rx * 220 * perspective, y: 300 + ry * 220 * perspective, depth, perspective };
    }
    function select(node) {
      if (!node) return;
      selected = node.id; draw(); onSelect(node);
    }
    function draw() {
      group.setAttribute("transform", `translate(${500 + view.x} ${300 + view.y}) scale(${view.scale}) translate(-500 -300)`);
      ringElements.forEach((path, i) => path.setAttribute("d", rings[i].map((p, j) => {
        const q = project(p); return `${j ? "L" : "M"} ${q.x.toFixed(2)} ${q.y.toFixed(2)}`;
      }).join(" ") + " Z"));
      nodes.forEach(node => { node.projected = project(node.position); });
      edges.forEach((edge, i) => {
        const a = edge.a.projected, b = edge.b.projected;
        const active = edge.source === selected || edge.target === selected;
        const depth = (a.depth + b.depth + 2) / 4;
        const path = edgeElements[i];
        path.setAttribute("d", `M ${a.x} ${a.y} Q ${(a.x + b.x) / 2} ${(a.y + b.y) / 2} ${b.x} ${b.y}`);
        path.setAttribute("stroke", active ? colors[byId.get(selected).source] || "#64748b" : "#94a3b8");
        path.setAttribute("stroke-width", active ? 1.4 + edge.score : 0.5 + edge.score * 0.7);
        path.setAttribute("stroke-opacity", active ? 0.8 : (selected ? 0.04 : 0.06) + depth * 0.18);
      });
      const focused = document.activeElement;
      const labelIds = new Set([...nodes].sort((a, b) => b.projected.depth - a.projected.depth).slice(0, 16).map(n => n.id));
      // 先画远端节点；近端节点自然遮挡远端。标签仅展示前景及选中节点。
      [...nodes].sort((a, b) => a.projected.depth - b.projected.depth).forEach(node => {
        const { circle, label, holder } = elements.get(node.id), q = node.projected;
        const active = node.id === selected;
        const radius = (nodes.length > 80 ? 7 : nodes.length > 30 ? 9 : 12) * q.perspective;
        circle.setAttribute("cx", q.x); circle.setAttribute("cy", q.y);
        circle.setAttribute("r", radius + (active ? 2 : 0));
        circle.setAttribute("fill-opacity", active ? 1 : 0.38 + (q.depth + 1) * 0.31);
        circle.setAttribute("stroke", active ? "#f59e0b" : "#fff");
        circle.setAttribute("stroke-width", active ? 3 : 1.2);
        circle.setAttribute("aria-pressed", String(active));
        circle.setAttribute("data-depth", q.depth.toFixed(4));
        label.setAttribute("x", q.x); label.setAttribute("y", q.y + radius + 17);
        label.setAttribute("font-size", active ? 13 : 11);
        label.setAttribute("font-weight", active ? 600 : 400);
        label.setAttribute("display", active || (q.depth > 0 && labelIds.has(node.id)) ? "" : "none");
        points.append(holder);
      });
      if (focused && elements.size && points.contains(focused) && document.activeElement !== focused) focused.focus({ preventScroll: true });
    }
    nodes.forEach(node => {
      const holder = make("g");
      const circle = make("circle", { fill: colors[node.source] || "#64748b", tabindex: 0, role: "button", "aria-label": node.label, "aria-pressed": "false", "data-node-id": node.id });
      const title = make("title"); title.textContent = node.label; circle.append(title);
      const label = make("text", { "text-anchor": "middle", fill: "currentColor", "pointer-events": "none" });
      label.textContent = node.label.length > 20 ? node.label.slice(0, 20) + "…" : node.label;
      holder.append(circle, label); elements.set(node.id, { circle, label, holder });
      circle.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(node); }
      }, options);
    });
    function point(event) {
      const p = svg.createSVGPoint(); p.x = event.clientX; p.y = event.clientY;
      const matrix = svg.getScreenCTM(); return matrix ? p.matrixTransform(matrix.inverse()) : p;
    }
    function zoom(factor) { view.scale = Math.max(0.45, Math.min(2.5, view.scale * factor)); draw(); }
    function reset() { view = initial(); draw(); }
    svg.addEventListener("pointerdown", event => {
      if (event.button !== 0 || gesture) return;
      const p = point(event), target = event.target.closest("[data-node-id]");
      gesture = { id: event.pointerId, start: p, last: p, distance: 0, pan: event.shiftKey, node: target ? byId.get(target.dataset.nodeId) : null };
      svg.setPointerCapture(event.pointerId);
    }, options);
    svg.addEventListener("pointermove", event => {
      if (!gesture || event.pointerId !== gesture.id) return;
      const p = point(event), dx = p.x - gesture.last.x, dy = p.y - gesture.last.y;
      gesture.distance = Math.max(gesture.distance, Math.hypot(p.x - gesture.start.x, p.y - gesture.start.y));
      if (gesture.pan) { view.x += dx; view.y += dy; }
      else { view.yaw += dx * 0.008; view.pitch -= dy * 0.008; }
      gesture.last = p; draw();
    }, options);
    svg.addEventListener("pointerup", event => {
      if (!gesture || event.pointerId !== gesture.id) return;
      const completed = gesture; gesture = null;
      if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
      if (completed.distance < 5 && completed.node) select(completed.node);
    }, options);
    const cancel = () => { gesture = null; };
    svg.addEventListener("pointercancel", cancel, options); svg.addEventListener("lostpointercapture", cancel, options);
    svg.addEventListener("wheel", event => { event.preventDefault(); zoom(event.deltaY < 0 ? 1.1 : 1 / 1.1); }, { ...options, passive: false });
    svg.addEventListener("keydown", event => {
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
        event.preventDefault();
        view.yaw += event.key === "ArrowRight" ? 0.12 : event.key === "ArrowLeft" ? -0.12 : 0;
        view.pitch += event.key === "ArrowDown" ? -0.12 : event.key === "ArrowUp" ? 0.12 : 0; draw();
      } else if (event.key === "Home") { event.preventDefault(); reset(); }
      else if (["+", "=", "-"].includes(event.key)) { event.preventDefault(); zoom(event.key === "-" ? 1 / 1.1 : 1.1); }
    }, options);
    draw(); select(nodes[0]);
    return { zoom, reset, destroy: () => { abort.abort(); gesture = null; } };
  }
  window.PhoneMemoryGraph = { render };
})();
