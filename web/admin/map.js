/* God-view map. Draws only coordinates returned by GET /v1/admin/world-map.
   Marker centers are those coordinates. Screen padding, the Y flip, and marker
   radius are camera choices and are not reported as world positions. */
(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const REFRESH_MS = 4000;
  const PLAYER_COLORS = ["#58a6ff", "#3fb950", "#d29922", "#f778ba", "#bc8cff", "#ffa657", "#79c0ff", "#ff7b72", "#7ee787", "#d2a8ff"];
  const STATE_COLOR = {
    garrisoned: "#c9d1d9",
    marching: "#ff7b72",
    returning: "#d29922",
    destroyed: "#8b949e",
  };

  const state = {
    requestFn: null,
    currentView: "",
    loadGen: 0,
    timer: 0,
    paused: false,
    userMoved: false,
    playerFilter: "",
    selected: null,
    lastBody: null,
    lastError: "",
    fetchedAt: null,
    hits: [],
    pointers: new Map(),
    drag: null,
    pinch: null,
    suppressClick: false,
    view: { minX: 0, minY: -10, w: 16, h: 10 },
  };

  function el(tag, attrs) {
    const node = document.createElement(tag);
    const props = attrs || {};
    Object.keys(props).forEach((key) => {
      const value = props[key];
      if (value == null) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else node.setAttribute(key, String(value));
    });
    for (let i = 2; i < arguments.length; i += 1) {
      const kid = arguments[i];
      if (kid == null || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }

  function svgEl(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach((key) => {
      if (attrs[key] != null) node.setAttribute(key, String(attrs[key]));
    });
    return node;
  }

  function known(value) {
    if (value === undefined || value === null || value === "") return "UNKNOWN";
    return String(value);
  }

  function playerColor(playerId) {
    const n = Number(playerId);
    if (!Number.isInteger(n)) return "#8b949e";
    const index = ((n % PLAYER_COLORS.length) + PLAYER_COLORS.length) % PLAYER_COLORS.length;
    return PLAYER_COLORS[index];
  }

  function finitePoint(source) {
    if (!source) return null;
    const x = Number(source.x);
    const y = Number(source.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    return { x: x, y: y };
  }

  function root() {
    return document.getElementById("map-root");
  }

  function stage() {
    return document.getElementById("map-stage");
  }

  function svg() {
    return document.getElementById("map-svg");
  }

  function elementAspect() {
    const node = svg() || stage();
    if (!node) return 16 / 10;
    const rect = node.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return 16 / 10;
    return rect.width / rect.height;
  }

  function worldPerPixel() {
    const node = svg();
    const rect = node ? node.getBoundingClientRect() : null;
    if (!rect || rect.width < 2) return state.view.w / 800;
    return state.view.w / rect.width;
  }

  function fit(bounds) {
    const aspect = elementAspect();
    const numbers = bounds && [bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y].map(Number);
    if (!numbers || numbers.some((value) => !Number.isFinite(value))) {
      const spanY = 10;
      const spanX = spanY * aspect;
      state.view = { minX: -spanX / 2, minY: -spanY / 2, w: spanX, h: spanY };
      return;
    }
    const minX = numbers[0];
    const minY = numbers[1];
    const maxX = numbers[2];
    const maxY = numbers[3];
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    let spanX = Math.max(maxX - minX, 1) * 1.28;
    let spanY = Math.max(maxY - minY, 1) * 1.28;
    if (spanX / spanY < aspect) spanX = spanY * aspect;
    else spanY = spanX / aspect;
    state.view = {
      minX: cx - spanX / 2,
      minY: -(cy + spanY / 2),
      w: spanX,
      h: spanY,
    };
  }

  function syncAspect() {
    const aspect = elementAspect();
    const current = state.view.w / state.view.h;
    if (!Number.isFinite(current) || Math.abs(current - aspect) / aspect < 0.01) return;
    const cx = state.view.minX + state.view.w / 2;
    const cy = state.view.minY + state.view.h / 2;
    if (current < aspect) state.view.w = state.view.h * aspect;
    else state.view.h = state.view.w / aspect;
    state.view.minX = cx - state.view.w / 2;
    state.view.minY = cy - state.view.h / 2;
  }

  function query() {
    if (!state.playerFilter) return "";
    return "?player_id=" + encodeURIComponent(state.playerFilter);
  }

  function stopTimer() {
    if (state.timer) window.clearInterval(state.timer);
    state.timer = 0;
  }

  function startTimer() {
    stopTimer();
    if (state.paused || !state.requestFn || state.currentView !== "map") return;
    state.timer = window.setInterval(() => {
      if (state.requestFn) show(state.requestFn);
    }, REFRESH_MS);
  }

  function refreshStatus() {
    const node = document.getElementById("map-refresh-state");
    if (!node) return;
    node.textContent = state.paused ? "Auto-refresh paused." : "Auto-refresh every 4 seconds.";
  }

  function ensureShell() {
    const host = root();
    if (!host || document.getElementById("map-stage")) return;
    const pause = el("button", { type: "button", id: "map-pause", class: "ghost", text: "Pause auto-refresh" });
    const refresh = el("button", { type: "button", id: "map-refresh", class: "ghost", text: "Refresh now" });
    const zoomIn = el("button", { type: "button", id: "map-zoom-in", class: "ghost", text: "Zoom in" });
    const zoomOut = el("button", { type: "button", id: "map-zoom-out", class: "ghost", text: "Zoom out" });
    const fitBtn = el("button", { type: "button", id: "map-fit", class: "ghost", text: "Fit" });
    const filter = el("select", { id: "map-player-filter" });
    filter.append(el("option", { value: "", text: "All players" }));
    host.append(
      el(
        "div",
        { class: "map-toolbar" },
        el("label", { class: "map-filter" }, el("span", { class: "label", text: "Player" }), filter),
        pause,
        refresh,
        zoomIn,
        zoomOut,
        fitBtn
      ),
      el(
        "div",
        { class: "map-meta" },
        el("p", {}, el("span", { class: "label", text: "World time " }), el("span", { id: "map-world-time", class: "mono", text: "UNKNOWN" })),
        el("p", {}, el("span", { class: "label", text: "Last fetched " }), el("span", { id: "map-fetched", class: "mono", text: "UNKNOWN" })),
        el("p", { id: "map-refresh-state", class: "muted", text: "Auto-refresh every 4 seconds." }),
        el("p", { id: "map-source", class: "muted", text: "UNKNOWN" }),
        el("p", { id: "map-truncation", class: "map-warn", hidden: "hidden" }),
        el("p", { id: "map-error", class: "map-warn", hidden: "hidden" })
      ),
      el(
        "div",
        { class: "map-layout" },
        el(
          "div",
          { id: "map-stage", class: "map-stage" },
          svgEl("svg", { id: "map-svg", role: "img", "aria-label": "World map" })
        ),
        el(
          "aside",
          { class: "map-side" },
          el("div", { id: "map-detail" }),
          el("div", { id: "map-legend" }),
          el("div", { id: "map-lists" })
        )
      )
    );
    pause.addEventListener("click", () => {
      state.paused = !state.paused;
      pause.textContent = state.paused ? "Resume auto-refresh" : "Pause auto-refresh";
      refreshStatus();
      if (state.paused) stopTimer();
      else startTimer();
    });
    refresh.addEventListener("click", () => {
      if (state.requestFn) show(state.requestFn);
    });
    zoomIn.addEventListener("click", () => zoomCenter(0.8));
    zoomOut.addEventListener("click", () => zoomCenter(1.25));
    fitBtn.addEventListener("click", () => {
      state.userMoved = false;
      fit(state.lastBody && state.lastBody.bounds);
      paint();
    });
    filter.addEventListener("change", () => {
      state.playerFilter = filter.value;
      state.userMoved = false;
      state.selected = null;
      if (state.requestFn) show(state.requestFn);
    });
    const mapStage = stage();
    mapStage.addEventListener("pointerdown", onPointerDown);
    mapStage.addEventListener("pointermove", onPointerMove);
    mapStage.addEventListener("pointerup", onPointerUp);
    mapStage.addEventListener("pointercancel", onPointerUp);
    mapStage.addEventListener("wheel", onWheel, { passive: false });
    if (window.ResizeObserver) {
      const observer = new ResizeObserver(() => {
        const rect = mapStage.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2 || !state.lastBody) return;
        if (!state.userMoved) fit(state.lastBody.bounds);
        else syncAspect();
        paint();
      });
      observer.observe(mapStage);
    }
  }

  function zoomAt(clientX, clientY, factor) {
    const node = svg();
    if (!node) return;
    const rect = node.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    const px = (clientX - rect.left) / rect.width;
    const py = (clientY - rect.top) / rect.height;
    const anchorX = state.view.minX + px * state.view.w;
    const anchorY = state.view.minY + py * state.view.h;
    const nextW = clamp(state.view.w * factor, 0.25, 1000000);
    const nextH = clamp(state.view.h * factor, 0.25, 1000000);
    state.view.w = nextW;
    state.view.h = nextH;
    state.view.minX = anchorX - px * nextW;
    state.view.minY = anchorY - py * nextH;
    state.userMoved = true;
    syncAspect();
    paint();
  }

  function zoomCenter(factor) {
    const node = svg();
    if (!node) return;
    const rect = node.getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function onWheel(event) {
    event.preventDefault();
    zoomAt(event.clientX, event.clientY, event.deltaY > 0 ? 1.12 : 0.89);
  }

  function pointerDistance() {
    const points = Array.from(state.pointers.values());
    if (points.length < 2) return 0;
    return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
  }

  function pointerMid() {
    const points = Array.from(state.pointers.values());
    return {
      x: (points[0].x + points[1].x) / 2,
      y: (points[0].y + points[1].y) / 2,
    };
  }

  function onPointerDown(event) {
    const mapStage = stage();
    if (!mapStage) return;
    try {
      mapStage.setPointerCapture(event.pointerId);
    } catch (err) {
      /* Capture is unavailable for a pointer the browser has already released. */
    }
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (state.pointers.size === 2) {
      state.pinch = { dist: pointerDistance(), mid: pointerMid(), view: Object.assign({}, state.view) };
      state.drag = null;
      state.suppressClick = true;
      return;
    }
    state.drag = {
      x: event.clientX,
      y: event.clientY,
      minX: state.view.minX,
      minY: state.view.minY,
      moved: false,
    };
  }

  function applyPinch() {
    if (!state.pinch || state.pointers.size < 2) return;
    const node = svg();
    if (!node) return;
    const rect = node.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2 || state.pinch.dist < 1) return;
    const dist = pointerDistance();
    const mid = pointerMid();
    const scale = state.pinch.dist / dist;
    const px0 = (state.pinch.mid.x - rect.left) / rect.width;
    const py0 = (state.pinch.mid.y - rect.top) / rect.height;
    const anchorX = state.pinch.view.minX + px0 * state.pinch.view.w;
    const anchorY = state.pinch.view.minY + py0 * state.pinch.view.h;
    const px = (mid.x - rect.left) / rect.width;
    const py = (mid.y - rect.top) / rect.height;
    state.view.w = clamp(state.pinch.view.w * scale, 0.25, 1000000);
    state.view.h = clamp(state.pinch.view.h * scale, 0.25, 1000000);
    state.view.minX = anchorX - px * state.view.w;
    state.view.minY = anchorY - py * state.view.h;
    state.userMoved = true;
    syncAspect();
    paint();
  }

  function onPointerMove(event) {
    if (!state.pointers.has(event.pointerId)) return;
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (state.pointers.size >= 2) {
      state.suppressClick = true;
      applyPinch();
      return;
    }
    if (!state.drag) return;
    const dx = event.clientX - state.drag.x;
    const dy = event.clientY - state.drag.y;
    if (Math.hypot(dx, dy) > 5) state.drag.moved = true;
    const node = svg();
    if (!node) return;
    const rect = node.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    state.view.minX = state.drag.minX - (dx / rect.width) * state.view.w;
    state.view.minY = state.drag.minY - (dy / rect.height) * state.view.h;
    if (state.drag.moved) {
      state.userMoved = true;
      paint();
    }
  }

  function onPointerUp(event) {
    const dragged = state.drag && state.drag.moved;
    state.pointers.delete(event.pointerId);
    if (state.pointers.size < 2) state.pinch = null;
    if (state.pointers.size === 0) {
      if (!dragged && !state.suppressClick) selectAt(event.clientX, event.clientY);
      state.drag = null;
      state.suppressClick = false;
    }
  }

  function worldToScreen(x, y) {
    const node = svg();
    const rect = node.getBoundingClientRect();
    const svgX = x;
    const svgY = -y;
    return {
      x: rect.left + ((svgX - state.view.minX) / state.view.w) * rect.width,
      y: rect.top + ((svgY - state.view.minY) / state.view.h) * rect.height,
    };
  }

  function selectAt(clientX, clientY) {
    const found = [];
    state.hits.forEach((hit) => {
      let dist = Infinity;
      if (hit.kind === "movement" && hit.ax != null) {
        const a = worldToScreen(hit.ax, hit.ay);
        const b = worldToScreen(hit.bx, hit.by);
        dist = distanceToSegment(clientX, clientY, a.x, a.y, b.x, b.y);
      }
      if (hit.x != null) {
        const screen = worldToScreen(hit.x, hit.y);
        dist = Math.min(dist, Math.hypot(screen.x - clientX, screen.y - clientY));
      }
      const limit = hit.kind === "movement" ? 12 : 18;
      if (dist <= limit) found.push({ kind: hit.kind, id: hit.id, dist: dist });
    });
    found.sort((left, right) => left.dist - right.dist);
    if (!found.length) state.selected = null;
    else state.selected = { kind: found[0].kind, id: found[0].id, choices: found.slice(0, 6) };
    renderPanels();
    paint();
  }

  function distanceToSegment(px, py, ax, ay, bx, by) {
    const dx = bx - ax;
    const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    if (len2 === 0) return Math.hypot(px - ax, py - ay);
    const t = clamp(((px - ax) * dx + (py - ay) * dy) / len2, 0, 1);
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }

  function entity(kind, id) {
    const body = state.lastBody;
    if (!body) return null;
    const rows = kind === "city" ? body.cities : kind === "army" ? body.armies : body.movements;
    if (!Array.isArray(rows)) return null;
    return rows.find((row) => String(row.id) === String(id)) || null;
  }

  function renderAll() {
    ensureShell();
    syncPlayers();
    renderMeta();
    renderPanels();
    if (!state.userMoved && state.lastBody) fit(state.lastBody.bounds);
    paint();
  }

  function syncPlayers() {
    const select = document.getElementById("map-player-filter");
    const body = state.lastBody;
    if (!select || !body) return;
    const players = Array.isArray(body.players) ? body.players : [];
    const signature = players.map((player) => String(player.id) + ":" + known(player.name)).join("|");
    if (select.dataset.signature === signature) return;
    const current = state.playerFilter;
    select.replaceChildren(el("option", { value: "", text: "All players" }));
    players.forEach((player) => {
      select.append(el("option", { value: String(player.id), text: known(player.name) }));
    });
    select.dataset.signature = signature;
    if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
  }

  function renderMeta() {
    const body = state.lastBody;
    const world = document.getElementById("map-world-time");
    const fetched = document.getElementById("map-fetched");
    const source = document.getElementById("map-source");
    const truncation = document.getElementById("map-truncation");
    const error = document.getElementById("map-error");
    if (world) world.textContent = body && body.server_time != null ? String(body.server_time) : "UNKNOWN";
    if (fetched) fetched.textContent = state.fetchedAt ? state.fetchedAt.toLocaleString() : "UNKNOWN";
    if (source) {
      const coordinate = body && body.coordinate_source != null ? String(body.coordinate_source) : "UNKNOWN";
      const neutral = body && body.neutral_entities != null ? String(body.neutral_entities) : "UNKNOWN";
      const fog = body && body.fog_of_war != null ? String(body.fog_of_war) : "UNKNOWN";
      source.textContent = "Coordinates: " + coordinate + ". Neutral / NPC: " + neutral + ". Fog of war: " + fog + ".";
    }
    if (truncation) {
      const text = truncationText(body && body.limits);
      truncation.hidden = !text;
      truncation.textContent = text;
    }
    if (error) {
      error.hidden = !state.lastError;
      error.textContent = state.lastError ? "Refresh failed: " + state.lastError : "";
    }
    refreshStatus();
  }

  function truncationText(limits) {
    if (!limits) return "";
    const parts = [];
    ["cities", "armies", "movements", "players"].forEach((key) => {
      const row = limits[key];
      if (row && row.truncated) parts.push(key + " TRUNCATED " + row.returned + "/" + row.total);
    });
    return parts.join(" · ");
  }

  function renderPanels() {
    renderDetail();
    renderLegend();
    renderLists();
  }

  function traceNode(row) {
    if (!row) return el("span", { text: "UNKNOWN" });
    if (row.trace_state === "AVAILABLE" && row.trace_id) {
      return el("a", { href: "#trace/id/" + row.trace_id, text: String(row.trace_id) });
    }
    if (row.trace_state === "LEGACY") return el("span", { text: "LEGACY / NOT TRACED" });
    if (row.trace_state === "NONE") return el("span", { text: "NONE" });
    return el("span", { text: "UNKNOWN" });
  }

  function positionText(position) {
    const point = finitePoint(position);
    if (!point) return "UNKNOWN";
    const city = position && position.city_id != null ? " city " + position.city_id : "";
    return point.x + ", " + point.y + city;
  }

  function renderDetail() {
    const host = document.getElementById("map-detail");
    if (!host) return;
    host.replaceChildren();
    host.append(el("h3", { text: "Details" }));
    const selected = state.selected;
    if (!selected) {
      host.append(el("p", { class: "muted", text: "Tap a city, army, or movement." }));
      return;
    }
    if (selected.choices && selected.choices.length > 1) {
      const chooser = el("div", { class: "map-choices" });
      selected.choices.forEach((choice) => {
        const row = entity(choice.kind, choice.id);
        const label = choice.kind + " " + choice.id + (row && row.name ? " " + row.name : "");
        const button = el("button", { type: "button", class: "ghost", text: label });
        button.addEventListener("click", () => {
          state.selected = { kind: choice.kind, id: choice.id };
          renderPanels();
          paint();
        });
        chooser.append(button);
      });
      host.append(chooser);
    }
    const row = entity(selected.kind, selected.id);
    if (!row) {
      host.append(el("p", { text: "Not in the latest map response." }));
      return;
    }
    const pairs = detailPairs(selected.kind, row);
    const list = el("dl", { class: "kv" });
    pairs.forEach((pair) => {
      list.append(el("dt", { text: pair[0] }));
      list.append(el("dd", {}, pair[1] instanceof Node ? pair[1] : el("span", { text: pair[1] })));
    });
    host.append(list);
    host.append(el("div", { class: "links" }, ...detailLinks(selected.kind, row)));
  }

  function detailPairs(kind, row) {
    if (kind === "city") {
      return [
        ["Kind", "city"],
        ["Id", known(row.id)],
        ["Name", known(row.name)],
        ["Owner", known(row.player_name) + " (" + known(row.player_id) + ")"],
        ["Position", positionText(row)],
        ["Garrison", Array.isArray(row.garrison_army_ids) ? (row.garrison_army_ids.length ? row.garrison_army_ids.join(", ") : "EMPTY") : "UNKNOWN"],
        ["Trace", traceNode(row)],
      ];
    }
    if (kind === "army") {
      return [
        ["Kind", "army"],
        ["Id", known(row.id)],
        ["Name", known(row.name)],
        ["Owner", known(row.player_name) + " (" + known(row.player_id) + ")"],
        ["Status", known(row.status)],
        ["Position state", known(row.position_state)],
        ["Position", positionText(row.position)],
        ["Home city", known(row.home_city_id)],
        ["Location city", known(row.location_city_id)],
        ["Movement", known(row.movement_id)],
        ["Units", unitsText(row.units)],
        ["Trace", traceNode(row)],
      ];
    }
    return [
      ["Kind", "movement"],
      ["Id", known(row.id)],
      ["Mission", known(row.mission)],
      ["Status", known(row.status)],
      ["Army", known(row.army_name) + " (" + known(row.army_id) + ")"],
      ["Army status", known(row.army_status)],
      ["Owner", known(row.player_name) + " (" + known(row.player_id) + ")"],
      ["Progress %", known(row.progress_percent)],
      ["ETA seconds", known(row.eta_seconds)],
      ["Depart", known(row.depart_at)],
      ["Arrive", known(row.arrive_at)],
      ["Origin", positionText(row.origin)],
      ["Current", positionText(row.position)],
      ["Destination", positionText(row.destination)],
      ["Direction", row.direction ? known(row.direction.dx) + ", " + known(row.direction.dy) : "UNKNOWN"],
      ["Event", known(row.event_id)],
      ["Trace", traceNode(row)],
    ];
  }

  function detailLinks(kind, row) {
    const links = [];
    if (kind === "city") links.push(el("a", { href: "#city/" + row.id, text: "Open city" }));
    if (kind === "army") {
      links.push(el("a", { href: "#army/" + row.id, text: "Open army" }));
      if (row.home_city_id != null) links.push(el("a", { href: "#city/" + row.home_city_id, text: "Home city" }));
      if (row.location_city_id != null) links.push(el("a", { href: "#city/" + row.location_city_id, text: "Location city" }));
      if (row.movement_id != null) links.push(el("a", { href: "#movement/" + row.movement_id, text: "Open movement" }));
    }
    if (kind === "movement") {
      links.push(el("a", { href: "#movement/" + row.id, text: "Open movement" }));
      if (row.army_id != null) links.push(el("a", { href: "#army/" + row.army_id, text: "Open army" }));
      if (row.origin && row.origin.city_id != null) links.push(el("a", { href: "#city/" + row.origin.city_id, text: "Origin city" }));
      if (row.destination && row.destination.city_id != null) links.push(el("a", { href: "#city/" + row.destination.city_id, text: "Destination city" }));
    }
    if (row.trace_state === "AVAILABLE" && row.trace_id) {
      links.push(el("a", { href: "#trace/id/" + row.trace_id, text: "Event trace" }));
    }
    return links.length ? links : [el("span", { class: "muted", text: "No linked view." })];
  }

  function unitsText(units) {
    if (units == null) return "UNKNOWN";
    if (!Array.isArray(units) || !units.length) return "EMPTY";
    return units
      .map((stack) => known(stack && stack.type) + " " + known(stack && stack.count))
      .join(", ");
  }

  function renderLegend() {
    const host = document.getElementById("map-legend");
    if (!host) return;
    host.replaceChildren(el("h3", { text: "Legend" }));
    const body = state.lastBody;
    const players = body && Array.isArray(body.players) ? body.players : null;
    if (!players) {
      host.append(el("p", { text: "UNKNOWN" }));
      return;
    }
    if (!players.length) host.append(el("p", { class: "muted", text: "Players: EMPTY" }));
    else {
      const row = el("div", { class: "map-legend-row" });
      players.forEach((player) => {
        row.append(
          el(
            "span",
            { class: "map-key" },
            el("i", { class: "swatch", style: "background:" + playerColor(player.id) }),
            known(player.name)
          )
        );
      });
      host.append(row);
    }
    host.append(
      el(
        "div",
        { class: "map-legend-row" },
        legendState("garrisoned"),
        legendState("marching"),
        legendState("returning"),
        legendState("destroyed")
      )
    );
    host.append(
      el(
        "div",
        { class: "map-legend-row" },
        el("span", { class: "map-key", text: "Attack: solid arrow" }),
        el("span", { class: "map-key", text: "Move: dotted arrow" }),
        el("span", { class: "map-key", text: "Return: dashed arrow" })
      )
    );
    host.append(el("p", { class: "muted", text: "Color is the owner. The ring is the army status. Arrows point along the server direction. Grid lines are a camera guide, not entities." }));
  }

  function legendState(status) {
    return el(
      "span",
      { class: "map-key" },
      el("i", { class: "swatch ring", style: "border-color:" + (STATE_COLOR[status] || "#8b949e") }),
      status
    );
  }

  function renderLists() {
    const host = document.getElementById("map-lists");
    if (!host) return;
    const body = state.lastBody;
    host.replaceChildren();
    if (!body) {
      host.append(el("p", { text: "EMPTY" }));
      return;
    }
    host.append(sectionList("Cities", body.cities, "city", (row) => known(row.name) + " · " + known(row.player_name) + " · " + positionText(row)));
    host.append(sectionList("Armies", body.armies, "army", (row) => known(row.name) + " · " + known(row.status) + " · " + positionText(row.position)));
    host.append(
      sectionList(
        "Movements",
        body.movements,
        "movement",
        (row) => known(row.mission) + " · " + known(row.status) + " · " + known(row.progress_percent) + "% · " + positionText(row.position)
      )
    );
    const unplotted = Array.isArray(body.armies) ? body.armies.filter((row) => !finitePoint(row.position)) : [];
    const block = el("div", { class: "map-list" });
    block.append(el("h3", { text: "Not plotted" }));
    if (!unplotted.length) block.append(el("p", { class: "muted", text: "EMPTY" }));
    unplotted.forEach((row) => block.append(rowButton("army", row, known(row.name) + " · " + known(row.position_state))));
    host.append(block);
  }

  function sectionList(title, rows, kind, label) {
    const block = el("div", { class: "map-list" });
    block.append(el("h3", { text: title }));
    if (!Array.isArray(rows)) {
      block.append(el("p", { text: "UNKNOWN" }));
      return block;
    }
    if (!rows.length) {
      block.append(el("p", { class: "muted", text: "EMPTY" }));
      return block;
    }
    rows.forEach((row) => block.append(rowButton(kind, row, label(row))));
    return block;
  }

  function rowButton(kind, row, label) {
    const button = el("button", { type: "button", class: "map-row", text: label });
    if (state.selected && state.selected.kind === kind && String(state.selected.id) === String(row.id)) button.classList.add("active");
    button.addEventListener("click", () => {
      state.selected = { kind: kind, id: row.id };
      renderPanels();
      paint();
    });
    return button;
  }

  function paint() {
    const node = svg();
    if (!node) return;
    state.hits = [];
    const body = state.lastBody;
    node.setAttribute("viewBox", state.view.minX + " " + state.view.minY + " " + state.view.w + " " + state.view.h);
    node.setAttribute("preserveAspectRatio", "xMidYMid meet");
    node.replaceChildren();
    const plate = svgEl("rect", {
      x: state.view.minX,
      y: state.view.minY,
      width: state.view.w,
      height: state.view.h,
      fill: "transparent",
    });
    node.append(plate);
    if (!body) {
      node.append(centerLabel("EMPTY"));
      return;
    }
    const cities = Array.isArray(body.cities) ? body.cities : [];
    const armies = Array.isArray(body.armies) ? body.armies : [];
    const movements = Array.isArray(body.movements) ? body.movements : [];
    const plottable =
      cities.some((city) => finitePoint(city)) ||
      armies.some((army) => finitePoint(army.position)) ||
      movements.some((movement) => finitePoint(movement.origin) && finitePoint(movement.destination));
    if (!plottable) {
      node.append(centerLabel("EMPTY"));
      return;
    }
    node.append(grid());
    const defs = svgEl("defs");
    const colors = {};
    function marker(color) {
      const id = "map-arrow-" + color.slice(1);
      if (colors[id]) return id;
      colors[id] = true;
      const mark = svgEl("marker", {
        id: id,
        markerWidth: 6,
        markerHeight: 6,
        refX: 5,
        refY: 3,
        orient: "auto",
        markerUnits: "strokeWidth",
      });
      mark.append(svgEl("path", { d: "M0,0 L6,3 L0,6 Z", fill: color }));
      defs.append(mark);
      return id;
    }
    node.append(defs);
    const unit = worldPerPixel();
    movements.forEach((movement) => {
      const origin = finitePoint(movement.origin);
      const destination = finitePoint(movement.destination);
      if (!origin || !destination) return;
      const color = playerColor(movement.player_id);
      const line = svgEl("line", {
        x1: origin.x,
        y1: -origin.y,
        x2: destination.x,
        y2: -destination.y,
        stroke: color,
        "stroke-width": 2 * unit,
        "marker-end": "url(#" + marker(color) + ")",
        fill: "none",
      });
      const dash = dashFor(movement.mission);
      if (dash) line.setAttribute("stroke-dasharray", dash.map((part) => part * unit).join(" "));
      if (isSelected("movement", movement.id)) line.setAttribute("stroke-width", String(4 * unit));
      node.append(line);
      state.hits.push({
        kind: "movement",
        id: movement.id,
        ax: origin.x,
        ay: origin.y,
        bx: destination.x,
        by: destination.y,
        x: finitePoint(movement.position) ? finitePoint(movement.position).x : null,
        y: finitePoint(movement.position) ? finitePoint(movement.position).y : null,
      });
      const current = finitePoint(movement.position);
      const direction = movement.direction;
      if (current && direction && Number.isFinite(Number(direction.dx)) && Number.isFinite(Number(direction.dy))) {
        const dx = Number(direction.dx);
        const dy = Number(direction.dy);
        const len = Math.hypot(dx, dy);
        if (len > 0) {
          const back = 16 * unit;
          const tail = svgEl("line", {
            x1: current.x - (dx / len) * back,
            y1: -(current.y - (dy / len) * back),
            x2: current.x,
            y2: -current.y,
            stroke: color,
            "stroke-width": 2.5 * unit,
            "marker-end": "url(#" + marker(color) + ")",
          });
          node.append(tail);
        }
      }
    });
    cities.forEach((city) => {
      const point = finitePoint(city);
      if (!point) return;
      const size = 11 * unit;
      const mark = svgEl("rect", {
        x: point.x - size / 2,
        y: -point.y - size / 2,
        width: size,
        height: size,
        fill: playerColor(city.player_id),
        stroke: isSelected("city", city.id) ? "#ffffff" : "#0e1116",
        "stroke-width": (isSelected("city", city.id) ? 2.5 : 1) * unit,
      });
      node.append(mark);
      node.append(
        svgEl("text", {
          x: point.x,
          y: -point.y - 14 * unit,
          "text-anchor": "middle",
          fill: "#e6edf3",
          "font-size": 11 * unit,
        })
      );
      const label = node.lastChild;
      label.textContent = city.name == null || city.name === "" ? "UNKNOWN" : String(city.name);
      state.hits.push({ kind: "city", id: city.id, x: point.x, y: point.y });
    });
    armies.forEach((army) => {
      const point = finitePoint(army.position);
      if (!point) return;
      const radius = 6 * unit;
      const dot = svgEl("circle", {
        cx: point.x,
        cy: -point.y,
        r: radius,
        fill: playerColor(army.player_id),
        stroke: STATE_COLOR[army.status] || "#8b949e",
        "stroke-width": (isSelected("army", army.id) ? 3 : 2) * unit,
      });
      node.append(dot);
      state.hits.push({ kind: "army", id: army.id, x: point.x, y: point.y });
    });
  }

  function isSelected(kind, id) {
    return state.selected && state.selected.kind === kind && String(state.selected.id) === String(id);
  }

  function dashFor(mission) {
    if (mission === "return") return [8, 5];
    if (mission === "move") return [2, 4];
    if (mission === "attack") return null;
    return [1, 3];
  }

  function centerLabel(text) {
    const node = svgEl("text", {
      x: state.view.minX + state.view.w / 2,
      y: state.view.minY + state.view.h / 2,
      "text-anchor": "middle",
      fill: "#8b949e",
      "font-size": state.view.h / 12,
    });
    node.textContent = text;
    return node;
  }

  function grid() {
    const group = svgEl("g", { stroke: "#30363d", "stroke-width": worldPerPixel(), fill: "#8b949e" });
    const step = niceStep(state.view.w);
    const minX = state.view.minX;
    const maxX = state.view.minX + state.view.w;
    const minSvgY = state.view.minY;
    const maxSvgY = state.view.minY + state.view.h;
    const startX = Math.ceil(minX / step) * step;
    for (let x = startX; x <= maxX; x += step) {
      group.append(svgEl("line", { x1: x, y1: minSvgY, x2: x, y2: maxSvgY }));
    }
    const maxWorldY = -minSvgY;
    const minWorldY = -maxSvgY;
    const startY = Math.ceil(minWorldY / step) * step;
    for (let y = startY; y <= maxWorldY; y += step) {
      group.append(svgEl("line", { x1: minX, y1: -y, x2: maxX, y2: -y }));
    }
    return group;
  }

  function niceStep(span) {
    const raw = Math.max(span, 1e-6) / 8;
    const pow = Math.pow(10, Math.floor(Math.log10(raw)));
    const err = raw / pow;
    const nice = err >= 5 ? 5 : err >= 2 ? 2 : 1;
    return nice * pow;
  }

  async function show(request) {
    state.requestFn = request;
    const gen = ++state.loadGen;
    stopTimer();
    ensureShell();
    try {
      const body = await request("/v1/admin/world-map" + query());
      if (gen !== state.loadGen || state.currentView !== "map") return;
      state.lastBody = body;
      state.lastError = "";
      state.fetchedAt = new Date();
      renderAll();
    } catch (error) {
      if (gen !== state.loadGen || state.currentView !== "map") return;
      state.lastError = error && error.message ? error.message : "Request failed";
      renderAll();
    } finally {
      if (gen === state.loadGen && state.currentView === "map" && !state.paused) startTimer();
    }
  }

  function onView(name) {
    state.currentView = name;
    if (name !== "map") {
      state.loadGen += 1;
      stopTimer();
    }
  }

  window.SimcoreWorldMap = { show: show, onView: onView };
})();
