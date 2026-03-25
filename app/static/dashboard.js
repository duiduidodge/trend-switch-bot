const els = {
  envPill: document.getElementById("envPill"),
  dryRunPill: document.getElementById("dryRunPill"),
  venuePill: document.getElementById("venuePill"),
  scanCadence: document.getElementById("scanCadence"),
  monitorCadence: document.getElementById("monitorCadence"),
  accountValue: document.getElementById("accountValue"),
  dailyPnl: document.getElementById("dailyPnl"),
  riskPosture: document.getElementById("riskPosture"),
  lossLimitDetail: document.getElementById("lossLimitDetail"),
  positionsCount: document.getElementById("positionsCount"),
  ordersCount: document.getElementById("ordersCount"),
  positionsGrid: document.getElementById("positionsGrid"),
  positionSummary: document.getElementById("positionSummary"),
  signalMatrix: document.getElementById("signalMatrix"),
  latestActions: document.getElementById("latestActions"),
  openOrders: document.getElementById("openOrders"),
  logStream: document.getElementById("logStream"),
  lastRefresh: document.getElementById("lastRefresh"),
  refreshButton: document.getElementById("refreshButton"),
  toastZone: document.getElementById("toastZone"),
};

const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const number = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 2,
});

function pct(value) {
  return `${value >= 0 ? "+" : ""}${number.format(value)}%`;
}

function formatDate(value) {
  if (!value) return "No timestamp";
  const date = new Date(value);
  return date.toLocaleString();
}

function toast(title, body) {
  const node = document.createElement("div");
  node.className = "toast";
  node.innerHTML = `<strong>${title}</strong><span>${body}</span>`;
  els.toastZone.appendChild(node);
  setTimeout(() => node.remove(), 3200);
}

async function callJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  return response.json();
}

function renderOverview(data) {
  const overview = data.overview;
  els.envPill.textContent = overview.env;
  els.dryRunPill.textContent = overview.dry_run ? "Paper" : "Live";
  els.venuePill.textContent = overview.hyperliquid_env;
  els.scanCadence.textContent = `${data.controls.signal_scan_interval_minutes} min`;
  els.monitorCadence.textContent = `${data.controls.monitor_interval_minutes} min`;

  els.accountValue.textContent = money.format(overview.account_value);
  els.dailyPnl.textContent = money.format(overview.daily_closed_pnl);
  els.lossLimitDetail.textContent = money.format(overview.daily_loss_limit);
  els.positionsCount.textContent = `${overview.positions_count} / ${overview.max_positions}`;
  els.ordersCount.textContent = `${overview.open_orders_count}`;

  if (overview.halted_by_loss_limit) {
    els.riskPosture.textContent = "Halted";
  } else if (overview.positions_count > 0) {
    els.riskPosture.textContent = "Deployed";
  } else {
    els.riskPosture.textContent = "Flat";
  }
}

function positionCard(position) {
  const pnlClass = position.unrealized_pnl_pct >= 0 ? "pnl-pill pnl-pill--profit" : "pnl-pill pnl-pill--loss";
  const directionClass = position.direction === "LONG" ? "direction-pill direction-pill--long" : "direction-pill direction-pill--short";
  return `
    <article class="position-card">
      <div class="position-top">
        <div class="position-asset">
          <span class="${directionClass}">${position.direction}</span>
          <strong>${position.asset}</strong>
          <span>Entry ${money.format(position.entry_price)} · Mark ${money.format(position.current_price)}</span>
        </div>
        <span class="${pnlClass}">${pct(position.unrealized_pnl_pct)} · ${money.format(position.unrealized_pnl_usd)}</span>
      </div>
      <div class="position-stats">
        <div class="stat-box">
          <span class="summary-label">Size</span>
          <strong>${number.format(position.size_asset)} ${position.asset}</strong>
        </div>
        <div class="stat-box">
          <span class="summary-label">Notional</span>
          <strong>${money.format(position.size_usd)}</strong>
        </div>
        <div class="stat-box">
          <span class="summary-label">Leverage</span>
          <strong>${number.format(position.leverage)}x</strong>
        </div>
        <div class="stat-box">
          <span class="summary-label">Margin Used</span>
          <strong>${money.format(position.margin_used)}</strong>
        </div>
      </div>
      <div class="position-footer">
        <span class="table-sub">Live risk block for ${position.asset}</span>
        <button class="flatten-button" data-close-asset="${position.asset}">Flatten ${position.asset}</button>
      </div>
    </article>
  `;
}

function renderPositions(data) {
  const positions = data.positions || [];
  const positionLabel = data.overview?.dry_run ? "paper" : "live";
  els.positionSummary.textContent = positions.length ? `${positions.length} ${positionLabel} position${positions.length > 1 ? "s" : ""}` : `No ${positionLabel} positions`;
  if (!positions.length) {
    els.positionsGrid.innerHTML = `<div class="empty-state">The book is flat. Run a signal scan or wait for the next scheduled cycle.</div>`;
    return;
  }
  els.positionsGrid.innerHTML = positions.map(positionCard).join("");
  for (const button of els.positionsGrid.querySelectorAll("[data-close-asset]")) {
    button.addEventListener("click", async () => {
      const asset = button.getAttribute("data-close-asset");
      await runControl(`/api/positions/${asset}/close`, `Closed ${asset}`, `${asset} flatten request sent.`);
    });
  }
}

function renderSignalMatrix(data) {
  const entries = [
    ["BTC LONG", data.latest_signal_state.BTC_LONG],
    ["BTC SHORT", data.latest_signal_state.BTC_SHORT],
    ["ETH LONG", data.latest_signal_state.ETH_LONG],
    ["ETH SHORT", data.latest_signal_state.ETH_SHORT],
    ["PAXG LONG", data.latest_signal_state.PAXG_LONG],
    ["PAXG SHORT", data.latest_signal_state.PAXG_SHORT],
  ];
  els.signalMatrix.innerHTML = `
    <div class="table-head">Signal · Last latch · Status</div>
    ${entries
    .map(([label, ts]) => {
      const ready = ts ? "Latched" : "Awaiting first run";
      return `
        <div class="table-row">
          <div class="row-grid row-grid--signal">
            <span class="mono">${label}</span>
            <span class="table-sub">${ts ? formatDate(ts) : "No completed candle recorded yet"}</span>
            <span class="state-pill ${ts ? "state-pill--active" : "state-pill--idle"}">${ready}</span>
          </div>
        </div>
      `;
    })
    .join("")}
  `;
}

function renderLatestActions(data) {
  const entries = Object.entries(data.latest_actions || {});
  if (!entries.length) {
    els.latestActions.innerHTML = `<div class="empty-state">No decisions logged yet. The first signal or monitor cycle will populate this feed.</div>`;
    return;
  }
  els.latestActions.innerHTML = `
    <div class="table-head">Asset · Action · Reason · Time</div>
    ${entries
    .map(
      ([asset, item]) => `
        <div class="table-row">
          <div class="row-grid row-grid--action">
            <span class="mono">${asset}</span>
            <span class="state-pill state-pill--active">${item.action}</span>
            <div>
              <strong>${item.reason || "No operator note attached."}</strong>
              <div class="table-sub">${item.category}${item.direction ? ` · ${item.direction}` : ""}</div>
            </div>
            <span class="table-sub">${formatDate(item.created_at)}</span>
          </div>
        </div>
      `,
    )
    .join("")}
  `;
}

function renderOrders(data) {
  const orders = data.open_orders || [];
  if (!orders.length) {
    els.openOrders.innerHTML = `<div class="empty-state">No resting ${data.overview?.dry_run ? "paper" : "exchange"} orders are active.</div>`;
    return;
  }
  els.openOrders.innerHTML = `
    <div class="table-head">Asset · Side · Size · Trigger</div>
    ${orders
    .map(
      (order) => `
        <div class="table-row">
          <div class="row-grid row-grid--order">
            <span class="mono">${order.coin}</span>
            <span class="mono">${order.side === "B" ? "BUY" : "SELL"}</span>
            <span>${order.sz}</span>
            <span class="mono">${order.triggerPx || order.limitPx || "-"}</span>
          </div>
        </div>
      `,
    )
    .join("")}
  `;
}

function renderLogs(data) {
  const logs = data.recent_logs || [];
  if (!logs.length) {
    els.logStream.innerHTML = `<div class="empty-state">No logs in storage yet.</div>`;
    return;
  }
  els.logStream.innerHTML = `
    <div class="table-head">Type · Asset · Message · Time</div>
    ${logs
    .map(
      (log) => `
        <div class="table-row">
          <div class="row-grid row-grid--log">
            <span class="mono">${log.category}</span>
            <span class="mono">${log.asset || "SYSTEM"}</span>
            <span>${log.payload.reason || log.payload.message || log.action}</span>
            <span class="table-sub">${formatDate(log.created_at)}</span>
          </div>
        </div>
      `,
    )
    .join("")}
  `;
}

function render(data) {
  renderOverview(data);
  renderPositions(data);
  renderSignalMatrix(data);
  renderLatestActions(data);
  renderOrders(data);
  renderLogs(data);
  els.lastRefresh.textContent = new Date().toLocaleTimeString();
}

async function refresh() {
  const data = await callJson("/api/dashboard");
  render(data);
}

async function runControl(url, title, body) {
  try {
    await callJson(url, { method: "POST" });
    toast(title, body);
    await refresh();
  } catch (error) {
    toast("Request failed", error.message);
  }
}

document.querySelectorAll("[data-action='signals']").forEach((button) => {
  button.addEventListener("click", () => runControl("/api/run/signals", "Signal scan complete", "The bot re-evaluated BTC, ETH, and PAXG entries."));
});

document.querySelectorAll("[data-action='monitor']").forEach((button) => {
  button.addEventListener("click", () => runControl("/api/run/monitor", "Monitor pass complete", "Open positions were re-checked against current regimes."));
});

els.refreshButton.addEventListener("click", async () => {
  try {
    await refresh();
    toast("Surface refreshed", "Latest account, position, and log data loaded.");
  } catch (error) {
    toast("Refresh failed", error.message);
  }
});

refresh().catch((error) => toast("Initial load failed", error.message));
setInterval(() => refresh().catch(() => {}), 15000);
