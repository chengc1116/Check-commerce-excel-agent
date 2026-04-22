/**
 * 应用入口 — 导航、初始化、分页组件、LLM状态
 */

// ============================================================
// 分页组件
// ============================================================
function renderPagination(containerId, data, loadFn) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const { page, total_pages, total } = data;
  if (total_pages <= 1) {
    container.innerHTML = `<div class="pagination-info">共 ${total} 条记录</div>`;
    return;
  }

  let pages = [];
  // 始终显示第一页和最后一页，中间显示当前页附近
  const range = 2;
  for (let i = 1; i <= total_pages; i++) {
    if (i === 1 || i === total_pages || (i >= page - range && i <= page + range)) {
      pages.push(i);
    } else if (pages[pages.length - 1] !== "...") {
      pages.push("...");
    }
  }

  const html = `
    <div class="pagination-info">共 ${total} 条，第 ${page}/${total_pages} 页</div>
    <div class="pagination-btns">
      <button class="btn btn-secondary btn-sm" ${page <= 1 ? "disabled" : ""} onclick="${loadFn.name}(${page - 1})">上一页</button>
      ${pages.map((p) =>
        p === "..."
          ? '<span class="pagination-ellipsis">...</span>'
          : `<button class="btn btn-sm ${p === page ? "btn-primary" : "btn-secondary"}" onclick="${loadFn.name}(${p})">${p}</button>`
      ).join("")}
      <button class="btn btn-secondary btn-sm" ${page >= total_pages ? "disabled" : ""} onclick="${loadFn.name}(${page + 1})">下一页</button>
    </div>
  `;
  container.innerHTML = html;
}

// ============================================================
// Navigation
// ============================================================
document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
    item.classList.add("active");
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    const page = item.dataset.page;
    document.getElementById("page-" + page).classList.add("active");

    // Load data for the page
    if (page === "dashboard") loadDashboard();
    if (page === "products") loadProductStats();
    if (page === "cbb") loadCBBStats();
    if (page === "history") loadHistory(1);
  });
});

// ============================================================
// LLM Status
// ============================================================
async function checkLLM() {
  try {
    const data = await API.getLLMStatus();
    const el = document.getElementById("llm-status");
    if (data.available) {
      const shortName = (full) => {
        const parts = full.split("/");
        const name = parts.length > 1 ? parts.slice(1).join("/") : full;
        return name.replace(/-Instruct$/, "");
      };
      const textModel = shortName(data.model || "unknown");
      const vlModel = shortName(data.vl_model || "unknown");
      el.innerHTML = `
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
          <span class="status-dot online"></span> LLM 在线
        </div>
        <div style="padding-left:14px;line-height:1.8;color:var(--text3)">
          <div>📝 文本: <span style="color:var(--text2)">${textModel}</span></div>
          <div>👁️ 视觉: <span style="color:var(--text2)">${vlModel}</span></div>
        </div>
      `;
    } else {
      el.innerHTML = `
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
          <span class="status-dot offline"></span> LLM 离线
        </div>
        <div style="padding-left:14px;color:var(--text3)">${data.error || "未配置"}</div>
      `;
    }
  } catch (e) {
    document.getElementById("llm-status").innerHTML = `
      <div style="display:flex;align-items:center;gap:6px">
        <span class="status-dot offline"></span> 连接失败
      </div>
    `;
  }
}

// ============================================================
// Modal
// ============================================================
document.getElementById("report-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

// ============================================================
// App Init（登录后调用）
// ============================================================
function initApp() {
  initReview();
  initImport();
  loadDashboard();
  checkLLM();
  setInterval(checkLLM, 30000);
}
