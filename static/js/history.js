/**
 * 审核记录模块 — 分页版 + 管理员删除
 */
let historyCurrentPage = 1;

function isAdmin() {
  const user = JSON.parse(localStorage.getItem("auth_user") || "null");
  return user && user.role === "super_admin";
}

async function loadHistory(page = 1) {
  historyCurrentPage = page;
  const status = document.getElementById("history-status-filter").value;
  const taskType = document.getElementById("history-type-filter").value;

  const params = { page };
  if (status) params.status = status;
  if (taskType) params.task_type = taskType;

  try {
    const data = await API.getReviews(params);
    const admin = isAdmin();
    const tbody = document.getElementById("history-table");
    tbody.innerHTML = (data.items || [])
      .map((r) => `<tr>
      <td>${r.file_name}</td><td>${r.task_label}</td>
      <td><strong class="${r.overall_score >= 75 ? "green" : r.overall_score >= 50 ? "orange" : "red"}">${r.overall_score || "-"}</strong></td>
      <td><span class="badge ${r.risk_level === "低" ? "badge-green" : r.risk_level === "中" ? "badge-orange" : "badge-red"}">${r.risk_level}</span></td>
      <td>${r.elapsed_seconds ? r.elapsed_seconds.toFixed(1) + "s" : "-"}</td>
      <td><span class="badge ${r.status === "completed" ? "badge-green" : r.status === "processing" ? "badge-blue" : "badge-red"}">${r.status === "completed" ? "完成" : r.status === "processing" ? "进行中" : "失败"}</span></td>
      <td>
        ${r.status === "completed" ? `<button class="btn btn-secondary btn-sm" onclick="showReport('${r.id}')" title="查看报告">📄</button>` : ""}
        ${r.status === "completed" ? `<button class="btn btn-secondary btn-sm" onclick="exportReview('${r.id}')" title="导出Word" style="margin-left:4px">📥</button>` : ""}
        ${admin ? `<button class="btn btn-danger btn-sm" onclick="deleteReview('${r.id}','${r.file_name}')" title="删除记录" style="margin-left:4px">🗑️</button>` : ""}
      </td>
      <td style="font-size:12px;color:var(--text3)">${r.created_at || ""}</td>
    </tr>`)
      .join("") ||
      '<tr><td colspan="8" style="text-align:center;color:var(--text3);padding:24px">暂无审核记录</td></tr>';

    renderPagination("history-pagination", data, loadHistory);
  } catch (e) {
    console.error(e);
  }
}

async function deleteReview(reviewId, fileName) {
  if (!confirm(`确定要删除审核记录「${fileName}」吗？此操作不可恢复。`)) return;
  try {
    await API.deleteReview(reviewId);
    loadHistory(historyCurrentPage);
  } catch (e) {
    alert("删除失败: " + e.message);
  }
}

async function exportReview(reviewId) {
  try {
    await API.exportReviewDocx(reviewId);
  } catch (e) {
    alert("导出失败: " + e.message);
  }
}
