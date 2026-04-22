/**
 * 货盘导入模块
 */
let importFile = null;

function initImport() {
  const importUpload = document.getElementById("import-upload");
  const importFileInput = document.getElementById("import-file");

  importUpload.addEventListener("click", () => importFileInput.click());
  importUpload.addEventListener("dragover", (e) => {
    e.preventDefault();
    importUpload.classList.add("dragover");
  });
  importUpload.addEventListener("dragleave", () => importUpload.classList.remove("dragover"));
  importUpload.addEventListener("drop", (e) => {
    e.preventDefault();
    importUpload.classList.remove("dragover");
    if (e.dataTransfer.files.length) handleImportFile(e.dataTransfer.files[0]);
  });
  importFileInput.addEventListener("change", (e) => {
    if (e.target.files.length) handleImportFile(e.target.files[0]);
  });

  document.getElementById("import-start-btn").addEventListener("click", startImport);
}

function handleImportFile(file) {
  importFile = file;
  document.getElementById("import-filename").textContent = file.name;
  document.getElementById("import-selected").style.display = "";
  document.getElementById("import-upload").style.display = "none";
}

function cancelImport() {
  importFile = null;
  document.getElementById("import-selected").style.display = "none";
  document.getElementById("import-upload").style.display = "";
  document.getElementById("import-file").value = "";
}

async function startImport() {
  if (!importFile) return;
  const formData = new FormData();
  formData.append("file", importFile);

  const sheet = document.getElementById("import-sheet").value;
  const month = document.getElementById("import-month").value;
  if (sheet) formData.append("sheet_name", sheet);
  if (month) formData.append("month", month);

  document.getElementById("import-start-btn").disabled = true;
  document.getElementById("import-result").style.display = "";
  document.getElementById("import-result").innerHTML =
    '<div style="text-align:center;padding:24px"><div class="spinner"></div> 正在导入...</div>';

  try {
    const result = await API.importInventory(formData);
    document.getElementById("import-result").innerHTML = `
      <div style="text-align:center;padding:24px">
        <div style="font-size:36px;margin-bottom:8px">✅</div>
        <p style="color:var(--green);font-size:16px">${result.message}</p>
        <p style="color:var(--text3);margin-top:8px">产品库现有 ${result.stats?.total || "?"} 个产品</p>
      </div>`;
    cancelImport();
  } catch (e) {
    const msg = e.message || "未知错误";
    const isPerm = msg.includes("权限不足");
    document.getElementById("import-result").innerHTML = `
      <div style="text-align:center;padding:24px">
        <div style="font-size:36px;margin-bottom:8px">${isPerm ? "🔒" : "❌"}</div>
        <p style="color:${isPerm ? "var(--orange)" : "var(--red)"}">${isPerm ? "权限不足" : "导入失败"}: ${msg}</p>
        ${isPerm ? '<p style="color:var(--text3);margin-top:8px">仅超级管理员可导入货盘数据</p>' : ""}
      </div>`;
  }
  document.getElementById("import-start-btn").disabled = false;
}
