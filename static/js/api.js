/**
 * API 封装层
 * 统一管理所有后端接口调用
 */
const API = {
  _getToken() {
    return localStorage.getItem("auth_token") || "";
  },

  _headers() {
    const h = {};
    const token = this._getToken();
    if (token) h["Authorization"] = `Bearer ${token}`;
    return h;
  },

  async get(url) {
    const r = await fetch(url, { headers: this._headers() });
    if (r.status === 401) { handleAuthExpired(); throw new Error("401"); }
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },

  async post(url, data) {
    const r = await fetch(url, { method: "POST", body: data, headers: this._headers() });
    if (r.status === 401) { handleAuthExpired(); throw new Error("401"); }
    if (r.status === 403) throw new Error("权限不足，仅超级管理员可执行此操作");
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || r.status);
    }
    return r.json();
  },

  async del(url) {
    const r = await fetch(url, { method: "DELETE", headers: this._headers() });
    if (r.status === 401) { handleAuthExpired(); throw new Error("401"); }
    if (r.status === 403) throw new Error("权限不足，仅超级管理员可执行此操作");
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || r.status);
    }
    return r.json();
  },

  /** 下载文件（带认证，使用 fetch + blob 方式） */
  async download(url) {
    const r = await fetch(url, { headers: this._headers() });
    if (r.status === 401) { handleAuthExpired(); throw new Error("401"); }
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || "下载失败");
    }
    const blob = await r.blob();
    const contentDisposition = r.headers.get("content-disposition") || "";
    let filename = "审核报告.docx";
    const match = contentDisposition.match(/filename\*?=(?:UTF-8''|"?)([^";]+)/i);
    if (match) {
      filename = decodeURIComponent(match[1].replace(/"/g, ""));
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  },

  // ---- Auth ----
  login(username, password) {
    return fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }).then(async (r) => {
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        throw new Error(e.detail || "登录失败");
      }
      return r.json();
    });
  },
  getMe() { return this.get("/api/auth/me"); },
  logout() { return this.post("/api/auth/logout", null); },

  // ---- Dashboard ----
  getDashboard: () => API.get("/api/dashboard"),

  // ---- Review ----
  startReview(formData) {
    return API.post("/api/review/start", formData);
  },
  getReviewStatus(id) {
    return API.get(`/api/review/status/${id}`);
  },
  getReviewResult(id) {
    return API.get(`/api/review/result/${id}`);
  },
  exportReviewDocx(id) {
    return API.download(`/api/review/export/${id}`);
  },
  deleteReview(id) {
    return API.del(`/api/reviews/${id}`);
  },

  // ---- Reviews (list) ----
  getReviews(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return API.get(`/api/reviews?${qs}`);
  },

  // ---- Products ----
  getProductStats: () => API.get("/api/products/stats"),
  getProducts(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return API.get(`/api/products?${qs}`);
  },
  importInventory(formData) {
    return API.post("/api/products/import-inventory", formData);
  },

  // ---- CBB ----
  getCBBStats: () => API.get("/api/cbb/stats"),
  getCBBModules(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return API.get(`/api/cbb?${qs}`);
  },

  // ---- LLM ----
  getLLMStatus: () => API.get("/api/llm/status"),
};

function handleAuthExpired() {
  localStorage.removeItem("auth_token");
  localStorage.removeItem("auth_user");
  showLoginPage();
}
