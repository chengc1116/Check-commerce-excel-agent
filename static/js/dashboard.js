/**
 * 仪表盘模块
 */
async function loadDashboard() {
  try {
    const data = await API.getDashboard();
    const ps = data.product_stats || {};
    const rs = data.review_stats || {};

    document.getElementById("dash-stats").innerHTML = `
      <div class="stat-card"><div class="label">产品总数</div><div class="value blue">${ps.total || 0}</div></div>
      <div class="stat-card"><div class="label">活跃产品</div><div class="value green">${ps.active || 0}</div></div>
      <div class="stat-card"><div class="label">审核总数</div><div class="value purple">${rs.total || 0}</div></div>
      <div class="stat-card"><div class="label">近7天审核</div><div class="value orange">${rs.recent_7d || 0}</div></div>
      <div class="stat-card"><div class="label">平均评分</div><div class="value ${rs.avg_score >= 75 ? "green" : rs.avg_score >= 50 ? "orange" : "red"}">${rs.avg_score || 0}</div></div>
      <div class="stat-card"><div class="label">销量月份</div><div class="value blue">${ps.sales_months || 0}</div></div>
    `;

    // Category chart
    const cats = ps.categories || [];
    if (cats.length) {
      const maxCnt = Math.max(...cats.map((c) => c.count));
      document.getElementById("category-chart").innerHTML = cats
        .map(
          (c) => `
        <div class="category-bar">
          <span class="cat-name">${c.name}</span>
          <div class="progress-bar" style="flex:1"><div class="fill blue" style="width:${(c.count / maxCnt * 100).toFixed(0)}%"></div></div>
          <span class="cat-count">${c.count}</span>
        </div>`
        )
        .join("");
    }

    // Recent reviews
    try {
      const rev = await API.getReviews({ page: 1 });
      const tbody = document.getElementById("dash-reviews");
      tbody.innerHTML =
        (rev.items || []).slice(0, 5)
          .map(
            (r) => `<tr>
        <td>${r.file_name}</td><td>${r.task_label}</td>
        <td><strong class="${r.overall_score >= 75 ? "green" : r.overall_score >= 50 ? "orange" : "red"}">${r.overall_score || "-"}</strong></td>
        <td><span class="badge ${r.risk_level === "低" ? "badge-green" : r.risk_level === "中" ? "badge-orange" : "badge-red"}">${r.risk_level}</span></td>
        <td>${r.elapsed_seconds ? r.elapsed_seconds.toFixed(1) + "s" : "-"}</td>
        <td><span class="badge ${r.status === "completed" ? "badge-green" : r.status === "processing" ? "badge-blue" : "badge-red"}">${r.status === "completed" ? "完成" : r.status === "processing" ? "进行中" : "失败"}</span></td>
        <td style="font-size:12px;color:var(--text3)">${r.created_at || ""}</td>
      </tr>`
          )
          .join("") ||
        '<tr><td colspan="7" style="text-align:center;color:var(--text3);padding:24px">暂无审核记录</td></tr>';
    } catch (e) {}
  } catch (e) {
    console.error(e);
  }
}
