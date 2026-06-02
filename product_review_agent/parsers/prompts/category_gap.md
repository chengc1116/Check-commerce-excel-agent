# 品类缺失 — Excel解析提示词

以下是品类缺失立项Excel表格的完整内容：

{sheets_content}

---

请从以上Excel内容中提取以下字段，返回JSON。字段按表格实际区域分组，请严格按语义理解识别，不要依赖固定列位置。

【一、基础信息】
- categoryl1: 一级品类
- categoryl2: 二级品类
- categoryl3: 三级品类
- project_name: 产品名称
- product_code: 自家产品型号/货号（如HY63，注意是自家产品不是竞品。找不到则设为null）
- brand: 产品品牌
- applicant: 负责人
- market_size: 市场大小（带时间维度和价格段信息，如"5000万/年，100-200元价格段"）
- estimated_sales: 目标销售额（带时间维度，如"300万/年"）
- pricing: 价格/毛利（保留原格式，如"139.9元/74%"）
- erp_cost: ERP成本（提取数字，如36.3）
- is_new_category: 是否新品类（是/否。是=True=品类缺失场景B，否=False=品牌缺失场景A）
- base_extra: 基础信息区域中除以上字段外的其它有价值内容，找不到则设为null

【二、群体分析】
- people_analysis: 人群分析的完整原文（包括人群描述、需求、痛点等，原样保存）
- scene_analysis: 场景分析的完整原文（包括场景描述、场景排序等，原样保存）
- group_extra: 群体分析区域中除人群和场景外的其它内容（如核心人群分析、痛点总结等），找不到则设为null

【三、类似产品信息】
- similar_product_code: 类似产品货号（自家其他品牌的同品类产品货号，如HY63。注意是自家产品不是竞品。找不到则设为null）
- similar_product_selling_point: 类似产品卖点（完整原文，描述该类似产品的核心卖点和特点）

【四、产品设计要求】
- design_purpose: 设计目的概述（保留原文）
- design_content: 具体设计内容（完整原文，描述产品具体的设计方案、模块调整、材料选择等）
- feasibility_analysis: 可行性分析（完整原文，描述模块获取方式、供应链可行性、开模难度等。此字段非常重要，请务必提取）

【五、竞品信息】
- competitor_name: 竞品名称/品牌（如"LP""迈克达威"，找不到则设为null）
- competitor_price: 竞品价格（保留原格式，如"89.9"，找不到则设为null）
- competitor_url: 竞品链接（URL，找不到则设为null）

== 注意事项 ==
1. 找不到的字段设为null，数组字段找不到则设为空数组[]
2. similar_product_code是自家其他品牌的同品类产品货号，不是竞品货号，注意区分
3. competitor_price是竞品价格，不是自家价格，注意区分
4. people_analysis和scene_analysis保留原文，不要拆分或重组
5. design_content和feasibility_analysis保留原文，非常重要
6. 返回纯JSON，不要加```json```包裹
