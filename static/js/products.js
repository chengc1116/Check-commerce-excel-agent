/**
 * 产品库模块 — 分页版
 */
let productCurrentPage = 1;

async function loadProductStats() {
  try {
    const stats = await API.getProductStats();
    document.getElementById("product-stats").innerHTML = `
      <div class="stat-card"><div class="label">产品总数</div><div class="value blue">${stats.total}</div></div>
      <div class="stat-card"><div class="label">活跃产品</div><div class="value green">${stats.active}</div></div>
      <div class="stat-card"><div class="label">归档产品</div><div class="value orange">${stats.archived}</div></div>`;
  } catch (e) {}
  loadProducts(1);
}

async function loadProducts(page = 1) {
  productCurrentPage = page;
  const brand = document.getElementById("product-brand-filter").value;
  const status = document.getElementById("product-status-filter").value;

  const params = { page };
  if (brand) params.brand = brand;
  if (status) params.status = status;

  try {
    const data = await API.getProducts(params);
    const tbody = document.getElementById("product-table");
    tbody.innerHTML = (data.items || []).map(
      (p) => `<tr>
      <td><strong>${p.sku || "-"}</strong></td>
      <td>${p.brand || "-"}</td><td>${p.category_l1 || "-"}</td><td>${p.category_l2 || "-"}</td><td>${p.category_l3 || "-"}</td>
      <td>${p.version || "-"}</td>
      <td><span class="badge ${p.status === "active" ? "badge-green" : "badge-orange"}">${p.status === "active" ? "活跃" : "归档"}</span></td>
    </tr>`
    ).join("") || '<tr><td colspan="7" style="text-align:center;color:var(--text3);padding:24px">暂无产品数据</td></tr>';

    renderPagination("product-pagination", data, loadProducts);
  } catch (e) {
    console.error(e);
  }
}
