# -*- coding: utf-8 -*-
"""
VL模型模块拆解 + 模块对比分析

共享能力：
  1. vision_decompose: 用VL模型从图片中拆解模块清单
  2. compare_module_sets: 用LLM对比两组模块，输出差距矩阵+升级建议
  3. build_module_table: 将模块列表格式化为文本表格
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Optional

from product_review_agent.agents.llm_client import LLMClient, get_llm_client

logger = logging.getLogger(__name__)


# ============================================================
# 品类模块定义：按物理结构限定拆解范围
# ============================================================

CATEGORY_MODULES = {
    "护膝": {
        "modules": ["绑带", "面料", "支撑体", "减震垫", "内衬", "涂装/外观"],
        "descriptions": {
            "绑带": "固定+加压的带状结构（单绑带/双绑带/X型交叉/8字缠绕）",
            "面料": "主体织物，承载整体（针织网眼/3D编织/莱卡混纺/竹炭纤维）",
            "支撑体": "侧面刚性/半刚性支撑（弹簧条/铰链/硅胶条/TPU条/无）",
            "减震垫": "髌骨/关节处的缓冲层（硅胶垫/EVA垫/凝胶垫/半透硅胶/无）",
            "内衬": "贴肤层，影响舒适度（针织内衬/绒面/硅胶防滑点/无）",
            "涂装/外观": "颜色、LOGO、装饰（纯色/渐变/反光条/印花）",
        },
        "user_perception": {
            "绑带": "高", "面料": "高", "支撑体": "高",
            "减震垫": "高", "内衬": "中", "涂装/外观": "低",
        },
    },
    "髌骨带": {
        "modules": ["绑带", "面料", "减震垫", "内衬", "固定系统", "涂装/外观"],
        "descriptions": {
            "绑带": "固定+加压的带状结构（单绑带/双绑带/上下双加压）",
            "面料": "主体织物（针织网眼/轻薄锦纶/3D编织/SBR）",
            "减震垫": "髌骨处的缓冲/按摩层（硅胶垫/分段式硅胶条/EVA垫/凝胶垫/无）",
            "内衬": "贴肤层（涤纶网布/绒面/硅胶防滑点/无）",
            "固定系统": "可调节固定结构（医疗级魔术贴/卡扣/无）",
            "涂装/外观": "颜色、LOGO、装饰（纯色/渐变/品牌涂装）",
        },
        "user_perception": {
            "绑带": "高", "面料": "高", "减震垫": "高",
            "内衬": "中", "固定系统": "中", "涂装/外观": "低",
        },
    },
    "护踝": {
        "modules": ["绑带", "面料", "支撑条", "踝骨垫", "脚跟套", "内衬"],
        "descriptions": {
            "绑带": "固定+加压的带状结构（8字缠绕/X型交叉/单环绕/双层缠绕）",
            "面料": "主体织物（针织网眼/3D编织/弹力布/竹炭纤维）",
            "支撑条": "侧面刚性支撑（铝合金条/塑钢条/硅胶条/TPU条/无）",
            "踝骨垫": "踝关节凸起处的保护垫（硅胶垫/EVA垫/凝胶垫/无）",
            "脚跟套": "脚跟固定结构（开放式/半包式/全包式/无）",
            "内衬": "贴肤层（针织内衬/绒面/硅胶防滑条/无）",
        },
        "user_perception": {
            "绑带": "高", "面料": "高", "支撑条": "高",
            "踝骨垫": "中", "脚跟套": "中", "内衬": "低",
        },
    },
    "护腕": {
        "modules": ["绑带", "面料", "支撑条", "拇指固定环", "加压垫"],
        "descriptions": {
            "绑带": "固定+加压的带状结构（单绑带/双绑带/环绕式/8字缠绕）",
            "面料": "主体织物（针织网眼/3D编织/弹力布/莱卡混纺）",
            "支撑条": "掌侧刚性支撑（铝合金板/塑钢板/硅胶条/TPU条/无）",
            "拇指固定环": "拇指穿过的固定结构（弹力环/魔术贴环/无）",
            "加压垫": "局部加压缓冲（硅胶垫/EVA垫/凝胶垫/气囊/无）",
        },
        "user_perception": {
            "绑带": "高", "面料": "高", "支撑条": "高",
            "拇指固定环": "中", "加压垫": "中",
        },
    },
}


def _match_category(category: str) -> str | None:
    """从品类字符串中匹配到品类定义key"""
    if not category:
        return None
    for key in CATEGORY_MODULES:
        if key in category:
            return key
    return None


def _build_module_spec(category_key: str) -> str:
    """为Prompt生成品类模块规范文本"""
    spec = CATEGORY_MODULES[category_key]
    lines = [f"该产品属于【{category_key}】类，必须按以下 {len(spec['modules'])} 个模块拆解："]
    for i, mod in enumerate(spec["modules"], 1):
        desc = spec["descriptions"][mod]
        perception = spec["user_perception"][mod]
        lines.append(f"  {i}. {mod} — {desc}（用户感知度：{perception}）")
    lines.append("")
    lines.append("严格要求：")
    lines.append(f"1. module_name 必须使用以上 {len(spec['modules'])} 个模块名称之一，不得自创新名称")
    lines.append("2. 如果图片中某个模块不存在（如无支撑体/无内衬），仍需列出该模块，appearance填\"无\"，material填\"无\"")
    lines.append("3. 如果图片不清晰无法判断某模块，confidence填\"low\"，但module_name仍必须使用规定名称")
    return "\n".join(lines)


# ============================================================
# VL模型：从图片拆解模块
# ============================================================

VL_DECOMPOSE_PROMPT = """【任务】仔细观察这张产品图片，将其拆解为独立的功能/材质模块。
【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。

{module_spec}

产品品类: {category}
产品名称: {product_name}

【每个模块需描述】
- material: 材质描述
- function: 功能描述
- appearance: 外观描述（颜色/形状/位置）
- confidence: 判断置信度（high/medium/low）

【输出格式】严格按此JSON结构输出：
{{
    "modules": [
        {{
            "module_name": "绑带",
            "material": "针织网眼+魔术贴",
            "function": "固定加压",
            "appearance": "黑色带状，双绑带交叉",
            "confidence": "high"
        }},
        {{
            "module_name": "减震垫",
            "material": "硅胶",
            "function": "缓冲减震",
            "appearance": "灰色半透明圆垫",
            "confidence": "medium"
        }}
    ],
    "overall_description": "产品整体描述（50-100字）",
    "image_quality": "good"
}}"""

VL_DECOMPOSE_PROMPT_FALLBACK = """【任务】仔细观察这张产品图片，将其拆解为独立的功能/材质模块。
【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。

【拆解规则】
1. 按功能和材质划分，例如：绑带/支撑体/垫片/内衬/面料/涂装/固定件等
2. 每个模块描述其：材质、功能、外观特征
3. 如果图片不清晰或无法判断，confidence填"low"
4. 尽量拆细，但不要过度拆分（通常5-10个模块）

产品品类: {category}
产品名称: {product_name}

【输出格式】严格按此JSON结构输出：
{{
    "modules": [
        {{
            "module_name": "绑带",
            "material": "弹性织带+魔术贴",
            "function": "固定加压",
            "appearance": "黑色X型交叉带",
            "confidence": "high"
        }},
        {{
            "module_name": "内衬",
            "material": "待确认",
            "function": "贴肤舒适",
            "appearance": "内侧浅色层",
            "confidence": "low"
        }}
    ],
    "overall_description": "产品整体描述（50-100字）",
    "image_quality": "medium"
}}"""


async def vision_decompose(
    llm: LLMClient,
    image_data: bytes | str,
    category: str = "",
    product_name: str = "",
) -> dict:
    """
    用VL模型从图片中拆解产品模块。

    Args:
        llm: LLM客户端
        image_data: 图片数据（bytes=base64编码，str=URL/路径）
        category: 品类信息
        product_name: 产品名称

    Returns:
        {"modules": [...], "overall_description": str, "image_quality": str}
    """
    if not llm.is_available:
        return _fallback_decompose("LLM不可用")

    # 根据品类选择Prompt（有品类定义用限定版，否则用通用版）
    category_key = _match_category(category)
    if category_key:
        module_spec = _build_module_spec(category_key)
        prompt_text = VL_DECOMPOSE_PROMPT.format(
            module_spec=module_spec,
            category=category or "未知",
            product_name=product_name or "未知产品",
        )
    else:
        prompt_text = VL_DECOMPOSE_PROMPT_FALLBACK.format(
            category=category or "未知",
            product_name=product_name or "未知产品",
        )

    messages = [
        {"role": "system", "content": "你是产品模块拆解专家。严格只返回JSON对象，禁止输出思考过程、解释文字或markdown代码块。"},
        LLMClient.build_image_message(
            [image_data],
            text=prompt_text,
        ),
    ]

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 4096

    try:
        result = await llm.acall_vision(messages, response_format="json")

        if isinstance(result, dict) and not result.get("_parse_error"):
            # 确保结构完整
            if "modules" not in result:
                result = {"modules": [], "overall_description": str(result)[:200], "image_quality": "unknown"}
            return result
        else:
            raw = result.get("_raw_text", "") if isinstance(result, dict) else str(result)
            logger.warning(f"[VL拆解] 返回格式异常: {raw[:200]}")
            return _fallback_decompose("返回格式异常")

    except Exception as e:
        logger.error(f"[VL拆解] 异常: {e}")
        return _fallback_decompose(str(e))

    finally:
        llm.max_tokens = original_max_tokens


async def vision_decompose_multiple(
    llm: LLMClient,
    images: list[bytes | str],
    category: str = "",
    product_name: str = "",
) -> dict:
    """
    多张图片一起拆解（同一产品多角度）。

    Args:
        images: 多张图片数据列表

    Returns:
        同 vision_decompose
    """
    if not images:
        return _fallback_decompose("无图片数据")

    if len(images) == 1:
        return await vision_decompose(llm, images[0], category, product_name)

    # 多图一起发送
    category_key = _match_category(category)
    if category_key:
        module_spec = _build_module_spec(category_key)
        prompt_text = VL_DECOMPOSE_PROMPT.format(
            module_spec=module_spec,
            category=category or "未知",
            product_name=product_name or "未知产品",
        )
    else:
        prompt_text = VL_DECOMPOSE_PROMPT_FALLBACK.format(
            category=category or "未知",
            product_name=product_name or "未知产品",
        )
    prompt_text += "\n\n注意：这是同一产品的多张图片（不同角度），请综合所有图片进行模块拆解。"

    messages = [
        {"role": "system", "content": "你是产品模块拆解专家。严格只返回JSON对象，禁止输出思考过程、解释文字或markdown代码块。"},
        LLMClient.build_image_message(images, text=prompt_text),
    ]

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 4096

    try:
        result = await llm.acall_vision(messages, response_format="json")
        if isinstance(result, dict) and not result.get("_parse_error"):
            if "modules" not in result:
                result = {"modules": [], "overall_description": str(result)[:200], "image_quality": "unknown"}
            return result
        else:
            return _fallback_decompose("返回格式异常")
    except Exception as e:
        logger.error(f"[VL多图拆解] 异常: {e}")
        return _fallback_decompose(str(e))
    finally:
        llm.max_tokens = original_max_tokens


# ============================================================
# LLM模块对比分析
# ============================================================

COMPARE_PROMPT = """【任务】深入对比我方产品与竞品的模块构成，给出详细的差距分析、升级建议和市场策略。
【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。

== 我方产品信息 ==
产品名称: {our_name}
品类: {our_category}

== 我方产品模块 ==
{our_modules_table}

== 竞品信息 ==
竞品名称: {competitor_name}
竞品品类: {competitor_category}

== 竞品模块（来自图片拆解）==
{competitor_modules_table}

== 立项产品模块信息（如有）==
{project_modules_info}

【分析要求】
1. 逐模块对比：找到我方和竞品相同/相似模块，详细评估各自优劣，给出具体技术/材质/设计差异
2. 差距矩阵：我方哪些模块领先、持平、落后，每个差距给出具体的改进方向
3. 用户感知评估：哪些模块差异用户能明显感知，哪些是隐性差异
4. 升级优先级：差距大且用户感知强的模块优先，给出P0/P1/P2分级
5. 可复用建议：哪些现有模块可以直接复用（说明来源产品），哪些需要新建/改造
6. 市场定位：基于模块对比结果，我方产品应采取什么市场定位策略

【输出格式】严格按此JSON结构输出（module_comparison至少4条，summary各字段2-4条且内容详细，upgrade_roadmap至少3条）：
{{
    "module_comparison": [
        {{
            "module_name": "绑带",
            "our_status": "领先",
            "competitor_status": "单绑带，加压力度弱",
            "gap_level": "低",
            "user_perception": "高",
            "upgrade_priority": "P2",
            "suggestion": "保持现有双绑带设计"
        }},
        {{
            "module_name": "减震垫",
            "our_status": "落后",
            "competitor_status": "加厚半透硅胶垫",
            "gap_level": "高",
            "user_perception": "高",
            "upgrade_priority": "P0",
            "suggestion": "升级为加厚硅胶垫"
        }}
    ],
    "summary": {{
        "our_strengths": ["我方优势1（具体说明）", "我方优势2", "我方优势3"],
        "our_weaknesses": ["我方不足1（具体说明）", "我方不足2"],
        "reuse_modules": ["可复用模块1 (来源产品)", "可复用模块2"],
        "new_modules_needed": ["需新建模块1 (具体说明)", "需新建模块2"],
        "market_positioning": "市场定位建议（100-150字）",
        "overall_assessment": "整体评价（150-300字，结合模块对比给出综合结论和行动建议）"
    }},
    "upgrade_roadmap": [
        {{
            "priority": "P0",
            "module": "模块名",
            "action": "具体升级动作",
            "expected_impact": "预期效果"
        }}
    ]
}}"""


async def compare_module_sets(
    llm: LLMClient,
    our_product: dict,
    competitor_product: dict,
    project_modules: list[dict] | None = None,
) -> dict:
    """
    对比我方与竞品的模块构成。

    Args:
        llm: LLM客户端
        our_product: 我方产品信息 {"name", "category", "modules": [...]}
        competitor_product: 竞品信息 {"name", "category", "modules": [...]}
        project_modules: 立项产品的模块信息（可选）

    Returns:
        {"module_comparison": [...], "summary": {...}, "upgrade_roadmap": [...]}
    """
    if not llm.is_available:
        return _fallback_compare("LLM不可用")

    our_table = build_module_table(our_product.get("modules", []))
    competitor_table = build_module_table(competitor_product.get("modules", []))

    if project_modules:
        project_info = build_module_table(project_modules)
        project_info = f"立项产品已有模块:\n{project_info}"
    else:
        project_info = "（立项产品模块信息暂缺）"

    prompt = COMPARE_PROMPT.format(
        our_name=our_product.get("name", "我方产品"),
        our_category=our_product.get("category", ""),
        our_modules_table=our_table,
        competitor_name=competitor_product.get("name", "竞品"),
        competitor_category=competitor_product.get("category", ""),
        competitor_modules_table=competitor_table,
        project_modules_info=project_info,
    )

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 8192

    try:
        result = await llm.acall_text(
            [
                {"role": "system", "content": "你是产品模块对比分析专家，擅长模块化产品拆解、差距分析和升级策略制定。严格只返回JSON对象，禁止输出思考过程、解释文字或markdown代码块。分析必须基于提供的数据，给出具体可执行的结论，避免空泛表述。"},
                {"role": "user", "content": prompt},
            ],
            response_format="json",
        )

        if isinstance(result, dict) and not result.get("_parse_error"):
            return result
        else:
            return _fallback_compare("返回格式异常")

    except Exception as e:
        logger.error(f"[模块对比] 异常: {e}")
        return _fallback_compare(str(e))

    finally:
        llm.max_tokens = original_max_tokens


# ============================================================
# 格式化工具
# ============================================================

def build_module_table(modules: list[dict]) -> str:
    """将模块列表格式化为文本表格"""
    if not modules:
        return "（无模块数据）"

    lines = ["模块名 | 类别 | 材质/子类型 | 用途 | 供应商"]
    lines.append("---|------|------|------|------")

    for m in modules:
        name = m.get("module_name") or m.get("cbb_name") or "-"
        category = m.get("category") or "-"
        sub_type = m.get("sub_type") or m.get("material") or "-"
        position = m.get("used_position") or m.get("function") or "-"
        supplier = m.get("supplier") or "-"

        lines.append(f"{name} | {category} | {sub_type} | {position} | {supplier}")

    return "\n".join(lines)


def build_product_module_summary(products: list[dict]) -> str:
    """将多个产品的模块信息汇总为文本"""
    if not products:
        return "（无产品数据）"

    parts = []
    for p in products:
        code = p.get("product_code") or p.get("sku", "-")
        brand = p.get("brand", "-")
        cat3 = p.get("category_l3", "-")
        modules = p.get("modules", [])

        parts.append(f"\n### 产品 {code} ({brand}) - {cat3}")
        parts.append(build_module_table(modules))
        parts.append("")

    return "\n".join(parts)


# ============================================================
# 降级返回
# ============================================================

def _fallback_decompose(reason: str) -> dict:
    return {
        "modules": [],
        "overall_description": f"模块拆解不可用（{reason}）",
        "image_quality": "unknown",
        "_error": reason,
    }


def _fallback_compare(reason: str) -> dict:
    return {
        "module_comparison": [],
        "summary": {
            "our_strengths": [],
            "our_weaknesses": [],
            "reuse_modules": [],
            "new_modules_needed": [],
            "overall_assessment": f"模块对比分析暂不可用（{reason}）",
        },
        "upgrade_roadmap": [],
        "_error": reason,
    }
