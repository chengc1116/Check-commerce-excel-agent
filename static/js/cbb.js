/**
 * CBB模块库模块 — 分页版
 */
let cbbCurrentPage = 1;

async function loadCBBStats() {
  try {
    const stats = await API.getCBBStats();
    document.getElementById("cbb-stats-grid").innerHTML = `
      <div class="stat-card"><div class="label">CBB模块总数</div><div class="value purple">${stats.total}</div></div>
      ${(stats.categories || [])
        .slice(0, 4)
        .map((c) => `<div class="stat-card"><div class="label">${c.category}</div><div class="value blue">${c.cnt}</div></div>`)
        .join("")}`;

    // Populate category filter
    const sel = document.getElementById("cbb-category-filter");
    sel.innerHTML =
      '<option value="">全部类别</option>' +
      (stats.categories || [])
        .map((c) => `<option value="${c.category}">${c.category} (${c.cnt})</option>`)
        .join("");
  } catch (e) {}
  loadCBB(1);
}

async function loadCBB(page = 1) {
  cbbCurrentPage = page;
  const cat = document.getElementById("cbb-category-filter").value;
  const search = document.getElementById("cbb-search").value;

  const params = { page };
  if (cat) params.category = cat;
  if (search) params.search = search;

  try {
    const data = await API.getCBBModules(params);
    const tbody = document.getElementById("cbb-table");
    tbody.innerHTML = (data.items || []).map(
      (m) => `<tr>
      <td><strong>${m.cbb_code || "-"}</strong></td><td>${m.cbb_name || "-"}</td>
      <td><span class="badge badge-purple">${m.category || "-"}</span></td>
      <td>${m.sub_type || "-"}</td><td>${m.specification || "-"}</td>
      <td>${m.supplier || "-"}</td><td>${m.price ? m.price + "元" : "-"}</td>
      <td>${m.usage_count || 0}</td>
    </tr>`
    ).join("") || '<tr><td colspan="8" style="text-align:center;color:var(--text3);padding:24px">暂无模块数据</td></tr>';

    renderPagination("cbb-pagination", data, loadCBB);
  } catch (e) {
    console.error(e);
  }
}
