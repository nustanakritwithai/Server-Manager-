(() => {
  const STORAGE_URL = "simcore.apiBaseUrl";
  const STORAGE_TOKEN = "simcore.adminToken";
  const STORAGE_SESSION = "simcore.adminSession";
  const REQUEST_MS = 25000;

  const state = {
    apiBaseUrl: "",
    token: "",
    sessionToken: "",
    view: "dashboard",
    worldTab: "players",
    selectedSnapshotId: null,
    auditOffset: 0,
  };
  let generation = 0;

  const $ = (id) => document.getElementById(id);

  function defaultApi() {
    const configured = window.SIMCORE_CONFIG && window.SIMCORE_CONFIG.apiBaseUrl;
    return String(configured || "").replace(/\/+$/, "");
  }

  function savedUrl() {
    return (localStorage.getItem(STORAGE_URL) || "").replace(/\/+$/, "");
  }

  function normalizeAdminToken(value) {
    return String(value || "")
      .replace(/[\u200B-\u200D\uFEFF]/g, "")
      .replace(/\u00A0/g, "")
      .replace(/[\r\n]/g, "")
      .trim();
  }

  function savedToken() {
    try {
      return normalizeAdminToken(localStorage.getItem(STORAGE_TOKEN) || "");
    } catch {
      return "";
    }
  }

  function persistToken(token) {
    const clean = normalizeAdminToken(token);
    try {
      if (clean) localStorage.setItem(STORAGE_TOKEN, clean);
      else localStorage.removeItem(STORAGE_TOKEN);
      return true;
    } catch {
      return false;
    }
  }

  function savedSession() {
    try {
      return localStorage.getItem(STORAGE_SESSION) || "";
    } catch {
      return "";
    }
  }

  function persistSession(sessionValue) {
    const stay = $("stay-signed-in").checked;
    try {
      if (sessionValue && stay) localStorage.setItem(STORAGE_SESSION, sessionValue);
      else localStorage.removeItem(STORAGE_SESSION);
      return true;
    } catch {
      return false;
    }
  }

  function signedIn() {
    return Boolean(state.sessionToken || state.token);
  }

  function sessionExpiryText(token) {
    const parts = String(token || "").split(".");
    if (parts.length !== 3 || parts[0] !== "simadm1") return "";
    try {
      const padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
      const extra = padded.length % 4 === 0 ? "" : "=".repeat(4 - (padded.length % 4));
      const payload = JSON.parse(atob(padded + extra));
      if (!payload || !payload.exp) return "";
      return "Session expires " + new Date(payload.exp * 1000).toLocaleString();
    } catch {
      return "";
    }
  }

  function showExpiry(token) {
    $("session-expiry").textContent = sessionExpiryText(token);
  }

  function h(tag, props) {
    const node = document.createElement(tag);
    const attrs = props || {};
    Object.keys(attrs).forEach((key) => {
      const value = attrs[key];
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

  function showError(error) {
    const node = $("global-error");
    node.hidden = false;
    node.textContent = error && error.message ? error.message : "Request failed";
  }

  function clearError() {
    const node = $("global-error");
    node.hidden = true;
    node.textContent = "";
  }

  function setSessionStatus(text, kind) {
    const node = $("session-status");
    node.textContent = text;
    node.className = "status" + (kind ? " " + kind : "");
  }

  function probeText(value) {
    if (value === undefined || value === null || value === "") return "UNKNOWN";
    return String(value);
  }

  function probeKind(value) {
    const text = probeText(value);
    if (text === "ok" || text === "READY" || text === "PASS" || text === "success") return "ok";
    if (text === "degraded" || text === "FAILED" || text === "failed" || text === "FAIL" || text === "failure") return "bad";
    if (text === "NOT INSTRUMENTED" || text === "UNKNOWN" || text === "INCOMPLETE" || text === "NOT CHECKED" || text === "LEGACY" || text === "NOT TRACED") return "warn";
    return "";
  }

  function stat(label, value) {
    const text = probeText(value);
    return h("div", { class: "stat " + probeKind(text) }, h("div", { class: "stat-label", text: label }), h("div", { class: "stat-value", text: text }));
  }

  function blank(value) {
    if (value === undefined || value === null || value === "") return "—";
    return String(value);
  }

  function idLink(kind, id, label) {
    if (id === undefined || id === null || id === "") return h("span", { text: "—" });
    return h("a", { href: "#" + kind + "/" + id, text: label == null ? String(id) : String(label) });
  }

  function traceLink(traceId) {
    if (traceId === undefined || traceId === null || traceId === "") {
      return h("span", { class: "badge warn", text: "LEGACY / NOT TRACED" });
    }
    return h("a", { href: "#trace/id/" + traceId, text: String(traceId) });
  }

  function verdictClass(verdict) {
    const text = verdict == null || verdict === "" ? "unknown" : String(verdict).toLowerCase().replace(/\s+/g, "-");
    return "verdict-banner verdict-" + text;
  }

  function raw(value) {
    const pre = h("pre", { class: "raw" });
    pre.textContent = JSON.stringify(value, null, 2);
    return pre;
  }

  function kv(pairs) {
    const dl = h("dl", { class: "kv" });
    pairs.forEach((pair) => {
      const value = pair[1];
      dl.append(h("dt", { text: pair[0] }));
      dl.append(h("dd", {}, value instanceof Node ? value : h("span", { text: blank(value) })));
    });
    return dl;
  }

  function badge(value) {
    const text = blank(value);
    return h("span", { class: "badge " + probeKind(value), text: text });
  }

  function renderTable(target, columns, rows, empty) {
    target.replaceChildren();
    if (!rows || !rows.length) {
      target.append(h("p", { class: "muted", text: empty || "No rows." }));
      return;
    }
    const table = h("table");
    const head = h("tr");
    columns.forEach((column) => head.append(h("th", { text: column.label })));
    table.append(h("thead", {}, head));
    const body = h("tbody");
    rows.forEach((row) => {
      const tr = h("tr");
      columns.forEach((column) => {
        const cell = column.cell(row);
        tr.append(h("td", {}, cell instanceof Node ? cell : h("span", { text: cell == null ? "" : String(cell) })));
      });
      body.append(tr);
    });
    table.append(body);
    target.append(h("div", { class: "scroll" }, table));
  }

  function showResult(id, value) {
    const node = $(id);
    node.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  async function request(path, options) {
    const opts = options || {};
    if (!state.apiBaseUrl) {
      throw new Error("Set the API URL");
    }
    const headers = new Headers(opts.headers || {});
    if (opts.json !== undefined) headers.set("Content-Type", "application/json");
    if (opts.admin !== false) {
      const token = normalizeAdminToken(state.token);
      if (state.sessionToken) headers.set("Authorization", "Bearer " + state.sessionToken);
      if (token) headers.set("X-Admin-Token", token);
      if (!state.sessionToken && !token) throw new Error("Sign in with the admin password or enter the admin token");
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REQUEST_MS);
    try {
      const response = await fetch(state.apiBaseUrl + path, {
        method: opts.method || "GET",
        headers,
        body: opts.json !== undefined ? JSON.stringify(opts.json) : undefined,
        signal: controller.signal,
      });
      const text = await response.text();
      let body = null;
      if (text) {
        try {
          body = JSON.parse(text);
        } catch {
          body = { raw: text };
        }
      }
      if (!response.ok) {
        const message =
          body && body.error && body.error.message
            ? body.error.code + ": " + body.error.message
            : "HTTP " + response.status;
        const error = new Error(message);
        error.status = response.status;
        error.body = body;
        throw error;
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Request timed out");
      if (error instanceof TypeError) throw new Error("Cannot reach " + state.apiBaseUrl);
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function query(params) {
    const search = new URLSearchParams();
    Object.keys(params).forEach((key) => {
      const value = params[key];
      if (value !== undefined && value !== null && String(value) !== "") search.set(key, String(value));
    });
    const text = search.toString();
    return text ? "?" + text : "";
  }

  async function loadHealth() {
    let api = "UNKNOWN";
    let db = "UNKNOWN";
    try {
      const health = await request("/health", { admin: false });
      api = health && health.status ? health.status : "UNKNOWN";
    } catch {
      api = "UNKNOWN";
    }
    try {
      const ready = await request("/health/ready", { admin: false });
      db = ready && ready.status ? ready.status : "UNKNOWN";
    } catch (error) {
      db = error.body && error.body.status ? error.body.status : "UNKNOWN";
    }
    return { api, db };
  }

  async function loadDashboard(gen) {
    const health = await loadHealth();
    if (gen !== generation) return;
    $("dash-health").replaceChildren(
      stat("API /health", health.api),
      stat("DB /health/ready", health.db)
    );
    if (!signedIn()) {
      $("dash-counts").replaceChildren(stat("Players", "UNKNOWN"), stat("Cities", "UNKNOWN"), stat("Armies", "UNKNOWN"), stat("Active movements", "UNKNOWN"));
      $("dash-events").replaceChildren(stat("Events", "UNKNOWN"));
      $("dash-world").replaceChildren(h("p", { class: "muted", text: "Sign in to load world counts." }));
      $("dash-snapshot").replaceChildren(stat("Latest snapshot", "UNKNOWN"));
      $("dash-gaps").replaceChildren(
        stat("Worker heartbeat", "UNKNOWN"),
        stat("Host CPU", "UNKNOWN"),
        stat("Host memory", "UNKNOWN"),
        stat("Host disk", "UNKNOWN")
      );
      return;
    }
    const dash = await request("/v1/admin/dashboard");
    if (gen !== generation) return;
    const counts = dash.counts || {};
    const events = dash.events || {};
    const world = dash.world || {};
    $("dash-counts").replaceChildren(
      stat("Server time", dash.server_time),
      stat("Offset seconds", dash.offset_seconds),
      stat("World version", world.world_version),
      stat("Players", counts.players),
      stat("Cities", counts.cities),
      stat("Armies", counts.armies),
      stat("Active movements", counts.active_movements)
    );
    $("dash-events").replaceChildren(
      stat("Events pending", events.pending),
      stat("Events processing", events.processing),
      stat("Events completed", events.completed),
      stat("Events failed", events.failed),
      stat("Events cancelled", events.cancelled)
    );
    $("dash-world").replaceChildren(
      h("div", { class: "links" },
        h("span", { text: "Commands " }),
        badge(world.commands_open),
        h("span", { text: " Worker paused " }),
        badge(world.worker_paused),
        h("span", { text: " Restore active " }),
        badge(world.restore_active)
      )
    );
    const snapSlot = $("dash-snapshot");
    snapSlot.replaceChildren();
    if (!Object.prototype.hasOwnProperty.call(dash, "latest_snapshot")) {
      snapSlot.append(stat("Latest snapshot", "UNKNOWN"));
    } else if (dash.latest_snapshot == null) {
      snapSlot.append(stat("Latest snapshot", "none"));
    } else {
      const snap = dash.latest_snapshot;
      snapSlot.append(
        h("div", { class: "stat" },
          h("div", { class: "stat-label", text: "Latest snapshot" }),
          h("div", { class: "stat-value" }, idLink("snapshots", snap.snapshot_id, "#" + snap.snapshot_id + " " + snap.status)),
          h("div", { class: "muted", text: blank(snap.checksum) })
        )
      );
    }
    const gaps = dash.uninstrumented || {};
    $("dash-gaps").replaceChildren(
      stat("Worker heartbeat", gaps.worker_heartbeat),
      stat("Host CPU", gaps.host_cpu),
      stat("Host memory", gaps.host_memory),
      stat("Host disk", gaps.host_disk)
    );
  }

  function syncRestore() {
    const typed = ($("restore-typed").value || "").trim();
    const ack = $("restore-ack").checked;
    const id = state.selectedSnapshotId;
    $("restore-go").disabled = !(id && ack && typed === String(id));
  }

  async function loadSnapshots() {
    const body = await request("/v1/admin/snapshots?limit=200");
    renderTable(
      $("snap-table"),
      [
        { label: "Id", cell: (row) => idLink("snapshots", row.snapshot_id) },
        { label: "Status", cell: (row) => badge(row.status) },
        { label: "Reason", cell: (row) => row.reason },
        { label: "Checksum", cell: (row) => row.checksum },
        { label: "World time", cell: (row) => row.world_time },
        { label: "Created", cell: (row) => row.created_at },
        { label: "Summary", cell: (row) => JSON.stringify(row.summary) },
      ],
      body.snapshots || [],
      "No snapshots."
    );
  }

  async function inspectSnapshot(id) {
    const body = await request("/v1/admin/snapshots/" + id + "/inspect");
    state.selectedSnapshotId = body.snapshot_id;
    $("restore-ack").checked = false;
    $("restore-typed").value = "";
    syncRestore();
    const slot = $("snap-inspect");
    slot.replaceChildren(
      h("h3", { text: "Inspect snapshot " + body.snapshot_id }),
      kv([
        ["status", badge(body.status)],
        ["reason", body.reason],
        ["checksum", body.checksum],
        ["payload_checksum", body.payload_checksum],
        ["checksum_ok", badge(body.checksum_ok)],
        ["canonical_ok", badge(body.canonical_ok)],
        ["summary_ok", badge(body.summary_ok)],
        ["schema_version", body.schema_version],
        ["world_version", body.world_version],
        ["world_time", body.world_time],
        ["created_at", body.created_at],
        ["error", body.error],
      ]),
      h("h4", { text: "Counts" }),
      raw(body.counts),
      h("h4", { text: "Stored summary" }),
      raw(body.summary),
      h("h4", { text: "World state in payload" }),
      raw(body.world_state)
    );
  }

  async function createSnapshot() {
    const body = await request("/v1/admin/snapshots", { method: "POST", json: { reason: "MANUAL" } });
    showResult("snap-result", body);
    await loadSnapshots();
  }

  async function restoreSnapshot(event) {
    event.preventDefault();
    const id = state.selectedSnapshotId;
    const typed = ($("restore-typed").value || "").trim();
    if (!$("restore-ack").checked || !id || typed !== String(id)) {
      showResult("snap-result", "Restore was not sent. Inspect a snapshot, check the warning, and type that snapshot id.");
      return;
    }
    const body = await request("/v1/admin/snapshots/" + id + "/restore", {
      method: "POST",
      json: { confirm: true },
    });
    showResult("snap-result", body);
    state.selectedSnapshotId = null;
    $("restore-ack").checked = false;
    $("restore-typed").value = "";
    syncRestore();
    await loadSnapshots();
  }

  function eventColumns() {
    return [
      { label: "Id", cell: (row) => idLink("events", row.id) },
      { label: "Type", cell: (row) => row.type },
      { label: "Status", cell: (row) => badge(row.status) },
      { label: "Due", cell: (row) => row.due_at },
      { label: "Attempts", cell: (row) => row.attempts },
      { label: "Processed", cell: (row) => blank(row.processed_at) },
      { label: "Locked by", cell: (row) => blank(row.locked_by) },
      { label: "Last error", cell: (row) => blank(row.last_error) },
      { label: "Idempotency", cell: (row) => row.idempotency_key },
      { label: "Movement", cell: (row) => idLink("movement", row.movement_id) },
      { label: "Trace", cell: (row) => traceLink(row.trace_id) },
    ];
  }

  function renderEventDetail(body) {
    const event = body.event;
    const slot = $("event-detail");
    const tx = body.transactions || [];
    slot.replaceChildren(
      h("h3", { text: "Event " + event.id }),
      kv([
        ["type", event.type],
        ["status", badge(event.status)],
        ["due_at", event.due_at],
        ["attempts", event.attempts],
        ["processed_at", event.processed_at],
        ["locked_by", event.locked_by],
        ["locked_at", event.locked_at],
        ["last_error", event.last_error],
        ["idempotency_key", event.idempotency_key],
        ["created_at", event.created_at],
        ["movement", idLink("movement", event.movement_id)],
        ["army", body.army ? idLink("army", body.army.id, body.army.name + " #" + body.army.id) : "—"],
        ["player", body.army ? idLink("player", body.army.player_id) : "—"],
        ["battle", body.battle ? idLink("battles", body.battle.id, "report " + body.battle.id + " · " + body.battle.winner) : "—"],
        ["trace", traceLink(event.trace_id)],
      ]),
      h("h4", { text: "Payload" }),
      raw(event.payload),
      h("h4", { text: "Transactions for this event" }),
      h("p", {}, h("a", {
        href: "#ledger",
        text: "Open ledger for event " + event.id,
        id: "event-ledger-link",
      }))
    );
    $("event-ledger-link").addEventListener("click", () => {
      $("ledger-event").value = String(event.id);
      $("ledger-player").value = "";
      $("ledger-city").value = "";
      $("ledger-resource").value = "";
    });
    const txHost = h("div");
    slot.append(txHost);
    renderTable(
      txHost,
      [
        { label: "Id", cell: (row) => row.id },
        { label: "Player", cell: (row) => idLink("player", row.player_id) },
        { label: "City", cell: (row) => idLink("city", row.city_id) },
        { label: "Resource", cell: (row) => row.resource },
        { label: "Delta", cell: (row) => row.delta },
        { label: "Balance", cell: (row) => row.balance_after },
        { label: "Reason", cell: (row) => row.reason },
        { label: "Key", cell: (row) => row.idempotency_key },
      ],
      tx,
      "No ledger rows for this event."
    );
  }

  async function loadEvents(focusId) {
    const status = $("event-status").value;
    const body = await request("/v1/admin/events" + query({ status: status, limit: 200 }));
    renderTable($("event-table"), eventColumns(), body.events || [], "No events.");
    if (focusId) renderEventDetail(await request("/v1/admin/events/" + focusId));
    else $("event-detail").replaceChildren();
  }

  function movementColumns() {
    return [
      { label: "Id", cell: (row) => idLink("movement", row.id) },
      { label: "Army", cell: (row) => idLink("army", row.army_id) },
      { label: "Mission", cell: (row) => row.mission },
      { label: "Status", cell: (row) => badge(row.status) },
      { label: "Depart", cell: (row) => row.depart_at },
      { label: "Arrive", cell: (row) => row.arrive_at },
      { label: "Origin city", cell: (row) => idLink("city", row.origin_city_id) },
      { label: "Dest city", cell: (row) => idLink("city", row.destination_city_id) },
      { label: "Cause event", cell: (row) => idLink("events", row.cause_event_id) },
      { label: "Resolved", cell: (row) => blank(row.resolved_at) },
      { label: "Trace", cell: (row) => traceLink(row.trace_id) },
    ];
  }

  async function loadWorld(entity, id) {
    $("movement-filter").hidden = state.worldTab !== "movements";
    const list = $("world-list");
    const detail = $("world-detail");
    detail.replaceChildren();
    if (state.worldTab === "players") {
      const body = await request("/v1/admin/players");
      renderTable(
        list,
        [
          { label: "Id", cell: (row) => idLink("player", row.id) },
          { label: "Name", cell: (row) => row.name },
          { label: "Cities", cell: (row) => (row.city_ids || []).join(", ") || "—" },
          { label: "Armies", cell: (row) => (row.army_ids || []).join(", ") || "—" },
          { label: "Created", cell: (row) => row.created_at },
        ],
        body.players || [],
        "No players."
      );
    } else if (state.worldTab === "cities") {
      const body = await request("/v1/admin/cities");
      renderTable(
        list,
        [
          { label: "Id", cell: (row) => idLink("city", row.id) },
          { label: "Name", cell: (row) => row.name },
          { label: "Player", cell: (row) => idLink("player", row.player_id, row.player_name || row.player_id) },
          { label: "X", cell: (row) => row.x },
          { label: "Y", cell: (row) => row.y },
          { label: "Wood", cell: (row) => row.wood },
          { label: "Food", cell: (row) => row.food },
          { label: "Iron", cell: (row) => row.iron },
          { label: "Gold", cell: (row) => row.gold },
          { label: "Garrison", cell: (row) => (row.garrison_army_ids || []).join(", ") || "—" },
          { label: "Updated", cell: (row) => row.last_updated },
        ],
        body.cities || [],
        "No cities."
      );
    } else if (state.worldTab === "armies") {
      const body = await request("/v1/admin/armies");
      renderTable(
        list,
        [
          { label: "Id", cell: (row) => idLink("army", row.id) },
          { label: "Name", cell: (row) => row.name },
          { label: "Player", cell: (row) => idLink("player", row.player_id) },
          { label: "Status", cell: (row) => badge(row.status) },
          { label: "Home", cell: (row) => idLink("city", row.home_city_id) },
          { label: "Location", cell: (row) => idLink("city", row.location_city_id) },
          { label: "Units", cell: (row) => JSON.stringify(row.units) },
          { label: "Movement", cell: (row) => (row.movement ? idLink("movement", row.movement.movement_id) : "—") },
        ],
        body.armies || [],
        "No armies."
      );
    } else {
      const status = $("movement-status").value;
      const body = await request("/v1/admin/movements" + query({ status: status, limit: 200 }));
      renderTable(list, movementColumns(), body.movements || [], "No movements.");
    }
    if (!entity || !id) return;
    if (entity === "player") {
      const body = await request("/v1/admin/players/" + id);
      detail.append(
        h("h3", { text: body.player.name + " #" + body.player.id }),
        kv([
          ["research", JSON.stringify(body.player.research)],
          ["created_at", body.player.created_at],
          ["balances", body.resource_balances],
          ["traces", h("a", { href: "#trace/player/" + body.player.id, text: "Find traces" })],
        ])
      );
      const cities = h("div");
      const armies = h("div");
      detail.append(h("h4", { text: "Cities" }), cities, h("h4", { text: "Armies" }), armies);
      renderTable(
        cities,
        [
          { label: "Id", cell: (row) => idLink("city", row.id, row.name) },
          { label: "Wood", cell: (row) => row.wood },
          { label: "Food", cell: (row) => row.food },
          { label: "Iron", cell: (row) => row.iron },
          { label: "Gold", cell: (row) => row.gold },
        ],
        body.cities || []
      );
      renderTable(
        armies,
        [
          { label: "Id", cell: (row) => idLink("army", row.id, row.name) },
          { label: "Status", cell: (row) => row.status },
          { label: "Home", cell: (row) => idLink("city", row.home_city_id) },
        ],
        body.armies || []
      );
    } else if (entity === "city") {
      const body = await request("/v1/admin/cities/" + id);
      const city = body.city;
      detail.append(
        h("h3", { text: city.name + " #" + city.id }),
        kv([
          ["player", body.player ? idLink("player", body.player.id, body.player.name) : "—"],
          ["x", city.x],
          ["y", city.y],
          ["wood", city.wood],
          ["food", city.food],
          ["iron", city.iron],
          ["gold", city.gold],
          ["rates", [city.wood_rate, city.food_rate, city.iron_rate, city.gold_rate].join(" / ")],
          ["last_updated", city.last_updated],
          ["balances", body.resource_balances],
          ["buildings", JSON.stringify(city.buildings)],
        ]),
        h("h4", { text: "Home armies" }),
        h("div", { class: "links" }, ...(body.home_army_ids || []).map((armyId) => idLink("army", armyId))),
        h("h4", { text: "Movements" }),
        h("div", { class: "links" }, ...(body.movement_ids || []).map((movementId) => idLink("movement", movementId)))
      );
    } else if (entity === "army") {
      const body = await request("/v1/admin/armies/" + id);
      const army = body.army;
      detail.append(
        h("h3", { text: army.name + " #" + army.id }),
        kv([
          ["player", body.player ? idLink("player", body.player.id, body.player.name) : idLink("player", army.player_id)],
          ["status", army.status],
          ["home", idLink("city", army.home_city_id)],
          ["location", idLink("city", army.location_city_id)],
          ["units", JSON.stringify(army.units)],
          ["position", JSON.stringify(army.position)],
          ["traces", h("a", { href: "#trace/army/" + army.id, text: "Find traces" })],
        ])
      );
      const moves = h("div");
      detail.append(h("h4", { text: "Movements" }), moves);
      renderTable(moves, movementColumns(), body.movements || []);
    } else if (entity === "movement") {
      const body = await request("/v1/admin/movements/" + id);
      const movement = body.movement;
      detail.append(
        h("h3", { text: "Movement " + movement.id }),
        kv([
          ["army", body.army ? idLink("army", body.army.id, body.army.name) : idLink("army", movement.army_id)],
          ["mission", movement.mission],
          ["status", movement.status],
          ["depart_at", movement.depart_at],
          ["arrive_at", movement.arrive_at],
          ["origin", idLink("city", movement.origin_city_id)],
          ["destination", idLink("city", movement.destination_city_id)],
          ["origin_xy", movement.origin_x + ", " + movement.origin_y],
          ["destination_xy", movement.destination_x + ", " + movement.destination_y],
          ["relocate", String(movement.relocate)],
          ["loot", [movement.loot_wood, movement.loot_food, movement.loot_iron, movement.loot_gold].join(" / ")],
          ["cause_event", idLink("events", movement.cause_event_id)],
          ["resolved_at", movement.resolved_at],
          ["battle", body.battle ? idLink("battles", body.battle.id, "report " + body.battle.id) : "—"],
          ["trace", traceLink(movement.trace_id)],
        ])
      );
      const events = h("div");
      detail.append(h("h4", { text: "Events" }), events);
      renderTable(events, eventColumns(), body.events || []);
    }
  }

  function renderBattleDetail(body) {
    const report = body.report;
    const slot = $("battle-detail");
    slot.replaceChildren(
      h("h3", { text: "Report " + report.id }),
      kv([
        ["event", body.event ? idLink("events", body.event.id) : idLink("events", report.event_id)],
        ["movement", body.movement ? idLink("movement", body.movement.id) : idLink("movement", report.movement_id)],
        ["attacker player", idLink("player", report.attacker_player_id)],
        ["defender player", idLink("player", report.defender_player_id)],
        ["attacker army", idLink("army", report.attacker_army_id)],
        ["defender city", idLink("city", report.defender_city_id)],
        ["seed", report.seed],
        ["winner", report.winner],
        ["created_at", report.created_at],
        ["trace", traceLink(report.trace_id)],
      ]),
      h("h4", { text: "Before / remaining / casualties" }),
      raw({
        attacker_before: report.attacker_before,
        defender_before: report.defender_before,
        attacker_remaining: report.attacker_remaining,
        defender_remaining: report.defender_remaining,
        attacker_casualties: report.attacker_casualties,
        defender_casualties: report.defender_casualties,
      }),
      h("h4", { text: "Loot and defender resources" }),
      raw({ loot: report.loot, defender_resources: report.defender_resources }),
      h("h4", { text: "Rounds" }),
      raw(report.rounds)
    );
    const tx = h("div");
    slot.append(h("h4", { text: "Ledger rows for the battle event" }), tx);
    renderTable(
      tx,
      [
        { label: "Id", cell: (row) => row.id },
        { label: "Player", cell: (row) => idLink("player", row.player_id) },
        { label: "City", cell: (row) => idLink("city", row.city_id) },
        { label: "Resource", cell: (row) => row.resource },
        { label: "Delta", cell: (row) => row.delta },
        { label: "Balance", cell: (row) => row.balance_after },
        { label: "Reason", cell: (row) => row.reason },
        { label: "Event", cell: (row) => idLink("events", row.source_event_id) },
        { label: "Key", cell: (row) => row.idempotency_key },
        { label: "Created", cell: (row) => row.created_at },
      ],
      body.transactions || [],
      "No ledger rows for this report."
    );
  }

  async function loadBattles(focusId) {
    const body = await request("/v1/admin/reports?limit=200");
    renderTable(
      $("battle-table"),
      [
        { label: "Id", cell: (row) => idLink("battles", row.id) },
        { label: "Event", cell: (row) => idLink("events", row.event_id) },
        { label: "Movement", cell: (row) => idLink("movement", row.movement_id) },
        { label: "Seed", cell: (row) => row.seed },
        { label: "Winner", cell: (row) => row.winner },
        { label: "Attacker", cell: (row) => idLink("player", row.attacker_player_id) },
        { label: "Defender", cell: (row) => idLink("player", row.defender_player_id) },
        { label: "Army", cell: (row) => idLink("army", row.attacker_army_id) },
        { label: "City", cell: (row) => idLink("city", row.defender_city_id) },
        { label: "Loot", cell: (row) => JSON.stringify(row.loot) },
        { label: "Created", cell: (row) => row.created_at },
      ],
      body.reports || [],
      "No battle reports."
    );
    if (focusId) renderBattleDetail(await request("/v1/admin/reports/" + focusId));
    else $("battle-detail").replaceChildren();
  }

  async function loadLedger() {
    const body = await request(
      "/v1/admin/transactions" +
        query({
          limit: 200,
          player_id: $("ledger-player").value.trim(),
          city_id: $("ledger-city").value.trim(),
          source_event_id: $("ledger-event").value.trim(),
          resource: $("ledger-resource").value.trim(),
        })
    );
    renderTable(
      $("ledger-table"),
      [
        { label: "Id", cell: (row) => row.id },
        { label: "Player", cell: (row) => idLink("player", row.player_id) },
        { label: "City", cell: (row) => idLink("city", row.city_id) },
        { label: "Resource", cell: (row) => row.resource },
        { label: "Delta", cell: (row) => row.delta },
        { label: "Balance after", cell: (row) => row.balance_after },
        { label: "Reason", cell: (row) => row.reason },
        { label: "Event", cell: (row) => idLink("events", row.source_event_id) },
        { label: "Trace", cell: (row) => traceLink(row.trace_id) },
        { label: "Idempotency", cell: (row) => row.idempotency_key },
        { label: "Created", cell: (row) => row.created_at },
      ],
      body.transactions || [],
      "No transactions."
    );
  }

  function fieldPairs(value) {
    if (value == null || typeof value !== "object") {
      return [["value", blank(value)]];
    }
    return Object.keys(value).map((key) => {
      const item = value[key];
      const shown = item != null && typeof item === "object" ? JSON.stringify(item) : blank(item);
      return [key, shown];
    });
  }

  function renderVerdict(target, verdict) {
    const text = verdict == null || verdict === "" ? "UNKNOWN" : String(verdict);
    target.replaceChildren(
      h("div", { class: verdictClass(verdict) },
        h("div", { class: "stat-label", text: "Integrity verdict" }),
        h("div", { class: "verdict-value", text: text })
      )
    );
  }

  function renderTraceDetail(body) {
    renderVerdict($("trace-verdict"), body && body.verdict);
    const reasons = (body && body.reasons) || [];
    const reasonHost = $("trace-reasons");
    reasonHost.replaceChildren(h("h3", { text: "Reasons" }));
    if (!reasons.length) {
      reasonHost.append(h("p", { class: "muted", text: body && body.verdict === "FAIL" ? "The server returned no reasons." : "No reasons returned." }));
    } else {
      const list = h("ul", { class: "reason-list" });
      reasons.forEach((reason) => list.append(h("li", { text: reason })));
      reasonHost.append(list);
    }
    const integrity = (body && body.integrity) || {};
    const checks = integrity.checks || [];
    const notChecked = integrity.not_checked || [];
    const checkHost = $("trace-checks");
    const checkTable = h("div");
    const uncheckedTable = h("div");
    checkHost.replaceChildren(h("h3", { text: "Checks" }), checkTable, h("h3", { text: "Not checked" }), uncheckedTable);
    renderTable(
      checkTable,
      [
        { label: "Check", cell: (row) => row.name },
        { label: "Status", cell: (row) => badge(row.status) },
        { label: "Reasons", cell: (row) => (row.reasons || []).join("; ") },
      ],
      checks,
      "No checks in the server response."
    );
    renderTable(
      uncheckedTable,
      [
        { label: "Check", cell: (row) => row.name },
        { label: "Status", cell: (row) => badge(row.status) },
        { label: "Detail", cell: (row) => row.detail },
      ],
      notChecked,
      "No unchecked items in the server response."
    );
    $("trace-legacy").replaceChildren();
    $("trace-list").replaceChildren();
    const timeline = $("trace-timeline");
    timeline.replaceChildren(h("h3", { text: "Timeline" }));
    const steps = (body && body.steps) || [];
    if (!steps.length) {
      timeline.append(h("p", { class: "muted", text: "No steps in the server response." }));
      return;
    }
    const list = h("div", { class: "timeline" });
    steps.forEach((step) => {
      list.append(
        h("article", { class: "timeline-step" },
          h("div", { class: "timeline-type", text: step.type }),
          h("p", { class: "muted", text: blank(step.game_time) }),
          h("h4", { text: "Ids" }),
          kv(fieldPairs(step.ids)),
          h("h4", { text: "Fields" }),
          kv(fieldPairs(step.fields))
        )
      );
    });
    timeline.append(list);
  }

  function renderTraceSearch(body) {
    $("trace-verdict").replaceChildren();
    $("trace-reasons").replaceChildren();
    $("trace-checks").replaceChildren();
    $("trace-timeline").replaceChildren();
    const legacy = (body && body.legacy) || [];
    const legacyHost = $("trace-legacy");
    const legacyTable = h("div");
    legacyHost.replaceChildren(h("h3", { text: "Legacy" }), legacyTable);
    renderTable(
      legacyTable,
      [
        { label: "Kind", cell: (row) => row.kind },
        { label: "Id", cell: (row) => row.id },
        { label: "Trace", cell: (row) => row.trace },
        { label: "Detail", cell: (row) => row.detail },
      ],
      legacy,
      "No legacy rows for this query."
    );
    const traces = (body && body.traces) || [];
    const listHost = $("trace-list");
    const traceTable = h("div");
    listHost.replaceChildren(h("h3", { text: "Traces" }), traceTable);
    renderTable(
      traceTable,
      [
        { label: "Trace", cell: (row) => traceLink(row.trace_id) },
        { label: "Command", cell: (row) => row.command_id },
        { label: "Type", cell: (row) => row.command_type },
        { label: "Player", cell: (row) => row.player_id },
        { label: "Army", cell: (row) => row.army_id },
        { label: "Verdict", cell: (row) => badge(row.verdict) },
      ],
      traces,
      "No traces for this query."
    );
  }

  function clearTrace() {
    $("trace-verdict").replaceChildren();
    $("trace-reasons").replaceChildren();
    $("trace-checks").replaceChildren();
    $("trace-legacy").replaceChildren();
    $("trace-list").replaceChildren();
    $("trace-timeline").replaceChildren();
  }

  async function loadTrace(parsed) {
    const entity = parsed.entity;
    const id = parsed.id;
    if (!entity) {
      clearTrace();
      return;
    }
    if (entity === "id" && id) {
      $("trace-id").value = id;
      renderTraceDetail(await request("/v1/admin/trace/" + encodeURIComponent(id)));
      return;
    }
    const params = {};
    if (entity === "player" && id) params.player = id;
    else if (entity === "army" && id) params.army = id;
    else if (entity === "event" && id) params.event = id;
    else if (entity === "command" && id) params.command = id;
    else return;
    if (params.player) $("trace-player").value = params.player;
    if (params.army) $("trace-army").value = params.army;
    if (params.event) $("trace-event").value = params.event;
    if (params.command) $("trace-command").value = params.command;
    renderTraceSearch(await request("/v1/admin/trace" + query(params)));
  }

  function renderAudit(body) {
    const chain = (body && body.chain) || {};
    const status = chain.status == null || chain.status === "" ? "UNKNOWN" : String(chain.status);
    const chainHost = $("audit-chain");
    chainHost.replaceChildren(
      h("div", { class: verdictClass(status) },
        h("div", { class: "stat-label", text: "Chain status" }),
        h("div", { class: "verdict-value", text: status }),
        h("p", { class: "muted", text: "Checked rows: " + blank(chain.checked_rows) })
      )
    );
    const reasons = chain.reasons || [];
    if (reasons.length) {
      const list = h("ul", { class: "reason-list" });
      reasons.forEach((reason) => list.append(h("li", { text: reason })));
      chainHost.append(list);
    }
    renderTable(
      $("audit-table"),
      [
        { label: "Id", cell: (row) => row.id },
        { label: "When", cell: (row) => row.timestamp },
        { label: "Actor", cell: (row) => row.actor },
        { label: "Action", cell: (row) => row.action },
        { label: "Target", cell: (row) => row.target },
        { label: "IP", cell: (row) => row.source_ip },
        { label: "Result", cell: (row) => badge(row.result) },
        { label: "Reason", cell: (row) => blank(row.reason) },
      ],
      (body && body.entries) || [],
      "No audit rows."
    );
    const total = body && body.total != null ? body.total : 0;
    const offset = body && body.offset != null ? body.offset : 0;
    const limit = body && body.limit != null ? body.limit : 50;
    $("audit-page").textContent = "Showing " + (total ? offset + 1 : 0) + "–" + Math.min(offset + limit, total) + " of " + total;
    $("audit-prev").disabled = offset <= 0;
    $("audit-next").disabled = offset + limit >= total;
  }

  async function loadAudit() {
    const body = await request(
      "/v1/admin/audit" +
        query({
          limit: 50,
          offset: state.auditOffset,
          actor: $("audit-actor").value.trim(),
          action: $("audit-action").value.trim(),
          result: $("audit-result").value.trim(),
        })
    );
    renderAudit(body);
  }

  function wireAck(boxId, buttonId) {
    $(boxId).addEventListener("change", () => {
      $(buttonId).disabled = !$(boxId).checked;
    });
  }

  function parseHash() {
    const raw = (location.hash || "#dashboard").replace(/^#/, "");
    const parts = raw.split("/");
    const head = parts[0] || "dashboard";
    const tail = parts[1] || null;
    if (head === "player" || head === "city" || head === "army" || head === "movement") {
      const tab = head === "player" ? "players" : head === "city" ? "cities" : head === "army" ? "armies" : "movements";
      return { view: "world", tab: tab, entity: head, id: tail };
    }
    if (head === "world") {
      const tabs = { players: true, cities: true, armies: true, movements: true };
      return { view: "world", tab: tabs[tail] ? tail : "players", entity: null, id: null };
    }
    if (head === "trace") {
      return { view: "trace", tab: null, entity: parts[1] || null, id: parts.slice(2).join("/") || null };
    }
    const views = { dashboard: true, snapshots: true, events: true, battles: true, ledger: true, actions: true, trace: true, audit: true, map: true };
    if (views[head]) return { view: head, tab: null, entity: null, id: tail };
    return { view: "dashboard", tab: null, entity: null, id: null };
  }

  function showView(name) {
    document.querySelectorAll("[data-view]").forEach((section) => {
      section.hidden = section.getAttribute("data-view") !== name;
    });
    document.querySelectorAll("[data-nav]").forEach((button) => {
      const on = button.getAttribute("data-nav") === name;
      button.classList.toggle("active", on);
    });
    document.querySelectorAll("[data-world]").forEach((button) => {
      button.classList.toggle("active", button.getAttribute("data-world") === state.worldTab);
    });
  }

  async function route() {
    const gen = ++generation;
    const parsed = parseHash();
    state.view = parsed.view;
    if (parsed.tab) state.worldTab = parsed.tab;
    if (window.SimcoreWorldMap) window.SimcoreWorldMap.onView(parsed.view);
    showView(parsed.view);
    clearError();
    try {
      if (parsed.view === "dashboard") await loadDashboard(gen);
      else if (parsed.view === "actions") return;
      else if (!signedIn()) throw new Error("Sign in with the admin password or enter the admin token");
      else if (parsed.view === "snapshots") {
        await loadSnapshots();
        if (gen !== generation) return;
        if (parsed.id) await inspectSnapshot(parsed.id);
      } else if (parsed.view === "events") await loadEvents(parsed.id);
      else if (parsed.view === "world") await loadWorld(parsed.entity, parsed.id);
      else if (parsed.view === "battles") await loadBattles(parsed.id);
      else if (parsed.view === "ledger") await loadLedger();
      else if (parsed.view === "trace") await loadTrace(parsed);
      else if (parsed.view === "audit") await loadAudit();
      else if (parsed.view === "map") {
        if (!window.SimcoreWorldMap) throw new Error("Map script did not load");
        await window.SimcoreWorldMap.show(request);
      }
    } catch (error) {
      if (gen === generation) showError(error);
    }
  }

  function connect(event) {
    event.preventDefault();
    state.apiBaseUrl = $("api-url").value.trim().replace(/\/+$/, "");
    const typed = normalizeAdminToken($("admin-token").value);
    if (typed) state.token = typed;
    $("admin-token").value = "";
    if (state.apiBaseUrl) localStorage.setItem(STORAGE_URL, state.apiBaseUrl);
    const remember = $("remember-token").checked;
    const tokenStored = persistToken(remember ? state.token : "");
    const sessionMessage = !state.token
      ? "API URL saved. Token is not loaded."
      : remember
        ? tokenStored
          ? "Token saved on this device."
          : "Token is active for this page, but browser storage is unavailable."
        : "Token is active for this page only.";
    setSessionStatus(sessionMessage, state.token ? "ok" : "");
    route();
  }

  async function signIn(event) {
    event.preventDefault();
    state.apiBaseUrl = $("api-url").value.trim().replace(/\/+$/, "");
    const password = $("admin-password").value;
    $("admin-password").value = "";
    if (state.apiBaseUrl) localStorage.setItem(STORAGE_URL, state.apiBaseUrl);
    clearError();
    try {
      const body = await request("/v1/admin/login", { method: "POST", json: { password: password }, admin: false });
      state.sessionToken = body && body.token ? String(body.token) : "";
      const stored = persistSession(state.sessionToken);
      const stay = $("stay-signed-in").checked;
      const message = !state.sessionToken
        ? "Sign-in did not return a session."
        : stay
          ? stored
            ? "Signed in. The session token is saved on this device until it expires."
            : "Signed in for this page, but browser storage is unavailable."
          : "Signed in for this tab only. The session token was not saved.";
      setSessionStatus(message, state.sessionToken ? "ok" : "");
      showExpiry(state.sessionToken);
      route();
    } catch (error) {
      setSessionStatus(error && error.message ? error.message : "Sign-in failed", "bad");
      showExpiry("");
    }
  }

  async function logOut() {
    const hadSession = Boolean(state.sessionToken);
    clearError();
    try {
      if (hadSession && state.apiBaseUrl) {
        await request("/v1/admin/logout", { method: "POST" });
      }
    } catch (error) {
      showError(error);
    }
    state.sessionToken = "";
    persistSession("");
    showExpiry("");
    setSessionStatus(state.token ? "Session cleared. The advanced admin token is still loaded." : "Signed out.", "");
    route();
  }

  $("admin-login-form").addEventListener("submit", (event) => {
    signIn(event).catch(showError);
  });
  $("logout-btn").addEventListener("click", () => {
    logOut().catch(showError);
  });
  $("session-form").addEventListener("submit", connect);
  $("lock-btn").addEventListener("click", () => {
    state.token = "";
    $("admin-token").value = "";
    $("remember-token").checked = false;
    const tokenRemoved = persistToken("");
    setSessionStatus(
      tokenRemoved ? "Token cleared from this page and browser profile." : "Token cleared from this page, but browser storage could not be cleared.",
      "",
    );
    route();
  });
  $("api-reset").addEventListener("click", () => {
    localStorage.removeItem(STORAGE_URL);
    state.apiBaseUrl = defaultApi();
    $("api-url").value = state.apiBaseUrl;
  });
  $("refresh-btn").addEventListener("click", () => route());
  document.querySelectorAll("[data-nav]").forEach((button) => {
    button.addEventListener("click", () => {
      location.hash = button.getAttribute("data-nav");
    });
  });
  document.querySelectorAll("[data-world]").forEach((button) => {
    button.addEventListener("click", () => {
      location.hash = "world/" + button.getAttribute("data-world");
    });
  });
  $("snap-manual").addEventListener("click", () => createSnapshot().catch(showError));
  $("snap-refresh").addEventListener("click", () => loadSnapshots().catch(showError));
  $("restore-form").addEventListener("submit", (event) => restoreSnapshot(event).catch(showError));
  $("restore-ack").addEventListener("change", syncRestore);
  $("restore-typed").addEventListener("input", syncRestore);
  $("event-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    if (location.hash !== "#events") location.hash = "events";
    else loadEvents(null).catch(showError);
  });
  $("movement-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    loadWorld(null, null).catch(showError);
  });
  $("ledger-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    loadLedger().catch(showError);
  });
  $("trace-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const traceId = $("trace-id").value.trim();
    const player = $("trace-player").value.trim();
    const army = $("trace-army").value.trim();
    const eventId = $("trace-event").value.trim();
    const commandId = $("trace-command").value.trim();
    let next = "trace";
    if (traceId) next = "trace/id/" + traceId;
    else if (eventId) next = "trace/event/" + eventId;
    else if (army) next = "trace/army/" + army;
    else if (player) next = "trace/player/" + player;
    else if (commandId) next = "trace/command/" + commandId;
    if (location.hash === "#" + next) loadTrace(parseHash()).catch(showError);
    else location.hash = next;
  });
  $("audit-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    state.auditOffset = 0;
    if (location.hash !== "#audit") location.hash = "audit";
    else loadAudit().catch(showError);
  });
  $("audit-prev").addEventListener("click", () => {
    state.auditOffset = Math.max(0, state.auditOffset - 50);
    loadAudit().catch(showError);
  });
  $("audit-next").addEventListener("click", () => {
    state.auditOffset += 50;
    loadAudit().catch(showError);
  });
  wireAck("clock-ack", "clock-go");
  wireAck("run-ack", "run-go");
  wireAck("tick-ack", "tick-go");
  $("clock-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!$("clock-ack").checked) return;
    request("/v1/admin/clock/advance", {
      method: "POST",
      json: {
        hours: Number($("clock-hours").value || 0),
        minutes: Number($("clock-minutes").value || 0),
        seconds: Number($("clock-seconds").value || 0),
      },
    })
      .then((body) => showResult("action-result", body))
      .catch(showError);
  });
  $("run-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!$("run-ack").checked) return;
    const id = $("run-id").value.trim();
    request("/v1/admin/events/" + id + "/run", { method: "POST" })
      .then((body) => showResult("action-result", body))
      .catch(showError);
  });
  $("tick-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!$("tick-ack").checked) return;
    const limit = $("tick-limit").value.trim() || "50";
    request("/v1/admin/worker/tick?limit=" + encodeURIComponent(limit), { method: "POST" })
      .then((body) => showResult("action-result", body))
      .catch(showError);
  });

  window.addEventListener("hashchange", () => route());
  state.apiBaseUrl = savedUrl() || defaultApi();
  state.token = savedToken();
  state.sessionToken = savedSession();
  $("api-url").value = state.apiBaseUrl;
  $("remember-token").checked = Boolean(state.token);
  if (state.sessionToken) $("stay-signed-in").checked = true;
  if (state.sessionToken) {
    setSessionStatus("Signed in from this device.", "ok");
    showExpiry(state.sessionToken);
  } else {
    setSessionStatus(
      state.token ? "Saved token loaded from this device." : "Not signed in.",
      state.token ? "ok" : "",
    );
    showExpiry("");
  }
  route();
})();
