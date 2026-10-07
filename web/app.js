(() => {
  const STORAGE_URL = "simcore.apiBaseUrl";
  const STORAGE_TOKEN = "simcore.token";
  const STORAGE_REFRESH = "simcore.refreshToken";
  const STORAGE_NAME = "simcore.playerName";
  const REQUEST_MS = 25000;
  const POLL_MS = 4000;
  const RETRY_MS = 15000;

  const defaultApi = () => {
    const configured = window.SIMCORE_CONFIG && window.SIMCORE_CONFIG.apiBaseUrl;
    return String(configured || "").replace(/\/+$/, "");
  };

  const state = {
    apiBaseUrl: "",
    token: sessionStorage.getItem(STORAGE_TOKEN) || "",
    refreshToken: sessionStorage.getItem(STORAGE_REFRESH) || "",
    playerName: sessionStorage.getItem(STORAGE_NAME) || "",
    offsetMs: 0,
    serverTime: "",
    cities: [],
    map: [],
    armies: [],
    reports: [],
    selectedArmyId: null,
    selectedCityId: null,
    unreachable: false,
    pollTimer: 0,
    retryTimer: 0,
    clockTimer: 0,
    busy: false,
  };

  const $ = (id) => document.getElementById(id);
  let epoch = 0;

  function savedUrl() {
    return (localStorage.getItem(STORAGE_URL) || "").replace(/\/+$/, "");
  }

  function setStatus(text, kind) {
    const node = $("api-status");
    node.textContent = text;
    node.className = "status" + (kind ? " " + kind : "");
  }

  function showBanner(html) {
    const banner = $("banner");
    banner.hidden = false;
    banner.innerHTML = html;
  }

  function hideBanner() {
    $("banner").hidden = true;
    $("banner").textContent = "";
  }

  function markUnreachable() {
    state.unreachable = true;
    const url = state.apiBaseUrl || "(ยังไม่ได้ตั้งค่า)";
    showBanner(
      "<strong>ติดต่อ API ไม่ได้</strong><br />" +
        "เซิร์ฟเวอร์อาจกำลังเริ่มทำงาน ใบรับรอง HTTPS ยังออกไม่เสร็จ หรือเครื่อง VPS ปิดอยู่ " +
        "ถ้าเพิ่งเปิดเครื่องหรือเพิ่งติดตั้ง รอประมาณหนึ่งนาทีแล้วกดลองอีกครั้ง<br />" +
        "<span class=\"muted\">Cannot reach " +
        escapeHtml(url) +
        ". Check that the simcore-api and simcore-caddy services are running.</span>"
    );
    setStatus("API ไม่ตอบ", "bad");
    window.clearTimeout(state.retryTimer);
    state.retryTimer = window.setTimeout(probe, RETRY_MS);
  }

  function markReachable() {
    state.unreachable = false;
    hideBanner();
    window.clearTimeout(state.retryTimer);
    setStatus("API ตอบแล้ว · " + state.apiBaseUrl, "ok");
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function formatNumber(value) {
    return Number(value).toLocaleString("th-TH");
  }

  async function api(path, options = {}) {
    const generation = epoch;
    if (!state.apiBaseUrl) {
      if (generation === epoch) markUnreachable();
      throw new Error("ยังไม่ได้ตั้ง API");
    }
    const headers = new Headers(options.headers || {});
    if (options.json !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    if (options.auth !== false && state.token) {
      headers.set("Authorization", "Bearer " + state.token);
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REQUEST_MS);
    try {
      const response = await fetch(state.apiBaseUrl + path, {
        method: options.method || "GET",
        headers,
        body: options.json !== undefined ? JSON.stringify(options.json) : undefined,
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
        const message = body && body.error && body.error.message ? body.error.message : "HTTP " + response.status;
        const error = new Error(message);
        error.status = response.status;
        error.body = body;
        if (generation === epoch && response.status >= 500 && path === "/health/ready") {
          showBanner(
            "<strong>API ตอบแล้ว แต่ยังไม่พร้อม</strong><br />ฐานข้อมูลอาจกำลังเริ่ม รอสักครู่แล้วกดลองอีกครั้ง"
          );
          setStatus("ฐานข้อมูลยังไม่พร้อม", "bad");
          window.clearTimeout(state.retryTimer);
          state.retryTimer = window.setTimeout(probe, RETRY_MS);
        } else if (generation === epoch) {
          markReachable();
        }
        throw error;
      }
      if (generation === epoch) markReachable();
      return body;
    } catch (error) {
      if (generation === epoch && (error.name === "AbortError" || error instanceof TypeError)) {
        markUnreachable();
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function probe() {
    window.clearTimeout(state.retryTimer);
    epoch += 1;
    setStatus("กำลังตรวจ " + state.apiBaseUrl + " …");
    try {
      await api("/health/ready", { auth: false });
      if (state.token) {
        await refreshAll();
      }
    } catch {
      // The banner is already visible.
    }
  }

  function applyUrl(url, persist) {
    epoch += 1;
    state.apiBaseUrl = String(url || "").trim().replace(/\/+$/, "");
    $("api-url").value = state.apiBaseUrl;
    if (persist) {
      if (state.apiBaseUrl && state.apiBaseUrl !== defaultApi()) {
        localStorage.setItem(STORAGE_URL, state.apiBaseUrl);
      } else {
        localStorage.removeItem(STORAGE_URL);
      }
    }
  }

  function syncClock(body) {
    const serverMs = Date.parse(body.server_time);
    if (!Number.isNaN(serverMs)) {
      state.offsetMs = serverMs - Date.now();
      state.serverTime = body.server_time;
    }
  }

  function nowOnServer() {
    return Date.now() + state.offsetMs;
  }

  function formatRemain(arriveAt) {
    const remain = Date.parse(arriveAt) - nowOnServer();
    if (Number.isNaN(remain)) return "";
    if (remain <= 0) return "ถึงแล้ว — รอผลจากเซิร์ฟเวอร์";
    const total = Math.ceil(remain / 1000);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return hours > 0 ? "ถึงใน " + hours + ":" + pad(minutes) + ":" + pad(seconds) : "ถึงใน " + pad(minutes) + ":" + pad(seconds);
  }

  function updateCountdowns() {
    document.querySelectorAll("[data-arrive]").forEach((node) => {
      node.textContent = formatRemain(node.getAttribute("data-arrive"));
    });
  }

  function resourceChips(city) {
    const rows = [
      ["wood", "ไม้", city.wood, city.wood_rate],
      ["food", "อาหาร", city.food, city.food_rate],
      ["iron", "เหล็ก", city.iron, city.iron_rate],
      ["gold", "ทอง", city.gold, city.gold_rate],
    ];
    return (
      '<div class="resources">' +
      rows
        .map(
          ([key, label, amount, rate]) =>
            '<span class="chip ' +
            key +
            '">' +
            label +
            " " +
            formatNumber(amount) +
            ' <span class="muted">+' +
            formatNumber(rate) +
            "/ชม.</span></span>"
        )
        .join("") +
      "</div>"
    );
  }

  function unitLine(units) {
    if (!units || !units.length) return "ไม่มีหน่วย";
    return units.map((stack) => stack.type + " × " + formatNumber(stack.count)).join(", ");
  }

  function renderCities() {
    const root = $("cities");
    if (!state.cities.length) {
      root.innerHTML = '<p class="muted">ยังไม่มีเมือง</p>';
      return;
    }
    root.innerHTML = state.cities
      .map(
        (city) =>
          '<article class="city"><p class="kicker">เมืองของฉัน</p><h3>' +
          escapeHtml(city.name) +
          "</h3><p class=\"muted\">(" +
          city.x +
          ", " +
          city.y +
          ")</p>" +
          resourceChips(city) +
          "</article>"
      )
      .join("");
  }

  function renderMap() {
    const map = $("map");
    const list = $("map-list");
    if (!state.map.length) {
      map.innerHTML = "";
      list.innerHTML = "";
      return;
    }
    const xs = state.map.map((city) => city.x);
    const ys = state.map.map((city) => city.y);
    const minX = Math.min(...xs) - 8;
    const maxX = Math.max(...xs) + 8;
    const minY = Math.min(...ys) - 8;
    const maxY = Math.max(...ys) + 8;
    const spanX = Math.max(1, maxX - minX);
    const spanY = Math.max(1, maxY - minY);
    map.innerHTML = state.map
      .map((city) => {
        const left = ((city.x - minX) / spanX) * 100;
        const top = (1 - (city.y - minY) / spanY) * 100;
        const classes = ["map-pin", city.is_mine ? "mine" : "", city.id === state.selectedCityId ? "selected" : ""]
          .filter(Boolean)
          .join(" ");
        return (
          '<button type="button" class="' +
          classes +
          '" style="left:' +
          left +
          "%;top:" +
          top +
          '%" data-city="' +
          city.id +
          '">' +
          escapeHtml(city.name) +
          "<small>" +
          escapeHtml(city.player_name || "") +
          "</small></button>"
        );
      })
      .join("");
    list.innerHTML = state.map
      .map((city) => {
        const selected = city.id === state.selectedCityId ? " selected" : "";
        return (
          '<li class="map-item' +
          selected +
          '" data-city="' +
          city.id +
          '"><strong>' +
          escapeHtml(city.name) +
          "</strong> · " +
          escapeHtml(city.player_name || "") +
          " (" +
          city.x +
          ", " +
          city.y +
          ")" +
          (city.is_mine ? " · ของเรา" : "") +
          "</li>"
        );
      })
      .join("");
  }

  function renderArmies() {
    const root = $("armies");
    if (!state.armies.length) {
      root.innerHTML = '<p class="muted">ยังไม่มีกองทัพ</p>';
      return;
    }
    root.innerHTML = state.armies
      .map((army) => {
        const movement = army.movement;
        let trip = '<p class="muted">ไม่มีขบวนที่กำลังเดินทาง</p>';
        if (movement) {
          trip =
            '<p class="kicker">' +
            escapeHtml(movement.mission) +
            " · " +
            escapeHtml(movement.status) +
            '</p><p class="eta" data-arrive="' +
            escapeHtml(movement.arrive_at) +
            '">' +
            escapeHtml(formatRemain(movement.arrive_at)) +
            "</p><p class=\"muted\">ออก " +
            escapeHtml(movement.depart_at) +
            "<br />ถึง " +
            escapeHtml(movement.arrive_at) +
            "</p>";
        }
        const selected = army.id === state.selectedArmyId ? " selected" : "";
        return (
          '<article class="army' +
          selected +
          '" data-army="' +
          army.id +
          '"><h3>' +
          escapeHtml(army.name) +
          '</h3><p class="muted">' +
          escapeHtml(army.status) +
          " · " +
          unitLine(army.units) +
          "</p>" +
          trip +
          "</article>"
        );
      })
      .join("");
    updateCountdowns();
  }

  function lootLine(loot) {
    if (!loot) return "—";
    return Object.entries(loot)
      .filter(([, amount]) => amount)
      .map(([name, amount]) => name + " " + formatNumber(amount))
      .join(", ") || "ไม่มีของที่ยึด";
  }

  function winnerLabel(winner) {
    if (winner === "attacker") return "ฝ่ายโจมตี";
    if (winner === "defender") return "ฝ่ายตั้งรับ";
    if (winner === "draw") return "เสมอ";
    return winner || "—";
  }

  function renderReports() {
    const root = $("reports");
    if (!state.reports.length) {
      root.innerHTML = '<p class="muted">ยังไม่มีรายงาน หลังกองทัพไปถึง เซิร์ฟเวอร์จะเขียนรายงานที่นี่</p>';
      return;
    }
    const newestFirst = state.reports.slice().reverse();
    root.innerHTML = newestFirst
      .map(
        (report) =>
          '<article class="report"><p class="kicker">รายงาน #' +
          report.id +
          "</p><h3>ผู้ชนะ: " +
          escapeHtml(winnerLabel(report.winner)) +
          '</h3><p class="muted">เมืองป้องกัน #' +
          report.defender_city_id +
          " · seed " +
          report.seed +
          "</p><p>ของที่ยึด: " +
          escapeHtml(lootLine(report.loot)) +
          "</p><p class=\"muted\">สูญเสียฝ่ายโจมตี: " +
          escapeHtml(unitLine(report.attacker_casualties)) +
          "<br />สูญเสียฝ่ายตั้งรับ: " +
          escapeHtml(unitLine(report.defender_casualties)) +
          "</p></article>"
      )
      .join("");
  }

  function fillSelect(select, options, selected) {
    const previous = selected != null ? String(selected) : select.value;
    select.innerHTML = options
      .map((option) => '<option value="' + option.value + '">' + escapeHtml(option.label) + "</option>")
      .join("");
    if (previous && [...select.options].some((option) => option.value === previous)) {
      select.value = previous;
    }
  }

  function renderOrders() {
    fillSelect(
      $("army-select"),
      state.armies.map((army) => ({
        value: String(army.id),
        label: army.name + " · " + army.status,
      })),
      state.selectedArmyId
    );
    fillSelect(
      $("city-select"),
      state.map.map((city) => ({
        value: String(city.id),
        label: city.name + " · " + (city.player_name || "") + (city.is_mine ? " · ของเรา" : ""),
      })),
      state.selectedCityId
    );
    if ($("army-select").value) state.selectedArmyId = Number($("army-select").value);
    if ($("city-select").value) state.selectedCityId = Number($("city-select").value);
    const army = state.armies.find((item) => item.id === state.selectedArmyId);
    const city = state.map.find((item) => item.id === state.selectedCityId);
    const hint = $("order-hint");
    if (!army || !city) {
      hint.textContent = "เลือกกองทัพและเมืองเป้าหมาย";
      $("attack-btn").disabled = true;
      $("move-btn").disabled = true;
      $("recall-btn").disabled = true;
      return;
    }
    const garrisoned = army.status === "garrisoned";
    $("attack-btn").disabled = state.busy || !garrisoned || city.is_mine;
    $("move-btn").disabled = state.busy || !garrisoned || !city.is_mine || city.id === army.location_city_id;
    const away = army.status === "marching" || (army.status === "garrisoned" && army.location_city_id !== army.home_city_id);
    $("recall-btn").disabled = state.busy || !away;
    if (!garrisoned && army.movement) {
      hint.textContent = "กองทัพกำลังเดินทาง นับถอยหลังจากเวลาถึงของเซิร์ฟเวอร์";
    } else if (city.is_mine) {
      hint.textContent = "เดินทัพได้เฉพาะเมืองของเรา โจมตีใช้กับเมืองของผู้อื่น";
    } else {
      hint.textContent = "โจมตีจะส่งกองทัพออกทันที ผลรบยังไม่เกิดจนกว่าจะถึงเวลา";
    }
  }

  function renderGame() {
    const active = document.activeElement;
    const hold = active && $("game").contains(active) && active.tagName === "SELECT";
    renderCities();
    if (!hold) {
      renderMap();
      renderArmies();
      renderOrders();
    } else {
      updateCountdowns();
    }
    renderReports();
    const clock = $("clock-line");
    clock.textContent = state.serverTime ? "เวลาเซิร์ฟเวอร์ " + state.serverTime : "";
  }

  function showGame(on) {
    $("game").hidden = !on;
    $("login-panel").hidden = on;
    $("logout-btn").hidden = !on;
    $("who-name").textContent = on ? state.playerName : "ยังไม่ได้เข้าสู่ระบบ";
  }

  function idempotencyKey() {
    if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    return "web-" + Date.now().toString(16) + "-" + Math.random().toString(16).slice(2);
  }

  function applySession(body) {
    state.token = body.access_token || body.token || "";
    state.refreshToken = body.refresh_token || "";
    state.playerName = body.player_name || state.playerName;
    if (state.token) sessionStorage.setItem(STORAGE_TOKEN, state.token);
    else sessionStorage.removeItem(STORAGE_TOKEN);
    if (state.refreshToken) sessionStorage.setItem(STORAGE_REFRESH, state.refreshToken);
    else sessionStorage.removeItem(STORAGE_REFRESH);
    if (state.playerName) sessionStorage.setItem(STORAGE_NAME, state.playerName);
  }

  function showChangePassword(on) {
    $("change-password-form").hidden = !on;
    $("login-form").hidden = on;
    $("register-form").hidden = on;
  }

  async function refreshAll() {
    const [timeBody, me, cities, map, armies, reports] = await Promise.all([
      api("/v1/time"),
      api("/v1/me"),
      api("/v1/me/cities"),
      api("/v1/map/cities"),
      api("/v1/me/armies"),
      api("/v1/me/reports"),
    ]);
    syncClock(timeBody);
    state.playerName = me.name;
    sessionStorage.setItem(STORAGE_NAME, me.name);
    state.cities = cities.cities;
    state.map = map.cities;
    state.armies = armies.armies;
    state.reports = reports.reports;
    if (timeBody && armies.server_time) {
      syncClock({ server_time: armies.server_time });
    }
    showGame(true);
    renderGame();
  }

  async function enterGame(body) {
    applySession(body);
    if (body.must_change_password) {
      showChangePassword(true);
      showGame(false);
      $("login-note").textContent = "ต้องเปลี่ยนรหัสผ่านก่อนเล่น";
      return;
    }
    showChangePassword(false);
    $("login-note").textContent = body.warning || "";
    await refreshAll();
    startPoll();
  }

  async function loginAccount(username, password) {
    $("login-note").textContent = "";
    const body = await api("/v1/auth/login", {
      method: "POST",
      auth: false,
      json: { username: username, password: password },
    });
    $("login-password").value = "";
    await enterGame(body);
  }

  async function registerAccount(username, email, password) {
    $("login-note").textContent = "";
    const payload = { username: username, password: password };
    if (email) payload.email = email;
    const body = await api("/v1/auth/register", { method: "POST", auth: false, json: payload });
    $("register-password").value = "";
    await enterGame(body);
  }

  async function submitPasswordChange(currentPassword, newPassword) {
    const body = await api("/v1/auth/change-password", {
      method: "POST",
      json: { current_password: currentPassword, new_password: newPassword },
    });
    $("current-password").value = "";
    $("new-password").value = "";
    await enterGame(body);
  }

  async function login(name) {
    $("login-note").textContent = "";
    const body = await api("/v1/auth/dev-login", { method: "POST", auth: false, json: { name } });
    state.refreshToken = "";
    sessionStorage.removeItem(STORAGE_REFRESH);
    await enterGame(body);
  }

  async function logout() {
    const token = state.token;
    try {
      if (token && state.apiBaseUrl) {
        await api("/v1/auth/logout", { method: "POST" });
      }
    } catch {
      // Clearing the local session is still the right outcome.
    }
    state.token = "";
    state.refreshToken = "";
    state.playerName = "";
    sessionStorage.removeItem(STORAGE_TOKEN);
    sessionStorage.removeItem(STORAGE_REFRESH);
    sessionStorage.removeItem(STORAGE_NAME);
    window.clearInterval(state.pollTimer);
    showChangePassword(false);
    showGame(false);
  }

  function startPoll() {
    window.clearInterval(state.pollTimer);
    state.pollTimer = window.setInterval(() => {
      if (!state.token || state.busy) return;
      refreshAll().catch(() => {});
    }, POLL_MS);
  }

  function selectCity(id) {
    state.selectedCityId = Number(id);
    renderMap();
    renderOrders();
  }

  async function sendOrder(kind) {
    const errorNode = $("order-error");
    errorNode.hidden = true;
    state.busy = true;
    renderOrders();
    try {
      let path = "/v1/commands/attack";
      let payload = { army_id: state.selectedArmyId, target_city_id: state.selectedCityId };
      if (kind === "move") {
        path = "/v1/commands/move";
        payload = { army_id: state.selectedArmyId, destination_city_id: state.selectedCityId };
      } else if (kind === "recall") {
        path = "/v1/commands/recall";
        payload = { army_id: state.selectedArmyId };
      }
      await api(path, {
        method: "POST",
        json: payload,
        headers: { "Idempotency-Key": idempotencyKey() },
      });
      await refreshAll();
    } catch (error) {
      if (!(error.name === "AbortError" || error instanceof TypeError)) {
        errorNode.hidden = false;
        errorNode.textContent = error.message;
      }
    } finally {
      state.busy = false;
      renderOrders();
    }
  }

  function bind() {
    $("api-form").addEventListener("submit", (event) => {
      event.preventDefault();
      applyUrl($("api-url").value, true);
      probe();
    });
    $("api-reset").addEventListener("click", () => {
      localStorage.removeItem(STORAGE_URL);
      applyUrl(defaultApi(), false);
      probe();
    });
    $("retry-btn").addEventListener("click", probe);
    $("login-form").addEventListener("submit", (event) => {
      event.preventDefault();
      loginAccount($("login-username").value.trim(), $("login-password").value).catch((error) => {
        if (!(error.name === "AbortError" || error instanceof TypeError)) {
          $("login-note").textContent = error.message;
        }
      });
    });
    $("register-form").addEventListener("submit", (event) => {
      event.preventDefault();
      registerAccount(
        $("register-username").value.trim(),
        $("register-email").value.trim(),
        $("register-password").value
      ).catch((error) => {
        if (!(error.name === "AbortError" || error instanceof TypeError)) {
          $("login-note").textContent = error.message;
        }
      });
    });
    $("change-password-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitPasswordChange($("current-password").value, $("new-password").value).catch((error) => {
        if (!(error.name === "AbortError" || error instanceof TypeError)) {
          $("login-note").textContent = error.message;
        }
      });
    });
    document.querySelectorAll("[data-player]").forEach((button) => {
      button.addEventListener("click", () => {
        const name = button.getAttribute("data-player");
        $("player-name").value = name;
        login(name).catch((error) => {
          if (!(error.name === "AbortError" || error instanceof TypeError)) {
            $("login-note").textContent = error.message;
          }
        });
      });
    });
    $("logout-btn").addEventListener("click", () => {
      logout().catch(() => {});
    });
    $("attack-btn").addEventListener("click", () => sendOrder("attack"));
    $("move-btn").addEventListener("click", () => sendOrder("move"));
    $("recall-btn").addEventListener("click", () => sendOrder("recall"));
    $("refresh-btn").addEventListener("click", () => {
      refreshAll().catch(() => {});
    });
    $("army-select").addEventListener("change", () => {
      state.selectedArmyId = Number($("army-select").value);
      renderArmies();
      renderOrders();
    });
    $("city-select").addEventListener("change", () => {
      selectCity($("city-select").value);
    });
    $("map").addEventListener("click", (event) => {
      const pin = event.target.closest("[data-city]");
      if (pin) selectCity(pin.getAttribute("data-city"));
    });
    $("map-list").addEventListener("click", (event) => {
      const item = event.target.closest("[data-city]");
      if (item) selectCity(item.getAttribute("data-city"));
    });
    $("armies").addEventListener("click", (event) => {
      const card = event.target.closest("[data-army]");
      if (!card) return;
      state.selectedArmyId = Number(card.getAttribute("data-army"));
      renderArmies();
      renderOrders();
    });
  }

  function boot() {
    applyUrl(savedUrl() || defaultApi(), false);
    bind();
    showGame(false);
    state.clockTimer = window.setInterval(updateCountdowns, 250);
    probe().then(() => {
      if (state.token) {
        return refreshAll()
          .then(startPoll)
          .catch(() => {
            if (!state.unreachable) logout();
          });
      }
      return undefined;
    });
  }

  boot();
})();
