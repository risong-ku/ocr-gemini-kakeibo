"use strict";

(() => {
  const { yen, todayJst, sessionExpired } = window.kakeiboUI;
  const byId = (id) => document.getElementById(id);
  let month = todayJst().slice(0, 7);
  let chart = null;
  let requestNumber = 0;
  let controller = null;
  const status = byId("dashboard-status");
  const categories = { child: "子ども費", couple: "夫婦生活費", excluded: "対象外" };
  let saved = new URLSearchParams(window.location.search).get("saved") === "1";
  if (saved) window.history.replaceState(null, "", "/dashboard");

  function drawChart(summary) {
    if (chart) chart.destroy();
    chart = null;
    byId("chart-status").hidden = typeof window.Chart === "function";
    if (typeof window.Chart !== "function") return;
    const [year, monthNumber] = month.split("-").map(Number);
    // Day zero of the following month accounts for leap years without local time.
    const date = new Date(0);
    date.setUTCFullYear(year, monthNumber, 0);
    const days = Array.from({ length: date.getUTCDate() }, (_, index) => index + 1);
    const daily = new Map(summary.daily.map((entry) => [entry.date, entry]));
    chart = new window.Chart(byId("daily-chart"), {
      type: "bar",
      data: {
        labels: days.map((day) => `${day}日`),
        datasets: [
          { label: "子ども費", data: days.map((day) => daily.get(`${month}-${String(day).padStart(2, "0")}`)?.child ?? 0), backgroundColor: "#25756b" },
          { label: "夫婦生活費", data: days.map((day) => daily.get(`${month}-${String(day).padStart(2, "0")}`)?.couple ?? 0), backgroundColor: "#ad6140" },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
        scales: {
          x: { stacked: true, grid: { display: false }, ticks: { maxTicksLimit: 8 } },
          y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } },
        },
        plugins: { tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${yen(context.parsed.y)}` } } },
      },
    });
  }

  function renderReceipts(receipts) {
    const list = byId("receipts");
    list.replaceChildren();
    if (!receipts.length) {
      const empty = document.createElement("p");
      empty.textContent = "この月のレシートはありません。";
      list.append(empty);
      return;
    }
    // The API already orders receipts by date and ID, and items by sequence.
    for (const receipt of receipts) {
      const row = document.createElement("article");
      row.className = "receipt-row";
      const details = document.createElement("details");
      const heading = document.createElement("summary");
      const label = document.createElement("span");
      label.textContent = `${Number(receipt.date.slice(5, 7))}/${Number(receipt.date.slice(8))} ${receipt.store || "（店名なし）"}`;
      const total = document.createElement("strong");
      total.textContent = yen(receipt.total);
      heading.append(label, total);
      const subtotals = document.createElement("span");
      subtotals.className = "receipt-subtotals";
      for (const [category, name] of Object.entries(categories)) {
        const amount = receipt.subtotals[category];
        if (category === "excluded" && amount === 0) continue;
        const subtotal = document.createElement("small");
        subtotal.textContent = `${name} ${yen(amount)}`;
        if (category === "excluded") subtotal.className = "excluded";
        subtotals.append(subtotal);
      }
      heading.append(subtotals);
      details.append(heading);
      const breakdown = document.createElement("ul");
      breakdown.className = "receipt-items";
      for (const item of receipt.items) {
        const entry = document.createElement("li");
        if (item.category === "excluded") entry.className = "excluded";
        const name = document.createElement("span");
        name.textContent = `${item.name || "（品目名なし）"}（${categories[item.category]}）`;
        const price = document.createElement("span");
        price.textContent = yen(item.price);
        entry.append(name, price);
        breakdown.append(entry);
      }
      details.append(breakdown);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "delete-receipt";
      remove.textContent = "削除";
      remove.setAttribute("aria-label", `${receipt.date} ${receipt.store} のレシートを削除`);
      remove.addEventListener("click", async () => {
        if (!window.confirm("このレシートを削除しますか？")) return;
        remove.disabled = true;
        status.textContent = "削除しています…";
        try {
          const response = await fetch(`/api/receipts/${encodeURIComponent(receipt.receipt_id)}`, { method: "DELETE" });
          if (sessionExpired(response)) {
            status.textContent = "削除できませんでした。ログインしてください。";
            return;
          }
          if (!response.ok && response.status !== 404) throw new Error("delete failed");
          await loadSummary();
        } catch {
          status.textContent = "削除できませんでした。接続を確認して、もう一度お試しください。";
        } finally {
          remove.disabled = false;
        }
      });
      row.append(details, remove);
      list.append(row);
    }
  }

  async function loadSummary() {
    const currentRequest = ++requestNumber;
    if (controller) controller.abort();
    controller = new AbortController();
    const [year, monthNumber] = month.split("-").map(Number);
    byId("month").textContent = `${year}年${monthNumber}月`;
    byId("previous-month").disabled = month === "0001-01";
    byId("next-month").disabled = month === "9999-12";
    byId("export-month").href = `/export.csv?month=${month}`;
    byId("summary-content").hidden = true;
    byId("retry-summary").hidden = true;
    status.textContent = saved ? "保存しました。集計を読み込んでいます…" : "集計を読み込んでいます…";
    try {
      const response = await fetch(`/api/summary?month=${month}`, { signal: controller.signal });
      if (currentRequest !== requestNumber) return;
      if (sessionExpired(response)) {
        status.textContent = "集計を表示するにはログインしてください。";
        return;
      }
      if (!response.ok) throw new Error("summary failed");
      const summary = await response.json();
      if (currentRequest !== requestNumber) return;
      byId("child-total").textContent = yen(summary.totals.child);
      byId("couple-total").textContent = yen(summary.totals.couple);
      renderReceipts(summary.receipts);
      byId("summary-content").hidden = false;
      // A CDN/chart failure must leave the totals, export and receipts usable.
      try {
        drawChart(summary);
      } catch {
        byId("chart-status").hidden = false;
      }
      status.textContent = saved ? "保存しました" : "";
      saved = false;
    } catch (error) {
      if (currentRequest !== requestNumber || error.name === "AbortError") return;
      status.textContent = "集計を読み込めませんでした。接続を確認して再読み込みしてください。";
      byId("retry-summary").hidden = false;
    }
  }

  function switchMonth(offset) {
    const [year, monthNumber] = month.split("-").map(Number);
    const index = year * 12 + monthNumber - 1 + offset;
    month = `${String(Math.floor(index / 12)).padStart(4, "0")}-${String(index % 12 + 1).padStart(2, "0")}`;
    loadSummary();
  }
  byId("previous-month").addEventListener("click", () => switchMonth(-1));
  byId("next-month").addEventListener("click", () => switchMonth(1));
  byId("retry-summary").addEventListener("click", loadSummary);
  loadSummary();
})();
