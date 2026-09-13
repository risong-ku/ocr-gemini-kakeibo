"use strict";

function discountCategoryWarnings(items) {
  const labels = { child: "子ども費", couple: "夫婦生活費", excluded: "対象外" };
  const positiveCategories = new Set(items.filter((item) => item.price > 0).map((item) => item.category));
  return items
    .filter((item) => item.price < 0 && !positiveCategories.has(item.category))
    .map((item) => {
      const label = labels[item.category];
      return `値引き行（${item.name} ${item.price}円）が ${label} にありますが、${label} の品目がありません。値引きは対象品目と同じカテゴリにしてください。`;
    });
}

// Mirrors services/gemini.py: the same rates and tolerance produce the same hint.
const TAX_RATES = [1.1, 1.08];
const TAX_TOLERANCE_YEN = 1;

function taxHint(sum, total) {
  for (const rate of TAX_RATES) {
    if (Math.abs(Math.round(sum * rate) - total) <= TAX_TOLERANCE_YEN) {
      return `（品目が税抜表示の可能性: ×${rate.toFixed(2)} で一致）`;
    }
  }
  return "";
}

window.kakeiboUI = (() => {
  const yen = (value) => `${value.toLocaleString("ja-JP")}円`;
  const todayJst = () => {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(new Date());
    const part = (type) => parts.find((entry) => entry.type === type).value;
    return `${part("year")}-${part("month")}-${part("day")}`;
  };
  const sessionExpired = (response) => {
    if (response.status !== 401) return false;
    const message = document.getElementById("session-expired");
    message.hidden = false;
    message.querySelector("a").focus();
    return true;
  };
  return { yen, todayJst, sessionExpired };
})();

(() => {
  const form = document.getElementById("confirmation-form");
  if (!form) return;
  const { yen, todayJst, sessionExpired } = window.kakeiboUI;
  const byId = (id) => document.getElementById(id);
  const fileInput = byId("file");
  const items = byId("items");
  const status = byId("save-status");
  let imageBlob = null;
  let clientToken = null;
  let parsing = false;
  let saving = false;
  let itemsSnapshot = null;
  let serverWarnings = [];

  function resetCollapse() {
    itemsSnapshot = null;
    byId("collapse-items").textContent = "1行にまとめる";
  }

  function showView(view) {
    for (const name of ["upload", "parsing", "confirmation"]) {
      byId(`${name}-view`).hidden = name !== view;
    }
  }

  function integer(input) {
    const value = input.value.trim();
    return /^-?[0-9]+$/.test(value) && Number.isSafeInteger(Number(value))
      ? Number(value) : null;
  }

  function toggleSign(input) {
    input.value = input.value.startsWith("-") ? input.value.slice(1) : `-${input.value || "0"}`;
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function updateTotals() {
    const sums = { child: 0, couple: 0, excluded: 0 };
    const currentItems = [];
    let uncertain = 0;
    let valid = true;
    for (const row of items.children) {
      const price = integer(row.querySelector(".item-price"));
      currentItems.push({ name: row.querySelector(".item-name").value, price, category: row.dataset.category });
      if (price === null) valid = false;
      sums[row.dataset.category] += price ?? 0;
      if (row.classList.contains("uncertain")) uncertain += 1;
    }
    for (const category of Object.keys(sums)) {
      byId(`${category}-subtotal`).textContent = yen(sums[category]);
    }
    const sum = sums.child + sums.couple + sums.excluded;
    byId("items-total").textContent = yen(sum);
    const total = integer(byId("total"));
    byId("total-warning").hidden = !valid || total === null || sum === total;
    byId("total-warning").textContent =
      `品目合計 ${yen(sum)} がレシート合計 ${total === null ? "—" : yen(total)} と一致しません`
      + (total === null ? "" : taxHint(sum, total));
    const warnings = byId("warnings");
    warnings.replaceChildren();
    for (const warning of [...serverWarnings, ...discountCategoryWarnings(currentItems)]) {
      const message = document.createElement("p");
      message.textContent = warning;
      warnings.append(message);
    }
    warnings.hidden = !warnings.children.length;
    byId("uncertain-count").hidden = uncertain === 0;
    byId("uncertain-count").textContent = `要確認 ${uncertain} 件（黄色の行）`;
    byId("add-item").disabled = items.children.length >= 999;
  }

  function addItem(item = { name: "", price: 0, category: "couple", uncertain: false }) {
    const row = byId("item-template").content.firstElementChild.cloneNode(true);
    row.querySelector(".item-name").value = item.name;
    row.querySelector(".item-price").value = item.price;
    row.dataset.category = item.category;
    row.classList.toggle("uncertain", item.uncertain);
    for (const button of row.querySelectorAll("[data-category]")) {
      button.setAttribute("aria-pressed", String(button.dataset.category === item.category));
      button.addEventListener("click", () => {
        row.dataset.category = button.dataset.category;
        row.classList.remove("uncertain");
        for (const choice of row.querySelectorAll("[data-category]")) {
          choice.setAttribute("aria-pressed", String(choice === button));
        }
        updateTotals();
      });
    }
    row.querySelector(".delete-item").addEventListener("click", () => {
      const next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      updateTotals();
      (next?.querySelector(".item-name") || byId("add-item")).focus();
    });
    row.querySelector(".toggle-price-sign").addEventListener("click", () => {
      toggleSign(row.querySelector(".item-price"));
    });
    items.append(row);
    return row;
  }

  // crypto.randomUUID is only available in secure contexts (https/localhost);
  // over plain http on the LAN (iPhone testing) fall back to getRandomValues.
  function newClientToken() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function openConfirmation(draft) {
    resetCollapse();
    clientToken = newClientToken();
    byId("date").value = draft.date;
    byId("store").value = draft.store;
    byId("total").value = draft.total;
    items.replaceChildren();
    for (const item of draft.items) addItem(item);
    if (!draft.items.length) addItem();
    // The items-sum mismatch is recomputed live (with the tax hint) by
    // updateTotals, so drop the server copy instead of showing it twice.
    serverWarnings = (draft.warnings || []).filter(
      (warning) => !warning.startsWith("品目合計")
    );
    updateTotals();
    showView("confirmation");
    byId("confirmation-heading").focus();
  }

  async function resizeImage(file) {
    const url = URL.createObjectURL(file);
    const image = new Image();
    const canvas = document.createElement("canvas");
    try {
      await new Promise((resolve, reject) => {
        image.onload = resolve;
        image.onerror = () => reject(new Error("画像を読み込めませんでした。JPEG または PNG の画像を選び直してください。"));
        image.src = url;
      });
      if (!image.naturalWidth || !image.naturalHeight) throw new Error("画像を読み込めませんでした。");
      const scale = Math.min(1, 1600 / Math.max(image.naturalWidth, image.naturalHeight));
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      const context = canvas.getContext("2d");
      if (!context) throw new Error("画像を縮小できませんでした。画像を選び直してください。");
      context.fillStyle = "#fff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.8));
      if (!blob || blob.type !== "image/jpeg") throw new Error("画像を JPEG に変換できませんでした。画像を選び直してください。");
      if (blob.size > 5 * 1024 * 1024) throw new Error("画像が大きすぎます。範囲を小さくして撮り直してください。");
      return blob;
    } finally {
      URL.revokeObjectURL(url);
      canvas.width = 0;
      canvas.height = 0;
    }
  }

  async function parseFile() {
    const file = fileInput.files[0];
    if (!file || parsing) return;
    parsing = true;
    fileInput.disabled = true;
    status.textContent = "";
    showView("parsing");
    byId("parsing-view").focus();
    try {
      imageBlob = await resizeImage(file);
      const body = new FormData();
      body.append("file", imageBlob, "receipt.jpg");
      const response = await fetch("/api/receipts/parse", { method: "POST", body });
      if (sessionExpired(response)) {
        showView("upload");
        return;
      }
      if (response.status === 502) {
        openConfirmation({
          date: todayJst(), store: "", total: 0, items: [],
          warnings: ["解析できませんでした。内容を手入力して保存できます。"],
        });
      } else if (response.ok) {
        openConfirmation(await response.json());
      } else {
        throw new Error("解析できませんでした。画像を選び直して、もう一度お試しください。");
      }
    } catch (error) {
      showView("upload");
      status.textContent = error instanceof TypeError
        ? "通信に失敗しました。接続を確認して画像を選び直してください。" : error.message;
    } finally {
      parsing = false;
      fileInput.disabled = false;
      fileInput.value = "";
    }
  }

  function showValidationErrors(errors) {
    for (const error of errors) {
      const path = error.loc || [];
      const field = path[2];
      let input = null;
      if (["date", "store", "total"].includes(field)) input = byId(field);
      if (field === "items" && Number.isInteger(path[3])) {
        const row = items.children[path[3]];
        if (row && ["name", "price"].includes(path[4])) input = row.querySelector(`.item-${path[4]}`);
      }
      if (input) input.setCustomValidity("入力内容を確認してください。");
    }
    form.reportValidity();
  }

  fileInput.addEventListener("change", parseFile);
  byId("manual-entry").addEventListener("click", () => {
    if (parsing || saving) return;
    imageBlob = null;
    fileInput.value = "";
    status.textContent = "";
    openConfirmation({ date: todayJst(), store: "", total: 0, items: [], warnings: [] });
  });
  byId("upload-form").addEventListener("submit", (event) => {
    event.preventDefault();
    parseFile();
  });
  byId("add-item").addEventListener("click", () => {
    if (items.children.length >= 999) return;
    addItem().querySelector(".item-name").focus();
    updateTotals();
  });
  byId("collapse-items").addEventListener("click", () => {
    if (itemsSnapshot !== null) {
      items.replaceChildren();
      for (const item of itemsSnapshot) addItem(item);
      resetCollapse();
    } else {
      itemsSnapshot = Array.from(items.children, (row) => ({
        name: row.querySelector(".item-name").value,
        price: row.querySelector(".item-price").value,
        category: row.dataset.category,
        uncertain: row.classList.contains("uncertain"),
      }));
      const store = byId("store").value;
      items.replaceChildren();
      addItem({
        name: store || "まとめ",
        price: byId("total").value, category: "couple", uncertain: false,
      });
      byId("collapse-items").textContent = "内訳に戻す";
    }
    updateTotals();
  });
  form.addEventListener("input", (event) => {
    if (event.target.setCustomValidity) event.target.setCustomValidity("");
    updateTotals();
  });
  byId("date").addEventListener("change", () => byId("date").setCustomValidity(""));
  document.querySelector("[data-sign-target]").addEventListener("click", () => toggleSign(byId("total")));
  byId("restart").addEventListener("click", () => {
    if (saving) return;
    resetCollapse();
    imageBlob = null;
    clientToken = null;
    form.reset();
    for (const input of form.querySelectorAll("input")) input.setCustomValidity("");
    items.replaceChildren();
    serverWarnings = [];
    byId("warnings").replaceChildren();
    status.textContent = "";
    showView("upload");
    fileInput.focus();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || !clientToken) return;
    for (const input of form.querySelectorAll(".item-price, #total")) {
      input.setCustomValidity(integer(input) === null ? "金額は整数で入力してください（値引きは負数）。" : "");
    }
    if (!form.reportValidity()) return;
    if (!items.children.length || items.children.length > 999) {
      status.textContent = "品目は1件以上999件以下で入力してください。";
      byId("add-item").focus();
      return;
    }
    const payload = {
      client_token: clientToken, date: byId("date").value, store: byId("store").value,
      total: integer(byId("total")),
      items: Array.from(items.children, (row) => ({
        name: row.querySelector(".item-name").value,
        price: integer(row.querySelector(".item-price")), category: row.dataset.category,
      })),
    };
    const body = new FormData();
    body.append("payload", JSON.stringify(payload));
    if (imageBlob) body.append("file", imageBlob, "receipt.jpg");
    saving = true;
    byId("confirmation-fields").disabled = true;
    byId("save").disabled = true;
    byId("save").textContent = "保存中…";
    status.textContent = "保存しています…";
    try {
      const response = await fetch("/api/receipts", { method: "POST", body });
      if (sessionExpired(response)) {
        status.textContent = "保存できませんでした。ログインしてください。";
        return;
      }
      if (response.status === 201) {
        resetCollapse();
        imageBlob = null;
        status.textContent = "保存しました";
        window.location.assign("/dashboard?saved=1");
        return;
      }
      if (response.status === 422) {
        const result = await response.json();
        byId("confirmation-fields").disabled = false;
        showValidationErrors(Array.isArray(result.detail) ? result.detail : []);
        status.textContent = "入力内容を確認して、もう一度保存してください。";
      } else {
        status.textContent = "保存できませんでした。入力内容は保持しています。もう一度保存してください。";
      }
    } catch {
      status.textContent = "通信に失敗しました。入力内容は保持しています。接続を確認して、もう一度保存してください。";
    } finally {
      saving = false;
      byId("confirmation-fields").disabled = false;
      byId("save").disabled = false;
      byId("save").textContent = "保存する";
    }
  });
})();
