const RED = "#ff4d4f";
const GREEN = "#00b578";
const GOLD = "#f0b90b";
const BLUE = "#5aa2ff";
const state = {
  interval: localStorage.getItem("paper-spot-interval") || "1d",
  ind1: localStorage.getItem("paper-spot-ind1") || "volume",
  ind2: localStorage.getItem("paper-spot-ind2") || "macd",
  payload: null,
  charts: [],
  lock: false,
  viewStart: "",
  viewEnd: "",
  catchupFor: "",
};

const $ = (id) => document.getElementById(id);

function boot() {
  restoreField("venue", "paper-spot-venue");
  restoreField("symbol", "paper-spot-symbol");
  restoreField("sync-start", "paper-spot-sync-start");
  restoreField("fast", "paper-spot-fast");
  restoreField("slow", "paper-spot-slow");
  $("ind1").value = state.ind1;
  $("ind2").value = state.ind2;
  document.querySelectorAll("#periods button").forEach((button) => {
    button.classList.toggle("on", button.dataset.interval === state.interval);
    button.addEventListener("click", () => {
      markInterval(button.dataset.interval);
      state.viewStart = "";
      state.viewEnd = "";
      state.catchupFor = "";
      loadChart();
    });
  });
  $("ind1").addEventListener("change", () => rememberIndicator("ind1"));
  $("ind2").addEventListener("change", () => rememberIndicator("ind2"));
  $("sync").addEventListener("click", syncBars);
  $("scan").addEventListener("click", scanGrid);
  $("validate").addEventListener("click", validateWindow);
  $("fast").addEventListener("change", () => {
    rememberLibrary();
    loadChart();
  });
  $("slow").addEventListener("change", () => {
    rememberLibrary();
    loadChart();
  });
  $("venue").addEventListener("change", () => {
    rememberLibrary();
    state.catchupFor = "";
    chooseStoredInterval().then(loadChart);
  });
  $("symbol").addEventListener("change", () => {
    rememberLibrary();
    state.catchupFor = "";
    chooseStoredInterval().then(loadChart);
  });
  $("rank-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr");
    if (!row || !row.dataset.fast) return;
    $("fast").value = row.dataset.fast;
    $("slow").value = row.dataset.slow;
    state.viewStart = $("search-start").value;
    state.viewEnd = $("search-end").value;
    document.querySelectorAll("#rank-body tr").forEach((item) => item.classList.remove("picked"));
    row.classList.add("picked");
    loadChart();
  });
  window.addEventListener("resize", resizeCharts);
  chooseStoredInterval().then(loadChart);
}

function restoreField(id, key) {
  const value = localStorage.getItem(key);
  if (value) $(id).value = value;
}

function rememberLibrary() {
  localStorage.setItem("paper-spot-venue", $("venue").value);
  localStorage.setItem("paper-spot-symbol", $("symbol").value.trim().toUpperCase());
  localStorage.setItem("paper-spot-sync-start", $("sync-start").value);
  localStorage.setItem("paper-spot-fast", $("fast").value);
  localStorage.setItem("paper-spot-slow", $("slow").value);
}

function markInterval(interval) {
  state.interval = interval;
  localStorage.setItem("paper-spot-interval", interval);
  document.querySelectorAll("#periods button").forEach((item) => {
    item.classList.toggle("on", item.dataset.interval === interval);
  });
}

async function chooseStoredInterval() {
  try {
    const report = await request(
      "/api/research/coverage?venue=" +
        encodeURIComponent($("venue").value) +
        "&symbol=" +
        encodeURIComponent($("symbol").value.trim().toUpperCase())
    );
    const rows = report.intervals || [];
    const current = rows.find((row) => row.interval === state.interval);
    if (current && current.count) return;
    const ready = rows.find((row) => row.count);
    if (ready) markInterval(ready.interval);
  } catch (error) {
    // 覆盖接口失败时仍尝试画已有图。
  }
}

function rememberIndicator(id) {
  state[id] = $(id).value;
  localStorage.setItem(id === "ind1" ? "paper-spot-ind1" : "paper-spot-ind2", state[id]);
  drawPanes();
}

function queryBase() {
  const params = new URLSearchParams({
    venue: $("venue").value,
    symbol: $("symbol").value.trim().toUpperCase(),
    interval: state.interval,
    fast: $("fast").value,
    slow: $("slow").value,
  });
  if (state.viewStart) params.set("start", state.viewStart);
  if (state.viewEnd) params.set("end", state.viewEnd);
  return params;
}

async function request(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({ error: "响应不是 JSON" }));
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

async function loadChart() {
  setStatus("正在读取本地 K 线…");
  try {
    const payload = await request("/api/research/chart?" + queryBase().toString());
    state.payload = payload;
    drawAll();
    const coverage = payload.coverage || {};
    const when = coverage.count
      ? `本地 ${coverage.count} 根，图上 ${payload.count} 根${payload.truncated ? "（只显示最近一段）" : ""}`
      : "本地还没有这个周期。先补齐 K 线。";
    const gaps = (payload.gaps || []).length;
    const kept = coverage.count ? "下次打开直接使用这份库。" : "";
    setStatus(`${payload.symbol} ${payload.label} · ${when}${gaps ? ` · 图上有 ${gaps} 处缺口` : ""} ${kept}`.trim());
    $("sync").textContent = coverage.count ? "追加最新" : "补齐本地 K 线";
    renderSummary(payload.summary);
    const key = `${payload.venue}:${payload.symbol}:${payload.interval}`;
    if (coverage.count && state.catchupFor !== key) {
      state.catchupFor = key;
      catchUp(false);
    }
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function syncBars() {
  rememberLibrary();
  if (libraryCoversStart()) {
    state.catchupFor = "";
    await catchUp(true);
    return;
  }
  setStatus("正在从官方公开行情补本地 K 线…");
  const body = {
    venue: $("venue").value,
    symbol: $("symbol").value.trim().toUpperCase(),
    interval: state.interval,
    start: $("sync-start").value,
  };
  if ($("sync-end").value) body.end = $("sync-end").value;
  try {
    const result = await request("/api/research/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.catchupFor = `${body.venue}:${body.symbol}:${body.interval}`;
    setStatus(`已写入 ${result.fetched} 根。本地共 ${result.coverage.count} 根。下次打开直接使用。`);
    await loadChart();
  } catch (error) {
    setStatus(error.message, true);
  }
}

function libraryCoversStart() {
  const coverage = state.payload && state.payload.coverage;
  if (!coverage || !coverage.count || coverage.first_open_ms == null) return false;
  if (coverage.symbol !== $("symbol").value.trim().toUpperCase()) return false;
  if (coverage.interval !== state.interval) return false;
  if (coverage.venue !== $("venue").value) return false;
  const startMs = Date.parse(`${$("sync-start").value}T00:00:00Z`);
  return Number.isFinite(startMs) && startMs >= coverage.first_open_ms;
}

async function catchUp(manual) {
  const venue = $("venue").value;
  const symbol = $("symbol").value.trim().toUpperCase();
  try {
    const result = await request("/api/research/catchup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ venue, symbol, interval: state.interval }),
    });
    state.catchupFor = `${venue}:${symbol}:${state.interval}`;
    if (result.fetched) {
      setStatus(`追加了 ${result.fetched} 根。本地共 ${result.coverage.count} 根。`);
      await loadChart();
      return;
    }
    if (manual) {
      setStatus(`本地库已是最新，共 ${result.coverage.count} 根。下次打开直接使用。`);
    }
  } catch (error) {
    if (manual) setStatus(error.message, true);
  }
}

async function scanGrid() {
  setStatus("正在扫描本地窗口…");
  try {
    const result = await request("/api/research/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        venue: $("venue").value,
        symbol: $("symbol").value.trim().toUpperCase(),
        interval: state.interval,
        start: $("search-start").value,
        end: $("search-end").value,
      }),
    });
    renderRank(result.rows || []);
    setStatus(`扫描完成，窗口 ${result.count} 根，吃单费率 ${result.taker_rate}。`);
    if (result.rows && result.rows.length) {
      $("fast").value = result.rows[0].fast;
      $("slow").value = result.rows[0].slow;
      state.viewStart = $("search-start").value;
      state.viewEnd = $("search-end").value;
      await loadChart();
    }
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function validateWindow() {
  setStatus("正在做样本外验证…");
  try {
    const result = await request("/api/research/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        venue: $("venue").value,
        symbol: $("symbol").value.trim().toUpperCase(),
        interval: state.interval,
        fast: Number($("fast").value),
        slow: Number($("slow").value),
        search_start: $("search-start").value,
        search_end: $("search-end").value,
        valid_start: $("valid-start").value,
        valid_end: $("valid-end").value,
      }),
    });
    const search = result.search;
    const validation = result.validation;
    $("summary").innerHTML =
      `实验 ${result.id}。扫描窗收益 ${pct(search.return_rate)}，验证窗收益 ${pct(validation.return_rate)}，` +
      `验证窗回撤 ${plainPct(validation.max_drawdown)}，费用 ${numberText(validation.fees)}，成交 ${validation.trades} 笔。` +
      `<br>${result.note}`;
    state.viewStart = $("valid-start").value;
    state.viewEnd = $("valid-end").value;
    setStatus(`已保存实验 ${result.id}。图上是验证窗口。`);
    await loadChart();
  } catch (error) {
    setStatus(error.message, true);
  }
}

function renderRank(rows) {
  $("rank-body").innerHTML = rows
    .map(
      (row) => `<tr class="clickable" data-fast="${row.fast}" data-slow="${row.slow}">
        <td>${row.fast} / ${row.slow}</td>
        <td class="${Number(row.return_rate) >= 0 ? "up" : "down"}">${pct(row.return_rate)}</td>
        <td>${plainPct(row.max_drawdown)}</td>
        <td>${numberText(row.fees)}</td>
        <td>${row.trades}</td>
        <td>${numberText(row.ending_equity)}</td>
      </tr>`
    )
    .join("");
}

function renderSummary(summary) {
  if (!summary) {
    $("summary").textContent = "这个窗口还不能计算双均线。补齐 K 线并保证慢线长于快线。";
    return;
  }
  $("summary").textContent =
    `当前图上收益 ${pct(summary.return_rate)}，最大回撤 ${plainPct(summary.max_drawdown)}，` +
    `费用 ${numberText(summary.fees)}，成交 ${summary.trades} 笔，期末权益 ${numberText(summary.ending_equity)}。` +
    `吃单费率 ${summary.taker_rate}。未成交的信号仍是三角，平仓圆点按盈亏着色。`;
}

function drawAll() {
  clearCharts();
  const payload = state.payload;
  if (!payload || !payload.candles.length) {
    $("legend").textContent = "本地没有可画的 K 线。";
    return;
  }
  const main = createChart($("pane-main"), false);
  const candles = main.addCandlestickSeries({
    upColor: RED,
    downColor: GREEN,
    borderUpColor: RED,
    borderDownColor: GREEN,
    wickUpColor: RED,
    wickDownColor: GREEN,
  });
  candles.setData(payload.candles);
  candles.setMarkers(buildMarkers(payload));
  const fast = main.addLineSeries({ color: GOLD, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  const slow = main.addLineSeries({ color: BLUE, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  fast.setData(payload.sma_fast);
  slow.setData(payload.sma_slow);
  state.charts.push(main);
  linkChart(main, candles);
  main.timeScale().fitContent();
  drawPanes();
  showLegend(null);
  requestAnimationFrame(resizeCharts);
}

function drawPanes() {
  state.lock = true;
  state.charts.slice(1).forEach((chart) => chart.remove());
  state.charts = state.charts.slice(0, 1);
  state.lock = false;
  if (!state.payload || !state.payload.candles.length || !state.charts[0]) return;
  addIndicator($("pane-1"), state.ind1, false);
  addIndicator($("pane-2"), state.ind2, true);
  const range = state.charts[0].timeScale().getVisibleRange();
  if (range) {
    state.lock = true;
    state.charts.slice(1).forEach((chart) => chart.timeScale().setVisibleRange(range));
    state.lock = false;
  }
  requestAnimationFrame(resizeCharts);
}

function addIndicator(container, name, showTime) {
  const chart = createChart(container, showTime);
  const series = paintIndicator(chart, name, state.payload);
  state.charts.push(chart);
  linkChart(chart, series);
}

function paintIndicator(chart, name, payload) {
  if (name === "volume") {
    const hist = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceLineVisible: false });
    hist.setData(payload.volume.map((item) => ({ time: item.time, value: item.value, color: item.up ? RED : GREEN })));
    const average = chart.addLineSeries({ color: GOLD, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    average.setData(payload.volume_ma);
    return hist;
  }
  if (name === "macd") {
    const hist = chart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    hist.setData(payload.macd.hist.map((item) => ({ time: item.time, value: item.value, color: item.up ? RED : GREEN })));
    chart.addLineSeries({ color: GOLD, lineWidth: 1, priceLineVisible: false }).setData(payload.macd.dif);
    chart.addLineSeries({ color: BLUE, lineWidth: 1, priceLineVisible: false }).setData(payload.macd.dea);
    return hist;
  }
  if (name === "rsi") {
    const line = chart.addLineSeries({ color: GOLD, lineWidth: 1, priceLineVisible: false });
    line.setData(payload.rsi);
    level(line, 30);
    level(line, 70);
    return line;
  }
  if (name === "kdj") {
    const k = chart.addLineSeries({ color: GOLD, lineWidth: 1, priceLineVisible: false });
    chart.addLineSeries({ color: BLUE, lineWidth: 1, priceLineVisible: false }).setData(payload.kdj.d);
    chart.addLineSeries({ color: "#c084fc", lineWidth: 1, priceLineVisible: false }).setData(payload.kdj.j);
    k.setData(payload.kdj.k);
    level(k, 20);
    level(k, 80);
    return k;
  }
  if (name === "atr") {
    const line = chart.addLineSeries({ color: GOLD, lineWidth: 1, priceLineVisible: false });
    line.setData(payload.atr);
    return line;
  }
  const line = chart.addLineSeries({ color: BLUE, lineWidth: 1, priceLineVisible: false });
  line.setData(payload.obv);
  return line;
}

function level(series, price) {
  series.createPriceLine({
    price,
    color: "#5c6b7a",
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
    title: "",
  });
}

function createChart(container, showTime) {
  return LightweightCharts.createChart(container, {
    width: container.clientWidth || 800,
    height: container.clientHeight || 160,
    layout: { background: { color: "#0b0e11" }, textColor: "#c3cdd8", attributionLogo: false },
    grid: { vertLines: { color: "#1b242e" }, horzLines: { color: "#1b242e" } },
    rightPriceScale: { borderColor: "#2a3542" },
    timeScale: {
      borderColor: "#2a3542",
      visible: showTime,
      timeVisible: state.interval !== "1d",
      secondsVisible: false,
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    handleScroll: { vertTouchDrag: false },
  });
}

function linkChart(chart, series) {
  chart.timeScale().subscribeVisibleTimeRangeChange((range) => {
    if (state.lock || !range) return;
    state.lock = true;
    state.charts.forEach((other) => {
      if (other === chart) return;
      const current = other.timeScale().getVisibleRange();
      if (current && Math.abs(current.from - range.from) < 1 && Math.abs(current.to - range.to) < 1) return;
      other.timeScale().setVisibleRange(range);
    });
    state.lock = false;
  });
  chart.subscribeCrosshairMove((param) => {
    if (state.lock) return;
    state.lock = true;
    showLegend(param);
    state.charts.forEach((other) => {
      if (other === chart) return;
      if (!param || param.time == null) other.clearCrosshairPosition();
      else other.setCrosshairPosition(0, param.time, seriesOf(other));
    });
    state.lock = false;
  });
  chart.__series = series;
}

function seriesOf(chart) {
  return chart.__series;
}

function clearCharts() {
  state.charts.forEach((chart) => chart.remove());
  state.charts = [];
}

function resizeCharts() {
  const boxes = [$("pane-main"), $("pane-1"), $("pane-2")];
  state.charts.forEach((chart, index) => {
    const box = boxes[index];
    if (!box) return;
    chart.applyOptions({ width: box.clientWidth, height: box.clientHeight });
  });
}

function buildMarkers(payload) {
  const items = [];
  payload.signals.forEach((signal) => {
    const buy = signal.side === "BUY";
    items.push({
      time: signal.time,
      position: buy ? "belowBar" : "aboveBar",
      color: buy ? RED : GREEN,
      shape: buy ? "arrowUp" : "arrowDown",
      text: buy ? "金叉" : "死叉",
    });
  });
  payload.fills.forEach((fill) => {
    const buy = fill.side === "BUY";
    const pnl = fill.pnl === "" ? null : Number(fill.pnl);
    const color = buy ? "#d7dde6" : pnl != null && pnl < 0 ? GREEN : RED;
    items.push({
      time: fill.time,
      position: buy ? "belowBar" : "aboveBar",
      color,
      shape: "circle",
      text: buy ? "买入" : "卖出",
    });
  });
  items.sort((left, right) => left.time - right.time);
  const unique = [];
  items.forEach((item) => {
    const previous = unique[unique.length - 1];
    if (previous && previous.time === item.time) {
      previous.text = `${previous.text} ${item.text}`;
      return;
    }
    unique.push(item);
  });
  return unique;
}

function showLegend(param) {
  const payload = state.payload;
  if (!payload || !payload.candles.length) return;
  const time = param && param.time != null ? param.time : payload.candles[payload.candles.length - 1].time;
  const index = payload.candles.findIndex((item) => item.time === time);
  const candle = index >= 0 ? payload.candles[index] : payload.candles[payload.candles.length - 1];
  const previous = index > 0 ? payload.candles[index - 1] : null;
  const volume = payload.volume.find((item) => item.time === candle.time);
  const change = previous ? candle.close - previous.close : candle.close - candle.open;
  const base = previous ? previous.close : candle.open;
  const percent = base ? (change / base) * 100 : 0;
  const tone = change >= 0 ? "up" : "down";
  $("legend").innerHTML =
    `<b>${payload.symbol}</b> 开 ${priceText(candle.open)} 高 ${priceText(candle.high)} ` +
    `低 ${priceText(candle.low)} 收 ${priceText(candle.close)} ` +
    `<span class="${tone}">${change >= 0 ? "+" : ""}${priceText(change)} ${percent >= 0 ? "+" : ""}${percent.toFixed(2)}%</span> ` +
    `量 ${volume ? compact(volume.value) : "—"}`;
}

function setStatus(text, failed) {
  const node = $("status");
  node.textContent = text;
  node.classList.toggle("error", Boolean(failed));
}

function pct(value) {
  const number = Number(value) * 100;
  const sign = number > 0 ? "+" : "";
  return `${sign}${number.toFixed(2)}%`;
}

function plainPct(value) {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function numberText(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}

function priceText(value) {
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: value >= 100 ? 2 : 0,
    maximumFractionDigits: value >= 100 ? 2 : 6,
  });
}

function compact(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(2)}K`;
  return numberText(value);
}

boot();
