# 爆品升级 — Excel解析提示词

以下是爆品升级立项Excel表格的完整内容：

{sheets_content}

---

请从以上Excel内容中提取以下字段，返回JSON。字段按表格实际区域分组，请严格按语义理解识别，不要依赖固定列位置。

【一、基础信息】
- categoryl1: 一级品类
- categoryl2: 二级品类
- categoryl3: 三级品类
- project_name: 产品名称
- product_code: 自家产品型号/货号（如HY63、HW51，注意是自家产品不是竞品）
- brand: 产品品牌
- applicant: 负责人
- market_size: 市场大小（带时间维度，如"5000万/年"）
- estimated_sales: 目标销售额（带时间维度，如"300万/年"）
- pricing: 价格/毛利（保留原格式，如"139.9元/74%"）
- erp_cost: ERP成本（提取数字，如36.3）
- is_new_category: 是否新品类（是/否，新品类=独立于公司现有供应链体系之外的品类，如骑行手套）

【二、群体分析】
- people_analysis: 人群分析的完整原文（包括人群描述、需求、痛点等，原样保存）
- scene_analysis: 场景分析的完整原文（包括场景描述、场景排序等，原样保存）
- group_extra: 群体分析区域中除人群和场景外的其它内容（如核心人群分析、痛点总结等），找不到则设为null

【三、自有产品升级方向】
- product_code: 自家产品货号（与基础信息中的product_code一致，若该区域有更具体的货号信息则以这里为准）
- upgrade_direction: 具体升级方向（完整原文，如"支撑性升级+外观调整+适用人群扩大"）

【四、产品设计要求】
- design_purpose: 设计目的概述（ABC三大分类选择：A类=品牌爆款/核心利润款，B类=流量款/跑量款，C类=长尾款/补充款。提取字母即可，如"A"或"A类"）
- outlook: 改外观/品牌的具体描述
- material: 升级材料的具体描述
- function: 升级功能的具体描述
- upgrade_modules: 具体升级模块（完整描述，如"1.支撑模块升级成记忆棉 2.外观模块调整"）

== 注意事项 ==
1. 找不到的字段设为null，数组字段找不到则设为空数组[]
2. product_code是自家产品货号，务必提取，通常出现在"升级方向"区域
3. people_analysis和scene_analysis保留原文，不要拆分或重组
4. design_purpose只需提取ABC分类字母，不需要描述
5. 返回纯JSON，不要加```json```包裹
