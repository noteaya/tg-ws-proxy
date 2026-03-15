(function () {
  var STATUS_INTERVAL_MS = 2000;
  var api = window.pywebview && window.pywebview.api;
  var statusEl = document.getElementById("status-text");
  var checkResultEl = document.getElementById("check-result");
  var statusTimer = null;
  var toastContainer = document.getElementById("toast-container");
  var bannerStartup = document.getElementById("banner-startup");

  function showScreen(id) {
    var screens = document.querySelectorAll(".screen");
    screens.forEach(function (s) {
      s.hidden = true;
    });
    var el = document.getElementById("screen-" + id);
    if (el) el.hidden = false;
    var navLinks = document.querySelectorAll(".nav-link");
    navLinks.forEach(function (a) {
      a.classList.toggle("active", a.getAttribute("href") === "#" + id);
    });
  }

  function getHash() {
    var h = window.location.hash.slice(1) || "main";
    if (["main", "first-run", "settings", "about", "differences"].indexOf(h) >= 0) return h;
    return "main";
  }

  function applyHash() {
    showScreen(getHash());
  }

  window.addEventListener("hashchange", applyHash);

  function showToast(message, type) {
    type = type || "info";
    var div = document.createElement("div");
    div.className = "toast toast-" + type;
    div.textContent = message;
    toastContainer.appendChild(div);
    setTimeout(function () {
      div.classList.add("toast-visible");
    }, 10);
    setTimeout(function () {
      div.classList.remove("toast-visible");
      setTimeout(function () { div.remove(); }, 300);
    }, 3500);
  }

  function updateStatus() {
    if (!api || !api.get_status) return;
    try {
      api.get_status().then(function (data) {
        if (statusEl && data && data.status_text) statusEl.textContent = data.status_text;
      }).catch(function () {
        if (statusEl) statusEl.textContent = "Прокси запущен";
      });
    } catch (e) {
      if (statusEl) statusEl.textContent = "Прокси запущен";
    }
  }

  function startStatusPoll() {
    updateStatus();
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = setInterval(updateStatus, STATUS_INTERVAL_MS);
  }

  function renderCheckResult(el, results) {
    if (!el) return;
    if (!results || results.length === 0) {
      el.innerHTML = "<p class=\"row\">Нет настроенных DC.</p>";
    } else if (results.length === 1 && results[0].error) {
      el.innerHTML = "<p class=\"row warn\">" + results[0].error + "</p>";
    } else {
      el.innerHTML = results.map(function (r) {
        var dc = r.dc || "?";
        var cls = r.status === "ws" ? "ok" : "warn";
        var msg = r.status === "ws"
          ? "DC" + dc + "  ·  WebSocket ✓  (" + (r.ms || 0) + " ms)"
          : "DC" + dc + "  ·  WebSocket ✗  → TCP fallback";
        return "<p class=\"row " + cls + "\">" + msg + "</p>";
      }).join("");
    }
    el.hidden = false;
  }

  function loadSettingsForm() {
    if (!api || !api.get_config) return;
    api.get_config().then(function (c) {
      document.getElementById("settings-host").value = c.host || "";
      document.getElementById("settings-port").value = c.port || 1080;
      document.getElementById("settings-dc").value = (c.dc_ip || []).join("\n");
      document.getElementById("settings-verbose").checked = !!c.verbose;
      document.getElementById("settings-start-with-windows").checked = !!c.start_with_windows;
      var group = document.getElementById("form-group-startup");
      var hint = document.getElementById("settings-startup-hint");
      if (c.start_with_windows_available) {
        group.style.display = "";
        hint.hidden = true;
      } else {
        group.style.display = "";
        hint.hidden = false;
      }
    });
  }

  function bindButtons() {
    if (!api) return;

    var btnOpenTg = document.getElementById("btn-open-tg");
    if (btnOpenTg) btnOpenTg.addEventListener("click", function () {
      api.open_in_telegram().then(function (r) {
        if (r && !r.success && r.message) showToast(r.message, "warning");
      });
    });

    var btnCheck = document.getElementById("btn-check");
    if (btnCheck) btnCheck.addEventListener("click", function () {
      checkResultEl.hidden = true;
      api.check_connection().then(function (res) { renderCheckResult(checkResultEl, res); }).catch(function () {
        if (checkResultEl) {
          checkResultEl.innerHTML = "<p class=\"row warn\">Ошибка проверки</p>";
          checkResultEl.hidden = false;
        }
      });
    });

    document.getElementById("btn-settings-nav").addEventListener("click", function () { window.location.hash = "settings"; });
    var btnSettings = document.querySelector('.nav-link[href="#settings"]');
    if (btnSettings) { /* nav already has link */ }

    var btnLogs = document.getElementById("btn-logs");
    if (btnLogs) btnLogs.addEventListener("click", function () {
      api.open_logs().then(function (r) {
        if (r && !r.opened && r.message) showToast(r.message, "info");
      });
    });

    var btnRestart = document.getElementById("btn-restart");
    if (btnRestart) btnRestart.addEventListener("click", function () { api.restart_proxy(); });

    var btnMinimize = document.getElementById("btn-minimize");
    if (btnMinimize) btnMinimize.addEventListener("click", function () { api.minimize_to_tray(); });

    var btnQuit = document.getElementById("btn-quit");
    if (btnQuit) btnQuit.addEventListener("click", function () { api.quit_app(); });

    // First run
    var firstRunOpenTg = document.getElementById("first-run-open-tg");
    var btnFirstRunOk = document.getElementById("btn-first-run-ok");
    if (btnFirstRunOk) btnFirstRunOk.addEventListener("click", function () {
      api.complete_first_run(firstRunOpenTg ? firstRunOpenTg.checked : false);
      api.clear_startup_error();
      window.location.hash = "main";
      startStatusPoll();
    });

    // Settings form
    var form = document.getElementById("form-settings");
    var settingsError = document.getElementById("settings-error");
    var settingsCheckResult = document.getElementById("settings-check-result");
    if (form) form.addEventListener("submit", function (e) {
      e.preventDefault();
      settingsError.hidden = true;
      var host = document.getElementById("settings-host").value.trim();
      var port = parseInt(document.getElementById("settings-port").value, 10);
      var dcText = document.getElementById("settings-dc").value.trim();
      var dc_ip = dcText ? dcText.split("\n").map(function (l) { return l.trim(); }).filter(Boolean) : [];
      var cfg = {
        host: host,
        port: port,
        dc_ip: dc_ip,
        verbose: document.getElementById("settings-verbose").checked,
        start_with_windows: document.getElementById("settings-start-with-windows").checked
      };
      api.save_config(cfg).then(function (res) {
        if (res && res.ok) {
          showToast("Сохранено. При необходимости перезапустите прокси.", "success");
          window.location.hash = "main";
        } else if (res && res.error) {
          settingsError.textContent = res.error;
          settingsError.hidden = false;
        }
      }).catch(function () {
        settingsError.textContent = "Ошибка сохранения";
        settingsError.hidden = false;
      });
    });

    var btnSettingsCheck = document.getElementById("btn-settings-check");
    if (btnSettingsCheck) btnSettingsCheck.addEventListener("click", function () {
      var dcText = document.getElementById("settings-dc").value.trim();
      var dc_ip = dcText ? dcText.split("\n").map(function (l) { return l.trim(); }).filter(Boolean) : [];
      settingsCheckResult.hidden = true;
      api.check_connection(dc_ip).then(function (res) { renderCheckResult(settingsCheckResult, res); }).catch(function () {
        settingsCheckResult.innerHTML = "<p class=\"row warn\">Ошибка проверки</p>";
        settingsCheckResult.hidden = false;
      });
    });
  }

  function init() {
    applyHash();

    if (!api) {
      if (statusEl) statusEl.textContent = "Прокси запущен";
      return;
    }

    bindButtons();

    api.get_startup_error().then(function (err) {
      if (err && bannerStartup) {
        bannerStartup.textContent = err;
        bannerStartup.hidden = false;
      }
    });

    api.get_startup_warnings().then(function (warnings) {
      warnings.forEach(function (w) { showToast(w, "warning"); });
    });

    api.is_first_run().then(function (first) {
      if (first) {
        api.get_config().then(function (c) {
          var manual = document.getElementById("first-run-manual");
          if (manual) manual.textContent = "Или вручную: Настройки → Прокси → SOCKS5  " + (c.host || "127.0.0.1") + " : " + (c.port || 1080);
        });
        window.location.hash = "first-run";
      } else {
        startStatusPoll();
      }
    });

    window.addEventListener("hashchange", function () {
      if (getHash() === "settings") loadSettingsForm();
    });
    if (getHash() === "settings") loadSettingsForm();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
