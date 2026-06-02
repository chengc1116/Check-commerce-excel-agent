# -*- coding: utf-8 -*-
"""
VL 商品模块化拆解报告生成器（独立组件）

核心能力：
  1. 单商品拆解：输入商品图片 → 输出完整9板块模块化拆解报告
  2. 多商品对比：输入自家+竞品图片 → 输出对比式拆解报告
  3. 自动识别图片中的商品数量，选择对应报告模式

两种报告格式：
  模式A（单商品）：9大板块完整报告
  模式B（多商品对比）：对比式拆解 + 复用度评估 + 差异汇总

两步式调用策略（对比模式）：
  Step1a+1b: VL模型分别看自家/竞品图（asyncio.gather并行）→ 各自模块拆解
  Step2: GLM-5文本模型 → 基于两方拆解结果做对比分析+推理（section3-9）

两步式调用策略（单商品模式）：
  Step1: VL模型看图 → 视觉分析+模块拆解
  Step2: GLM-5文本模型 → 补全推理板块（CBB/装配/成本/打样等）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# 加载 .env 环境变量
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "project_review.db"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_IMAGES_PER_CALL = 3  # 单次VL调用的最大图片数


# ============================================================
# 图片加载工具
# ============================================================

def load_images_from_dir(image_dir: str) -> list[tuple[str, bytes, str]]:
    """加载目录下所有图片，返回 (filename, data, ext) 列表"""
    images = []
    for fname in sorted(os.listdir(image_dir)):
        ext = Path(fname).suffix.lower()
        if ext in IMAGE_EXTS:
            fpath = os.path.join(image_dir, fname)
            with open(fpath, "rb") as f:
                data = f.read()
            images.append((fname, data, ext.lstrip(".")))
    return images


def find_product_images(product_code: str, brand: str = "",
                        image_roots: list[str] = None) -> list[tuple[str, bytes, str]]:
    """
    从图片根目录中按货号检索商品图片。
    目录结构: {image_roots}/{product_code}/  (如 data/images/new_products/HY63/)
    """
    # 默认使用项目相对路径 data/images/new_products
    if image_roots is None:
        project_root = Path(__file__).resolve().parent.parent
        image_roots = [str(project_root / "data" / "images" / "new_products")]

    for root in image_roots:
        product_dir = os.path.join(root, product_code)
        if os.path.isdir(product_dir):
            images = load_images_from_dir(product_dir)
            if images:
                logger.info(f"找到 {product_code} 图片: {product_dir} ({len(images)}张)")
                return images

    logger.warning(f"未找到 {product_code} 的商品图片 (搜索路径: {image_roots})")
    return []


# ============================================================
# DB 查询工具
# ============================================================

def query_product_info(db_path: Path, product_code: str, brand: str = "") -> Optional[dict]:
    """从 products 表查询货号信息"""
    clean_code = product_code.strip()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if brand:
        cur.execute(
            "SELECT product_code, category1, category2, category3, brand "
            "FROM products WHERE product_code = ? AND brand = ?",
            (clean_code, brand),
        )
    else:
        cur.execute(
            "SELECT product_code, category1, category2, category3, brand "
            "FROM products WHERE product_code = ?",
            (clean_code,),
        )
    row = cur.fetchone()

    if not row and not clean_code.startswith("C-"):
        if brand:
            cur.execute(
                "SELECT product_code, category1, category2, category3, brand "
                "FROM products WHERE product_code = ? AND brand = ?",
                ("C-" + clean_code, brand),
            )
        else:
            cur.execute(
                "SELECT product_code, category1, category2, category3, brand "
                "FROM products WHERE product_code = ?",
                ("C-" + clean_code,),
            )
        row = cur.fetchone()

    conn.close()
    return dict(row) if row else None


# ============================================================
# Prompt 构建
# ============================================================

def build_single_product_step1_prompt(
    product_code: str,
    category_info: Optional[dict],
    known_modules: Optional[list[str]] = None,
) -> str:
    """Step1: 单商品VL看图 → 视觉分析 + A/B/C模块拆解"""

    cat1 = category_info.get("category1", "未知") if category_info else "未知"
    cat2 = category_info.get("category2", "未知") if category_info else "未知"
    cat3 = category_info.get("category3", "未知") if category_info else "未知"
    brand = category_info.get("brand", "未知") if category_info else "未知"

    if known_modules:
        module_list = "\n".join(f"  {i+1}. {m}" for i, m in enumerate(known_modules))
        known_section = f"""
### 已知模块定义（品类模块库）
{module_list}

请基于以上模块定义验证拆解：观察到的模块标记observed=true并描述视觉特征。
发现图片中有但模块列表中没有的，归入 extra_modules。"""
    else:
        known_section = """
### 模块命名规范
每个B级模块必须是可以独立采购/替换的物料单元。
命名格式：具体物料名 + 可选的功能/规格描述。
不要用抽象的功能描述（如"压力分散系统""脊椎支撑系统"），要用具体的物料名称。

必选模块（每个产品必须有）：
- 版型(PATTERN): 产品的整体构造方式。命名格式为"{产品类型}版型"，
  如"SBR护膝版型"、"髌骨带版型"、"护腰版型"。
  注意：版型是产品形态，不是面料，一个产品只有一个版型。
- 面料(FABRIC): 产品使用的具体面料材质。
  如"四面弹莱卡布"、"锦纶拉毛布"、"冰感双面莱卡"、"SBR复合布"。
  一个产品可能有2-3种不同面料（外层、内衬、接触层）。

可选模块（产品中存在才拆，不存在不要硬凑）：
- 外观(APPEARANCE): LOGO标识和装饰件。
  如"热转印LOGO标"、"硅胶滴塑标"、"织唛标"、"印花装饰"。
- 支撑件(PAD): 填充、支撑、缓冲的材料或结构件。
  如"3D记忆棉"、"铝条支撑片"、"PE塑料条"、"TPE弹性垫"、"硅胶防滑垫"。
- 魔术贴(VELCRO): 粘扣固定件。如"尼龙勾面魔术贴"、"射出勾面"。
- 织带(WEBBING): 绑带、松紧带、织带。如"尼龙松紧带"、"弹力织带"。
- 配件(PARTS): 扣具、拉链、旋钮等五金/塑料配件。
  如"PP塑料壳"、"塑料插扣"、"BOA旋钮"、"YKK拉链"。

判断标准：如果一个零件不能独立采购或替换，就不要单独拆成模块，
而是归入它所属的更大模块中。例如"缝线"归入所属面料模块，
"螺丝"归入所属配件模块。

一般3-7个B级模块（版型+面料必选，其余按实际情况）。"""

    return f"""你是产品模块化拆解专家。根据商品图片，输出产品视觉分析和模块拆解。

## 产品信息
- 货号: {product_code}
- 品牌: {brand}
- 品类: {cat1} > {cat2} > {cat3}
{known_section}

## 输出JSON格式
```json
{{
  "product_code": "{product_code}",
  "category_type": "产品品类类型名称",
  "section1_visual_analysis": {{
    "product_type": "产品类型判断",
    "structure_form": "结构形态",
    "functional_zones": "功能区描述",
    "material_texture": "材料质感判断",
    "size_estimate": "尺寸比例估算",
    "color_scheme": "颜色方案",
    "key_visual_features": ["特征1", "特征2", "特征3"]
  }},
  "section2_abc_modules": {{
    "a_level": {{
      "name": "产品整体名称",
      "type": "产品类型",
      "typical_size": "典型尺寸范围",
      "weight_estimate": "重量估算",
      "target_users": "目标人群"
    }},
    "b_level": [
      {{
        "id": "B1", "name": "模块名称", "core_function": "核心功能",
        "priority": 5, "typical_material": "典型材料",
        "observed": true, "visual_description": "基于图片的视觉描述"
      }}
    ],
    "c_level": [
      {{
        "id": "C1", "name": "子模块名称", "parent": "B1",
        "function_description": "功能描述", "key_parameter": "关键参数"
      }}
    ]
  }}
}}
```

规则：
1. B级模块的name必须是具体的物料名称（如"铝条支撑片"），不要用抽象功能描述（如"脊椎支撑系统"）
2. 版型和面料是必选模块，其他类别有才拆、没有不要硬凑
3. C级每个B下1-3个，priority 1-5(5=不可或缺)，数字字段必须是数字"""





def build_step2_prompt_single(
    product_code: str,
    category_info: Optional[dict],
    step1_result: dict,
) -> str:
    """Step2: 单商品 → GLM-5补全板块三到九"""

    cat2 = category_info.get("category2", "未知") if category_info else "未知"
    b_level = step1_result.get("section2_abc_modules", {}).get("b_level", [])
    b_summary = "\n".join(
        f"  - {b.get('id','?')} {b.get('name','?')} | 功能: {b.get('core_function','?')} | 材料: {b.get('typical_material','?')}"
        for b in b_level
    ) if isinstance(b_level, list) else "无"
    c_level = step1_result.get("section2_abc_modules", {}).get("c_level", [])
    c_summary = "\n".join(
        f"  - {c.get('id','?')} {c.get('name','?')} → 父{c.get('parent','?')} | {c.get('function_description','?')}"
        for c in c_level
    ) if isinstance(c_level, list) else "无"

    return f"""你是资深的产品模块化拆解专家，精通CBB模块复用、工艺选型和成本估算。

## 任务
根据Step1的视觉分析结果，补全模块化拆解报告的6个板块（section3-section9）。

## 产品信息
- 货号: {product_code}
- 品类: {cat2}
- 产品类型: {step1_result.get('category_type', '未知')}

## Step1 结果（视觉分析+模块拆解已由VL模型完成）
视觉特征: {json.dumps(step1_result.get('section1_visual_analysis', {}), ensure_ascii=False)}

B级模块:
{b_summary}

C级子模块:
{c_summary}

---

## 输出JSON格式（6个板块）
```json
{{
  "product_code": "{product_code}",
  "section3_cbb_check": {{
    "reuse_analysis": [
      {{"module": "B1 模块名称", "reusability": "高/中/低", "match_score": 85,
        "cbb_reference": "可复用的模块或工艺参考", "barrier_suggestion": "✅强制复用 / ✅建议复用 / ⚠️允许创新 / ❌需新开模",
        "reason": "判断理由"}}
    ],
    "overall_reuse_rate": 55,
    "reuse_summary": "总体复用率评估总结"
  }},
  "section4_physical_attrs": {{
    "fabric_material": [{{"module": "B级模块名", "standardization": "统一规格建议"}}],
    "structural_parts": [{{"module": "B级模块名", "standardization": "标准化建议"}}],
    "fasteners_connectors": [{{"module": "B级模块名", "standardization": "通用化建议"}}],
    "electronic_functional": [{{"module": "模块名或无", "standardization": "说明"}}]
  }},
  "section5_assembly_bom": {{
    "assembly_steps": [
      {{"step": 1, "name": "工序名称", "description": "操作描述", "modules": ["B1","B2"], "is_critical": false}}
    ],
    "process_allocation": [
      {{"process": "工艺名称", "applicable_modules": ["B1","B2"], "key_parameters": "关键参数"}}
    ]
  }},
  "section6_prototype_plan": {{
    "days": [
      {{"day": 1, "task": "工作任务", "deliverable": "关键交付物", "gate_standard": "门控标准"}}
    ],
    "total_days": 7,
    "critical_path": "关键路径说明"
  }},
  "section7_cost_estimate": {{
    "cost_items": [
      {{"item": "成本项名称", "amount_low": 3, "amount_high": 5, "percentage": 15, "category": "材料/加工/其他"}}
    ],
    "material_total_low": 8, "material_total_high": 15,
    "processing_total_low": 7, "processing_total_high": 12,
    "unit_cost_low": 18, "unit_cost_high": 32,
    "batch_optimization": "批量化优化建议"
  }},
  "section8_implicit_knowledge": {{
    "items": [
      {{"knowledge": "隐性知识描述", "current_location": "现存位置", "explicitation_plan": "显性化方案"}}
    ]
  }},
  "section9_next_steps": {{
    "suggestions": ["建议1", "建议2", "建议3"],
    "summary": "一段话总结：产品类型+模块数+复用率+成本区间+打样周期"
  }}
}}
```

规则：
1. 复用率基于模块通用性判断，魔术贴/包边等通用件标注"强制复用"
2. 成本基于运动护具/睡眠用品行业经验
3. 7天打样计划要具体到每天任务
4. 所有字段必填，数字字段必须是数字"""


def build_step2_compare_prompt(
    product_code: str,
    category_info: Optional[dict],
    own_step1: dict,
    competitor_step1: Optional[dict] = None,
    competitor_desc: str = "竞品",
    upgrade_direction: str = "",
    project_data: Optional[dict] = None,
) -> str:
    """Step2: 对比模式 → GLM-5基于VL对比结果做推理板块（section4-9）

    支持两种输入格式：
      - 1步VL直接对比: own_step1 包含 section3_module_comparison, competitor_step1=None
      - 2步VL拆解: own_step1 和 competitor_step1 分别是自家和竞品的独立拆解
    """

    cat2 = category_info.get("category2", "未知") if category_info else "未知"

    # 判断输入格式：1步对比 or 2步拆解
    is_one_step = competitor_step1 is None

    if is_one_step:
        # 1步VL直接对比的结果格式
        own_b = own_step1.get("section2_abc_modules", {}).get("b_level", [])
        own_b_str = "\n".join(
            f"  - {b.get('id','?')} {b.get('name','?')} | 功能: {b.get('core_function','?')} | 材料: {b.get('typical_material','?')} | 优先级: {b.get('priority','?')}"
            for b in own_b
        ) if isinstance(own_b, list) else "  无"

        comp_b = own_step1.get("competitor_modules", [])
        comp_b_str = "\n".join(
            f"  - {b.get('id','?')} {b.get('name','?')} | 功能: {b.get('core_function','?')} | 材料: {b.get('typical_material','?')}"
            for b in comp_b
        ) if isinstance(comp_b, list) else "  无"

        own_vis = json.dumps(own_step1.get("section1_visual_analysis", {}), ensure_ascii=False)
        comp_vis = json.dumps(own_step1.get("competitor_visual_analysis", {}), ensure_ascii=False)

        # section3 已由VL完成，直接传入
        section3_json = json.dumps(own_step1.get("section3_module_comparison", {}), ensure_ascii=False, indent=2)
    else:
        # 2步VL拆解的结果格式（兼容旧逻辑）
        own_b = own_step1.get("section2_abc_modules", {}).get("b_level", [])
        own_b_str = "\n".join(
            f"  - {b.get('id','?')} {b.get('name','?')} | 功能: {b.get('core_function','?')} | 材料: {b.get('typical_material','?')} | 优先级: {b.get('priority','?')}"
            for b in own_b
        ) if isinstance(own_b, list) else "  无"

        comp_b = competitor_step1.get("section2_abc_modules", {}).get("b_level", [])
        comp_b_str = "\n".join(
            f"  - {b.get('id','?')} {b.get('name','?')} | 功能: {b.get('core_function','?')} | 材料: {b.get('typical_material','?')} | 优先级: {b.get('priority','?')}"
            for b in comp_b
        ) if isinstance(comp_b, list) else "  无"

        own_vis = json.dumps(own_step1.get("section1_visual_analysis", {}), ensure_ascii=False)
        comp_vis = json.dumps(competitor_step1.get("section1_visual_analysis", {}), ensure_ascii=False)

        section3_json = None

    # 表格信息
    project_info = ""
    if project_data:
        project_info = f"""
## 立项表信息
- 升级方向: {project_data.get('upgrade_direction', '未提供')}
- 具体升级模块: {project_data.get('upgrade_modules', '未提供')}
- 升级功能: {project_data.get('upgrade_function', '未提供')}
- 设计目的: {project_data.get('design_purpose', '未提供')}
- 价格/毛利: {project_data.get('price_margin', '未提供')}
- ERP成本: {project_data.get('erp_cost', '未提供')}
"""

    # 构建 prompt：1步VL模式传入已有section3，2步VL模式需GLM自行生成section3
    if is_one_step and section3_json:
        section3_section = f"""
## VL模型已完成的模块对比（section3，请直接使用，无需重做）
```json
{section3_json}
```
"""
        section3_output = f"""  "section3_module_comparison": {section3_json},"""
        section3_rules = "1. section3已由VL模型完成，直接引用，不要修改"
    else:
        section3_section = ""
        section3_output = """  "section3_module_comparison": {
    "same_modules": [
      {"module_name": "模块名", "own_detail": "自家描述", "competitor_detail": "竞品描述", "reuse": true}
    ],
    "competitor_only": [
      {"module_name": "模块名", "detail": "竞品独有描述", "upgrade_direction_hit": true}
    ],
    "own_only": [
      {"module_name": "模块名", "detail": "自家独有描述", "is_advantage": true}
    ],
    "structural_differences": [
      {"aspect": "版型/面料/工艺差异点", "own": "自家情况", "competitor": "竞品情况"}
    ],
    "overall_reuse_rate": 55,
    "reuse_summary": "复用度评估总结"
  },"""
        section3_rules = "1. section3：逐模块对比，相同模块按功能/材料相似度匹配，不要求名称完全一致\n2. competitor_only中的upgrade_direction_hit：该模块是否被立项表的升级方向命中"

    return f"""你是资深的产品模块化拆解与升级评估专家，精通同品类产品对比分析。

## 任务
根据两款产品的拆解结果，输出完整的升级评估报告。

## 产品信息
- 自家产品: {product_code} ({cat2})
- 对比产品: {competitor_desc}
{project_info}
## 自家产品模块

### 视觉特征
{own_vis}

### B级模块
{own_b_str}

---

## {competitor_desc}模块

### 视觉特征
{comp_vis}

### B级模块
{comp_b_str}

---
{section3_section}
{f'## 升级方向（来自立项表）\n{upgrade_direction}' if upgrade_direction else ''}

## 输出JSON格式
```json
{{
  "product_code": "{product_code}",
{section3_output}
  "section4_upgrade_direction_score": {{
    "direction_hits_gap": true,
    "direction_hit_modules": ["命中的竞品独有模块名"],
    "direction_miss_modules": ["未命中的竞品独有模块名"],
    "direction_quality": "精准/部分命中/偏离",
    "reason": "升级方向评估理由"
  }},
  "section5_module_reuse": {{
    "reuse_analysis": [
      {{"module": "模块名", "reusability": "高/中/低", "reason": "理由"}}
    ],
    "overall_reuse_rate": 55,
    "new_modules_needed": ["需新建的模块名"],
    "core_module_reuse_rate": 80,
    "reuse_summary": "复用度评估总结"
  }},
  "section6_upgrade_value": {{
    "incremental_modules": ["新增模块名"],
    "user_perception": "高/中/低",
    "price_competitiveness": "评估",
    "value_summary": "增量价值评估总结"
  }},
  "section7_execution_feasibility": {{
    "prototype_days": 7,
    "process_difficulty": "低/中/高",
    "new_process_needed": ["需新开工艺"],
    "supply_chain_risk": "低/中/高",
    "is_new_category": false,
    "feasibility_summary": "可行性评估总结"
  }},
  "section8_physical_attrs": {{
    "fabric_material": [{{"module": "模块名", "standardization": "统一规格建议"}}],
    "structural_parts": [{{"module": "模块名", "standardization": "标准化建议"}}],
    "fasteners_connectors": [{{"module": "模块名", "standardization": "通用化建议"}}]
  }},
  "section9_next_steps": {{
    "suggestions": ["建议1", "建议2", "建议3"],
    "summary": "一段话总结：模块对比结论+复用率+升级方向评估+执行建议"
  }}
}}
```

规则：
{section3_rules}
3. section4：评估升级方向是否命中了关键差距模块
4. section5：复用度基于"相同模块/B级总模块数"计算，核心模块(priority=5)加权
5. section6：增量价值评估结合价格/毛利判断性价比
6. section7：执行可行性考虑工艺难度和供应链风险
7. 所有字段必填，数字字段必须是数字"""





# ============================================================
# ModuleSplitter 主类
# ============================================================

class ModuleSplitter:
    """
    VL商品模块化拆解器

    支持两种模式：
      - 单商品拆解：analyze_single()
      - 多商品对比：analyze_compare()
      - 自动选择：analyze()（根据图片标签自动判断）
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            from product_review_agent.agents.llm_client import LLMClient
            self._llm = LLMClient()
        return self._llm

    def _parse_vl_response(self, result) -> Optional[dict]:
        """解析VL返回的文本为JSON"""
        if isinstance(result, dict):
            if not result.get("_parse_error"):
                return result
            raw = result.get("_raw_text", "")
        elif isinstance(result, str):
            raw = result
        else:
            logger.warning("[VL解析] 返回值类型异常: %s", type(result).__name__)
            return None

        if not raw:
            logger.warning("[VL解析] 返回内容为空")
            return None

        # 去think标签
        raw = re.sub(r"<think.*?>.*?</think\s*>", "", raw, flags=re.DOTALL).strip()

        # 提取JSON
        first_brace = raw.find("{")
        last_brace = raw.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            json_str = raw[first_brace:last_brace + 1]
            try:
                parsed = json.loads(json_str)
                n_b = len(parsed.get("section2_abc_modules", {}).get("b_level", []))
                logger.info(f"[VL解析] JSON解析成功, b_level模块数: {n_b}")
                return parsed
            except json.JSONDecodeError as e:
                logger.warning(f"[VL解析] JSON解析失败: {e}, 原始文本前200字: {raw[:200]}")
                fixed = self.llm._try_fix_json(json_str)
                if fixed:
                    logger.info("[VL解析] JSON修复成功")
                    return fixed
                logger.warning("[VL解析] JSON修复也失败")

        logger.warning(f"[VL解析] 未找到有效JSON, 原始文本前200字: {raw[:200]}")
        return None

    async def _call_vl_step(self, images: list[bytes], prompt: str,
                            timeout: int = 200, max_tokens: int = 8000,
                            max_retries: int = 2) -> Optional[dict]:
        """Step1: VL看图分析（空响应自动重试）"""
        from product_review_agent.agents.llm_client import LLMClient

        user_msg = LLMClient.build_image_message(images, text=prompt)
        system_msg = {
            "role": "system",
            "content": "你是产品模块化拆解专家。请严格按照JSON格式输出，所有字段必填，数字字段必须是数字类型。"
        }
        messages = [system_msg, user_msg]

        img_sizes = [len(img) for img in images]
        logger.info(f"[VL调用] 图片数: {len(images)}, 各张大小: {img_sizes} bytes")

        for attempt in range(max_retries):
            try:
                result = await asyncio.wait_for(
                    self.llm.acall_vision(messages, response_format="text", max_tokens=max_tokens),
                    timeout=timeout
                )

                # 检查是否返回空内容
                if result is None or (isinstance(result, str) and not result.strip()):
                    if attempt < max_retries - 1:
                        logger.warning(f"[VL调用] 第{attempt+1}次返回空内容, 1秒后重试...")
                        await asyncio.sleep(1)
                        continue
                    else:
                        logger.error(f"[VL调用] {max_retries}次均返回空内容")
                        return None

                parsed = self._parse_vl_response(result)
                if parsed is None:
                    if attempt < max_retries - 1:
                        logger.warning(f"[VL调用] 第{attempt+1}次JSON解析失败, 1秒后重试...")
                        await asyncio.sleep(1)
                        continue
                    raw_preview = str(result)[:300] if result else "(None)"
                    logger.error(f"[VL调用] {max_retries}次均JSON解析失败, 原始响应前300字: {raw_preview}")
                return parsed

            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    logger.warning(f"[VL调用] 第{attempt+1}次超时({timeout}s), 重试...")
                    continue
                logger.error(f"VL调用超时（{timeout}s）, 已重试{max_retries}次")
                return None
            except Exception as e:
                logger.error(f"VL调用异常: {e}")
                return None

        return None

    async def _call_text_step(self, prompt: str, timeout: int = 120,
                              max_tokens: int = 16000) -> Optional[dict]:
        """Step2: GLM-5文本补全"""
        messages = [
            {"role": "system", "content": "你是资深的产品模块化拆解专家。请严格按照JSON格式输出，所有字段必填，数字字段必须是数字类型。"},
            {"role": "user", "content": prompt},
        ]

        try:
            result = await asyncio.wait_for(
                self.llm.acall_text(messages, response_format="json", max_tokens=max_tokens),
                timeout=timeout
            )
            if isinstance(result, dict) and not result.get("_parse_error"):
                return result
            # 尝试文本解析
            return self._parse_vl_response(result)
        except asyncio.TimeoutError:
            logger.error(f"GLM-5调用超时（{timeout}s）")
            return None
        except Exception as e:
            logger.error(f"GLM-5调用异常: {e}")
            return None

    async def analyze_single(
        self,
        images: list[bytes],
        product_code: str = "",
        category_info: Optional[dict] = None,
        known_modules: Optional[list[str]] = None,
    ) -> dict:
        """
        单商品模块化拆解（9板块完整报告）

        Args:
            images: 商品图片bytes列表
            product_code: 货号
            category_info: {"category1", "category2", "category3", "brand"}
            known_modules: 已知模块名列表（来自category_modules.json匹配）
        """
        selected = images[:MAX_IMAGES_PER_CALL]
        logger.info(f"🔍 单商品拆解: {product_code} ({len(selected)}张图)")

        # Step1: VL看图
        t0 = time.time()
        prompt1 = build_single_product_step1_prompt(product_code, category_info, known_modules)
        step1 = await self._call_vl_step(selected, prompt1)

        if not step1:
            return {"product_code": product_code, "error": "VL Step1 分析失败"}

        step1_time = time.time() - t0
        b_level = step1.get("section2_abc_modules", {}).get("b_level", [])
        n_b = len(b_level) if isinstance(b_level, list) else "?"
        logger.info(f"✅ Step1完成: {step1.get('category_type','?')}, {n_b}个B级模块 ({step1_time:.1f}s)")

        # Step2: GLM-5补全
        t1 = time.time()
        prompt2 = build_step2_prompt_single(product_code, category_info, step1)
        step2 = await self._call_text_step(prompt2)

        if not step2:
            # 至少返回Step1结果
            step1["_step1_time"] = f"{step1_time:.1f}s"
            step1["_step2_status"] = "failed"
            return step1

        step2_time = time.time() - t1

        # 合并
        final = {**step1, **step2}
        final["product_code"] = product_code
        final["_step1_time"] = f"{step1_time:.1f}s"
        final["_step2_time"] = f"{step2_time:.1f}s"
        final["_mode"] = "single"

        logger.info(f"✅ 单商品报告完成: {product_code} (VL:{step1_time:.1f}s + GLM:{step2_time:.1f}s)")
        return final

    async def analyze_compare(
        self,
        own_images: list[bytes],
        competitor_images: list[bytes],
        product_code: str = "",
        category_info: Optional[dict] = None,
        competitor_desc: str = "竞品",
        upgrade_direction: str = "",
        project_data: Optional[dict] = None,
    ) -> dict:
        """
        多商品对比拆解（自家+竞品）

        两步异步策略：
          Step1a+1b: VL模型分别看自家和竞品图片（asyncio.gather并行）→ 各自输出模块拆解
          Step2: GLM-5基于两方拆解结果做对比分析+推理（section3-9）
        """
        max_per_side = 2
        own_selected = own_images[:max_per_side]
        comp_selected = competitor_images[:max_per_side]
        logger.info(f"🔍 对比拆解: {product_code} vs {competitor_desc} "
                    f"(自家{len(own_selected)}张 + 竞品{len(comp_selected)}张, 2步异步VL)")

        # Step1a + Step1b: VL分别拆解自家和竞品（异步并行）
        t0 = time.time()
        prompt_own = build_single_product_step1_prompt(product_code, category_info)
        comp_category = dict(category_info) if category_info else {}
        comp_category["brand"] = "竞品"
        comp_code = competitor_desc or "竞品"
        prompt_comp = build_single_product_step1_prompt(comp_code, comp_category)

        step1a, step1b = await asyncio.gather(
            self._call_vl_step(own_selected, prompt_own),
            self._call_vl_step(comp_selected, prompt_comp),
        )
        step1_time = time.time() - t0

        # 容错：允许只有一方成功
        if not step1a and not step1b:
            return {"product_code": product_code, "error": "VL拆解失败（自家+竞品均失败）"}
        if not step1a:
            logger.warning("⚠️ 自家产品VL拆解失败，使用空占位")
            step1a = {"section1_visual_analysis": {}, "section2_abc_modules": {"b_level": [], "c_level": []}}
        if not step1b:
            logger.warning("⚠️ 竞品VL拆解失败，使用空占位")
            step1b = {"section1_visual_analysis": {}, "section2_abc_modules": {"b_level": [], "c_level": []}}

        own_b = step1a.get("section2_abc_modules", {}).get("b_level", [])
        comp_b = step1b.get("section2_abc_modules", {}).get("b_level", [])
        n_own = len(own_b) if isinstance(own_b, list) else "?"
        n_comp = len(comp_b) if isinstance(comp_b, list) else "?"
        logger.info(f"✅ Step1 VL拆解完成: 自家{n_own}模块 + 竞品{n_comp}模块 ({step1_time:.1f}s)")

        # Step2: GLM-5做对比分析+推理（section3-9）
        t2 = time.time()
        prompt2 = build_step2_compare_prompt(
            product_code, category_info,
            step1a, step1b,  # 2步VL模式: 传入两方拆解结果
            competitor_desc, upgrade_direction, project_data,
        )
        step2 = await self._call_text_step(prompt2, max_tokens=16000)

        if not step2:
            # 至少返回VL拆解结果
            result = {
                "product_code": product_code,
                "category_type": step1a.get("category_type", ""),
                "section1_visual_analysis": step1a.get("section1_visual_analysis", {}),
                "section2_abc_modules": step1a.get("section2_abc_modules", {}),
                "competitor_modules": step1b.get("section2_abc_modules", {}).get("b_level", []),
                "competitor_visual_analysis": step1b.get("section1_visual_analysis", {}),
                "_step1_time": f"{step1_time:.1f}s",
                "_step2_status": "failed",
                "_mode": "compare_2step",
            }
            return result

        step2_time = time.time() - t2

        # 合并：VL的视觉分析+模块拆解 + GLM的section3-9对比推理
        final = {
            "product_code": product_code,
            "category_type": step1a.get("category_type", ""),
            "section1_visual_analysis": step1a.get("section1_visual_analysis", {}),
            "section2_abc_modules": step1a.get("section2_abc_modules", {}),
            "competitor_modules": step1b.get("section2_abc_modules", {}).get("b_level", []),
            "competitor_visual_analysis": step1b.get("section1_visual_analysis", {}),
            **step2,
            "_step1_time": f"{step1_time:.1f}s",
            "_step2_time": f"{step2_time:.1f}s",
            "_mode": "compare_2step",
        }

        total = step1_time + step2_time
        logger.info(f"✅ 对比报告完成: {product_code} (VL:{step1_time:.1f}s + GLM:{step2_time:.1f}s = {total:.1f}s)")
        return final

    async def analyze(
        self,
        images: list[dict],
        product_code: str = "",
        category_info: Optional[dict] = None,
        known_modules: Optional[list[str]] = None,
        upgrade_direction: str = "",
        project_data: Optional[dict] = None,
    ) -> dict:
        """
        自动选择模式的入口方法

        Args:
            images: 图片列表，每个元素:
                {"data": bytes, "label": "own"/"competitor"/"product", "desc": "描述(可选)"}
            product_code: 货号
            category_info: 品类信息
            known_modules: 已知模块（单商品模式）
            upgrade_direction: 升级方向（对比模式）
            project_data: 立项表信息（对比模式）

        自动判断逻辑：
            - 有label="own"或"competitor"的图片 → 对比模式
            - 所有图片label="product"或无label → 单商品模式
        """
        own_images = []
        competitor_images = []
        single_images = []

        for img in images:
            data = img.get("data", img) if isinstance(img, dict) else img
            label = img.get("label", "product") if isinstance(img, dict) else "product"

            if label == "own":
                own_images.append(data)
            elif label == "competitor":
                competitor_images.append(data)
            else:
                single_images.append(data)

        # 判断模式
        if own_images and competitor_images:
            # 对比模式
            competitor_desc = "竞品"
            for img in images:
                if isinstance(img, dict) and img.get("label") == "competitor" and img.get("desc"):
                    competitor_desc = img["desc"]
                    break
            return await self.analyze_compare(
                own_images=own_images,
                competitor_images=competitor_images,
                product_code=product_code,
                category_info=category_info,
                competitor_desc=competitor_desc,
                upgrade_direction=upgrade_direction,
                project_data=project_data,
            )
        else:
            # 单商品模式：如果有own图就用own，否则用所有图
            img_list = own_images if own_images else (single_images if single_images else competitor_images)
            return await self.analyze_single(
                images=img_list,
                product_code=product_code,
                category_info=category_info,
                known_modules=known_modules,
            )


# ============================================================
# 命令行入口（批量拆解商品图片文件夹）
# ============================================================

async def run_batch(
    image_dir: str = r"F:\商品数据\SERUNA",
    brand: str = "SERUNA",
    concurrency: int = 2,
    dry_run: bool = False,
):
    """批量拆解商品图片文件夹中的所有货号"""

    logger.info("=" * 60)
    logger.info("🧩 VL 商品模块化拆解（批量模式）")
    logger.info(f"图片目录: {image_dir}")
    logger.info(f"品牌: {brand}")
    logger.info("=" * 60)

    splitter = ModuleSplitter()

    # 扫描目录
    if not os.path.isdir(image_dir):
        logger.error(f"图片目录不存在: {image_dir}")
        return

    product_dirs = sorted([
        d for d in os.listdir(image_dir)
        if os.path.isdir(os.path.join(image_dir, d))
        and not d.endswith(".xlsx")
        and not d.startswith("店透视")
    ])
    logger.info(f"发现 {len(product_dirs)} 个货号目录")

    # 匹配品类
    tasks = []
    for code in product_dirs:
        img_dir = os.path.join(image_dir, code)
        images = load_images_from_dir(img_dir)
        if not images:
            continue

        cat_info = query_product_info(DB_PATH, code, brand)
        tasks.append({
            "product_code": code,
            "images": images,
            "category_info": cat_info,
        })

    if dry_run:
        for t in tasks:
            code = t["product_code"]
            cat = t["category_info"]
            cat_str = f"{cat['category1']}>{cat['category2']}>{cat['category3']}" if cat else "DB无记录"
            logger.info(f"  {code}: {cat_str} ({len(t['images'])}张)")
        return

    # 并发执行
    semaphore = asyncio.Semaphore(concurrency)

    async def _analyze_one(task: dict) -> dict:
        async with semaphore:
            code = task["product_code"]
            image_bytes = [data for _, data, _ in task["images"]]
            logger.info(f"🔍 分析 {code} ...")
            result = await splitter.analyze_single(
                images=image_bytes,
                product_code=code,
                category_info=task["category_info"],
            )
            n_b = len(result.get("section2_abc_modules", {}).get("b_level", []))
            logger.info(f"✅ {code}: {result.get('category_type','?')}, {n_b}个B级模块")
            return result

    coros = [_analyze_one(t) for t in tasks]
    results = await asyncio.gather(*coros, return_exceptions=True)

    # 保存
    final_results = []
    for r in results:
        if isinstance(r, Exception):
            final_results.append({"error": str(r)})
        else:
            final_results.append(r)

    out_path = PROJECT_ROOT / "output" / "vl_module_split_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(final_results, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已保存: {out_path}")

    # 打印摘要
    success = [r for r in final_results if "error" not in r]
    logger.info(f"\n完成: {len(success)}/{len(final_results)} 成功")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="VL 商品模块化拆解")
    parser.add_argument("--image-dir", default=r"F:\商品数据\SERUNA")
    parser.add_argument("--brand", default="SERUNA")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    asyncio.run(run_batch(
        image_dir=args.image_dir,
        brand=args.brand,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
