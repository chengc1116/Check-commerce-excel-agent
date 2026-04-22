/**
 * 项目审核模块
 */
let selectedTaskType = null;
let selectedFile = null;
let reviewPollTimer = null;

function initReview() {
  // Task type card clicks
  document.querySelectorAll(".task-card").forEach((card) => {
    card.addEventListener("click", () => {
      document.querySelectorAll(".task-card").forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");
      selectedTaskType = card.dataset.type;
      document.getElementById("review-upload-card").style.display = "";
      document.getElementById("review-progress-card").style.display = "none";
      document.getElementById("review-result-card").style.display = "none";
    });
  });

  // Upload area
  const reviewUpload = document.getElementById("review-upload");
  const reviewFileInput = document.getElementById("review-file");

  reviewUpload.addEventListener("click", () => reviewFileInput.click());
  reviewUpload.addEventListener("dragover", (e) => {
    e.preventDefault();
    reviewUpload.classList.add("dragover");
  });
  reviewUpload.addEventListener("dragleave", () => reviewUpload.classList.remove("dragover"));
  reviewUpload.addEventListener("drop", (e) => {
    e.preventDefault();
    reviewUpload.classList.remove("dragover");
    if (e.dataTransfer.files.length) handleReviewFile(e.dataTransfer.files[0]);
  });
  reviewFileInput.addEventListener("change", (e) => {
    if (e.target.files.length) handleReviewFile(e.target.files[0]);
  });

  // Start button
  document.getElementById("review-start-btn").addEventListener("click", startReview);
}

function handleReviewFile(file) {
  selectedFile = file;
  document.getElementById("review-filename").textContent = file.name;
  document.getElementById("review-selected").style.display = "";
  document.getElementById("review-upload").style.display = "none";
}

function cancelReview() {
  selectedFile = null;
  document.getElementById("review-selected").style.display = "none";
  document.getElementById("review-upload").style.display = "";
  document.getElementById("review-file").value = "";
}

async function startReview() {
  if (!selectedTaskType || !selectedFile) return;
  const formData = new FormData();
  formData.append("file", selectedFile);
  formData.append("task_type", selectedTaskType);

  document.getElementById("review-upload-card").style.display = "none";
  document.getElementById("review-progress-card").style.display = "";

  try {
    const result = await API.startReview(formData);
    pollReviewStatus(result.review_id);
  } catch (e) {
    alert("审核启动失败: " + e.message);
    document.getElementById("review-progress-card").style.display = "none";
    document.getElementById("review-upload-card").style.display = "";
  }
}

async function pollReviewStatus(reviewId) {
  if (reviewPollTimer) clearInterval(reviewPollTimer);
  reviewPollTimer = setInterval(async () => {
    try {
      const status = await API.getReviewStatus(reviewId);
      if (status.status === "completed" || status.status === "error") {
        clearInterval(reviewPollTimer);
        reviewPollTimer = null;
        document.getElementById("review-progress-card").style.display = "none";
        if (status.status === "error") {
          document.getElementById("review-result-card").style.display = "";
          document.getElementById("review-result-content").innerHTML = `
            <div style="text-align:center;padding:24px">
              <div style="font-size:48px;margin-bottom:12px">❌</div>
              <p style="color:var(--red);font-size:16px">审核失败</p>
              <p style="color:var(--text3);margin-top:8px">${status.error || "未知错误"}</p>
              <button class="btn btn-secondary" style="margin-top:16px" onclick="resetReview()">重新审核</button>
            </div>`;
        } else {
          showReviewResult(reviewId);
        }
      }
    } catch (e) {
      console.error(e);
    }
  }, 2000);
}

async function showReviewResult(reviewId) {
  try {
    const data = await API.getReviewResult(reviewId);
    const s = data.overall_score || 0;
    const riskClass = s >= 75 ? "low" : s >= 50 ? "mid" : "high";
    const starCount = s >= 90 ? 5 : s >= 75 ? 4 : s >= 60 ? 3 : s >= 40 ? 2 : 1;
    const stars = "★".repeat(starCount) + "☆".repeat(5 - starCount);

    let specificHtml = "";
    try {
      const ss = typeof data.specific_score === "string" ? JSON.parse(data.specific_score) : data.specific_score || {};
      if (ss.dimensions && ss.dimensions.length) {
        specificHtml = `<div style="margin-top:16px"><div class="card-title">专项分析评分</div>
          ${ss.dimensions.map((d) => `<div class="review-dimension">
            <span class="dim-name">${d.name}</span>
            <div class="progress-bar" style="flex:1"><div class="fill ${d.score / d.max_score >= 0.7 ? "green" : d.score / d.max_score >= 0.4 ? "orange" : "red"}" style="width:${d.score / d.max_score * 100}%"></div></div>
            <span class="dim-score">${d.score}/${d.max_score}</span>
          </div>`).join("")}
        </div>`;
      }
    } catch (e) {}

    document.getElementById("review-result-card").style.display = "";
    document.getElementById("review-result-content").innerHTML = `
      <div class="review-score-row">
        <div class="score-circle ${riskClass}">${s}</div>
        <div>
          <div style="font-size:20px;font-weight:600;margin-bottom:4px">${data.file_name}</div>
          <div style="color:var(--text2)">${data.task_label}</div>
          <div class="score-stars" style="margin-top:4px;font-size:18px">${stars}</div>
        </div>
        <div style="margin-left:auto;text-align:right">
          <div><span class="badge ${data.risk_level === "低" ? "badge-green" : data.risk_level === "中" ? "badge-orange" : "badge-red"}">风险: ${data.risk_level}</span></div>
          <div style="color:var(--text3);font-size:13px;margin-top:4px">耗时 ${data.elapsed_seconds?.toFixed(1) || 0}s</div>
        </div>
      </div>
      <div class="review-actions">
        <button class="btn btn-primary btn-sm" onclick="showReport('${reviewId}')">📄 查看完整报告</button>
        <button class="btn btn-primary btn-sm" onclick="exportReviewResult('${reviewId}')">📥 导出Word</button>
        <button class="btn btn-secondary btn-sm" onclick="resetReview()">🔍 重新审核</button>
      </div>
      ${specificHtml}`;
  } catch (e) {
    console.error(e);
  }
}

async function showReport(reviewId) {
  const data = await API.getReviewResult(reviewId);
  document.getElementById("modal-content").innerHTML = `
    <h3 style="margin-bottom:16px">📋 审核报告 — ${data.file_name}</h3>
    <div class="report-content">${data.report || "暂无报告内容"}</div>`;
  document.getElementById("report-modal").classList.add("show");
}

function closeModal() {
  document.getElementById("report-modal").classList.remove("show");
}

function resetReview() {
  selectedFile = null;
  document.getElementById("review-selected").style.display = "none";
  document.getElementById("review-upload").style.display = "";
  document.getElementById("review-upload-card").style.display = "none";
  document.getElementById("review-progress-card").style.display = "none";
  document.getElementById("review-result-card").style.display = "none";
  document.querySelectorAll(".task-card").forEach((c) => c.classList.remove("selected"));
  selectedTaskType = null;
  document.getElementById("review-file").value = "";
}

async function exportReviewResult(reviewId) {
  try {
    await API.exportReviewDocx(reviewId);
  } catch (e) {
    alert("导出失败: " + e.message);
  }
}
