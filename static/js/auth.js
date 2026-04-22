/**
 * 认证模块 — 登录、登出、权限判断
 */

function showLoginPage() {
  document.getElementById("login-page").style.display = "";
  document.getElementById("app-main").style.display = "none";
}

function showAppPage() {
  document.getElementById("login-page").style.display = "none";
  document.getElementById("app-main").style.display = "";
}

function updateUserInfo() {
  const user = JSON.parse(localStorage.getItem("auth_user") || "null");
  if (!user) return;
  document.getElementById("user-display-name").textContent = user.display_name || user.username;
  const roleMap = { super_admin: "超级管理员", user: "普通用户" };
  document.getElementById("user-role-label").textContent = roleMap[user.role] || user.role;
  document.getElementById("user-avatar").textContent = user.role === "super_admin" ? "👑" : "👤";

  // 管理员专属元素
  const adminItems = document.querySelectorAll(".admin-only");
  adminItems.forEach((el) => {
    el.style.display = user.role === "super_admin" ? "" : "none";
  });
}

async function doLogin() {
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;
  const errorEl = document.getElementById("login-error");

  if (!username || !password) {
    errorEl.textContent = "请输入用户名和密码";
    errorEl.style.display = "";
    return;
  }

  const btn = document.getElementById("login-btn");
  btn.disabled = true;
  btn.textContent = "登录中...";
  errorEl.style.display = "none";

  try {
    const result = await API.login(username, password);
    localStorage.setItem("auth_token", result.token);
    localStorage.setItem("auth_user", JSON.stringify(result.user));
    showAppPage();
    updateUserInfo();
    initApp();
  } catch (e) {
    errorEl.textContent = e.message || "登录失败";
    errorEl.style.display = "";
  } finally {
    btn.disabled = false;
    btn.textContent = "登 录";
  }
}

async function doLogout() {
  try {
    await API.logout();
  } catch (e) {}
  localStorage.removeItem("auth_token");
  localStorage.removeItem("auth_user");
  showLoginPage();
}

// 登录页回车键
document.getElementById("login-password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});
document.getElementById("login-username").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("login-password").focus();
});

// 检查初始登录状态
(async function checkAuth() {
  const token = localStorage.getItem("auth_token");
  if (!token) { showLoginPage(); return; }
  try {
    const user = await API.getMe();
    localStorage.setItem("auth_user", JSON.stringify(user));
    showAppPage();
    updateUserInfo();
    initApp();
  } catch (e) {
    showLoginPage();
  }
})();
